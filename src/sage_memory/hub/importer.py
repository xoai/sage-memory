"""M3.4 — Transactional import of a source DB into a hub-registered project.

Per ADR-008 §"hub import" + ADR-009 §6:

  - Source DB opened **read-only** via ``file:<path>?mode=ro`` URI
    (sqlite3 enforces no writes, even by us).
  - Destination ownership acquired BEFORE the transaction begins;
    released AFTER commit-or-rollback (try/finally — release happens
    on every exit path).
  - The entire import wraps in a single ``with destination_conn:``
    block — sqlite3's context-manager commits on clean exit, rolls
    back on any exception. No partial state survives a mid-import
    error.
  - Dedup on ``content_hash``: a memory whose hash already exists in
    the destination is skipped (counted, not failed).
  - ``memories`` + ``memories_vec`` + ``edges`` all copied. Edges
    whose endpoints don't resolve in the destination (post-import)
    are silently dropped — keeps the destination's referential
    integrity invariant.

NOTE: module is `importer.py` not `import.py` because `import` is a
Python keyword (ADR-008 rev 2 rename — `from sage_memory.hub import
import` would not parse).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from . import config as _hub_config
from . import ownership as _hub_ownership


logger = logging.getLogger("sage_memory.hub.importer")


def import_from_source(
    source_db: Path,
    target_project: str,
    *,
    hub_path: Path | None = None,
) -> dict[str, Any]:
    """Import memories + vec + edges from ``source_db`` into ``target_project``.

    Returns an envelope:
      Success: ``{success: True, imported: N, skipped: M, edges: K, message: ...}``
      Failure: ``{success: False, message: ...}`` — no destination state changed.
    """
    cfg_path = hub_path or _hub_config.DEFAULT_HUB_PATH
    if not cfg_path.exists():
        return {
            "success": False,
            "message": (
                f"hub config not found at {cfg_path}. "
                f"Run `sage-memory hub init` first."
            ),
        }

    try:
        cfg = _hub_config.load(cfg_path)
    except Exception as exc:
        return {"success": False, "message": f"could not load hub config: {exc}"}

    project = next((p for p in cfg.projects if p.name == target_project), None)
    if project is None:
        return {"success": False, "message": f"no hub project named {target_project!r}"}
    if not project.writable:
        return {
            "success": False,
            "message": (
                f"project {target_project!r} is not writable. Re-add with "
                f"`--writable` to enable imports."
            ),
        }

    source_db = Path(source_db)
    if not source_db.exists():
        return {
            "success": False,
            "message": f"source DB does not exist: {source_db}",
        }

    # Open source read-only — the URI form prevents any accidental write
    # (sqlite3 returns SQLITE_READONLY on attempted modification).
    try:
        source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        return {
            "success": False,
            "message": f"could not open source DB read-only: {exc}",
        }
    source.row_factory = sqlite3.Row
    # memories_vec is backed by the sqlite_vec extension (vec0 module).
    # Reading it requires the extension to be loaded into THIS conn —
    # extension state isn't shared across connections.
    try:
        import sqlite_vec
        source.enable_load_extension(True)
        sqlite_vec.load(source)
        source.enable_load_extension(False)
    except Exception as exc:  # pragma: no cover — install-error path
        source.close()
        return {
            "success": False,
            "message": f"could not load sqlite_vec into source: {exc}",
        }

    # Acquire ownership of destination (or piggyback if server already holds it).
    already_owned = str(project.path) in _hub_ownership.registry
    transient_token = None
    if not already_owned:
        transient_token = _hub_ownership.acquire(project.path)
        if transient_token is None:
            source.close()
            info = _hub_ownership.check(project.path)
            pid = info.owner_pid if info else "?"
            return {
                "success": False,
                "message": (
                    f"project {target_project!r} is hub-managed by PID {pid}; "
                    f"cannot import."
                ),
            }

    try:
        return _do_import(source, project.path)
    finally:
        source.close()
        if transient_token is not None:
            _hub_ownership.release(transient_token)


def _do_import(
    source: sqlite3.Connection, project_path: Path,
) -> dict[str, Any]:
    """Transactional copy. Switches active project context; restores it
    on every exit path."""
    from .. import db as _db

    prior_root = _db._active_project
    prior_set = _db._active_project_set

    try:
        _db.close_all()
        _db.override_project_root(project_path)
        dest = _db._open(_db.get_project_db_path(project_path))

        # Single transaction on the destination. sqlite3 context-manager:
        # commits on clean exit, rolls back on any exception → no
        # partial state survives.
        imported = 0
        skipped = 0
        edges_copied = 0
        try:
            with dest:
                imported, skipped, id_remap = _copy_memories(source, dest)
                edges_copied = _copy_edges(source, dest, id_remap)
        except Exception as exc:
            logger.exception("hub.importer: transaction rolled back")
            return {
                "success": False,
                "message": f"import failed (transaction rolled back): {exc}",
            }

        return {
            "success": True,
            "imported": imported,
            "skipped": skipped,
            "edges": edges_copied,
            "message": (
                f"imported {imported} memories ({skipped} duplicates "
                f"skipped) + {edges_copied} edges"
            ),
        }
    finally:
        _db.close_all()
        if prior_set:
            _db.override_project_root(prior_root)
        else:
            _db.override_project_root(None)
            _db._active_project_set = False


def _copy_memories(
    source: sqlite3.Connection,
    dest: sqlite3.Connection,
) -> tuple[int, int, dict[str, str]]:
    """Copy memories + memories_vec rows. Dedup on content_hash.

    Returns (imported, skipped, id_remap) where ``id_remap`` maps each
    SOURCE memory id to the corresponding DESTINATION id:
      - For copied rows, dest_id == source_id (we INSERT with the
        source id).
      - For dedup-skipped rows, dest_id is the EXISTING destination
        id whose content_hash matched (M3 review M2 — without the
        remap, edges to dedup-skipped memories would be silently
        dropped).

    Also handles id-collision (M3 review C1): if a SOURCE row's id
    already exists in destination AND content_hash differs, the
    destination's row wins (preserves the destination's authority
    over its existing ids). The source row is skipped with the
    destination's row added to the remap so its edges still point
    at the destination memory.
    """
    src_rows = source.execute("SELECT * FROM memories").fetchall()
    imported = 0
    skipped = 0
    id_remap: dict[str, str] = {}
    for row in src_rows:
        src_id = row["id"]

        # Content-hash dedup: destination already has equivalent content.
        existing_by_hash = dest.execute(
            "SELECT id FROM memories WHERE content_hash = ? LIMIT 1",
            (row["content_hash"],),
        ).fetchone()
        if existing_by_hash is not None:
            skipped += 1
            id_remap[src_id] = existing_by_hash[0]
            continue

        # ID-collision (M3 review C1): destination has SAME id but
        # DIFFERENT content. Pre-M3 review behavior would have
        # crashed the entire import on the UNIQUE(id) constraint
        # below. Now we keep the destination's row authoritative
        # and skip the source row.
        existing_by_id = dest.execute(
            "SELECT id FROM memories WHERE id = ? LIMIT 1",
            (src_id,),
        ).fetchone()
        if existing_by_id is not None:
            skipped += 1
            id_remap[src_id] = src_id  # dest's existing row keeps the id
            logger.warning(
                "hub.importer: id-collision on %s (destination keeps "
                "its existing memory; source skipped)",
                src_id,
            )
            continue

        dest.execute(
            """
            INSERT INTO memories (
                id, title, content, tags, content_hash, embedded,
                created_at, updated_at, accessed_at, access_count, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                src_id, row["title"], row["content"], row["tags"],
                row["content_hash"], row["embedded"],
                row["created_at"], row["updated_at"], row["accessed_at"],
                row["access_count"], row["status"],
            ),
        )
        id_remap[src_id] = src_id
        imported += 1

        # Copy the vec row if present (only for embedded=1 memories).
        if row["embedded"]:
            vec_row = source.execute(
                "SELECT embedding FROM memories_vec WHERE memory_id = ?",
                (src_id,),
            ).fetchone()
            if vec_row is not None:
                dest.execute(
                    "INSERT INTO memories_vec (memory_id, embedding) VALUES (?, ?)",
                    (src_id, vec_row["embedding"]),
                )

    return imported, skipped, id_remap


def _copy_edges(
    source: sqlite3.Connection,
    dest: sqlite3.Connection,
    id_remap: dict[str, str],
) -> int:
    """Copy edges, remapping endpoints through ``id_remap`` (M3 review M2).

    Pre-fix behavior: edges referencing dedup-skipped source memories
    were silently dropped because their source/target ids didn't
    resolve in the destination. Post-fix: ``id_remap`` carries the
    correct destination id for every source memory (copied or
    dedup-skipped), so edges follow the merge.

    Edges where either endpoint isn't in the remap (source had a
    dangling edge — referenced a memory that's missing in source
    itself) are still dropped; SQLite's FK enforcement would reject
    them anyway.
    """
    src_edges = source.execute("SELECT * FROM edges").fetchall()
    copied = 0
    for e in src_edges:
        new_source = id_remap.get(e["source_id"])
        new_target = id_remap.get(e["target_id"])
        if new_source is None or new_target is None:
            continue
        # Don't INSERT OR IGNORE on duplicate (source, target, relation)
        # — that masks bugs. Skip explicitly.
        dup = dest.execute(
            """
            SELECT 1 FROM edges
             WHERE source_id = ? AND target_id = ? AND relation = ?
             LIMIT 1
            """,
            (new_source, new_target, e["relation"]),
        ).fetchone()
        if dup is not None:
            continue
        # Edge id may collide if we re-imported (rare; the dest.edges
        # PK is uuid hex like memories.id). On collision, generate a
        # new id rather than crashing the whole transaction.
        edge_id = e["id"]
        if dest.execute(
            "SELECT 1 FROM edges WHERE id = ? LIMIT 1", (edge_id,),
        ).fetchone() is not None:
            import uuid
            edge_id = uuid.uuid4().hex
        dest.execute(
            """
            INSERT INTO edges (
                id, source_id, target_id, relation, properties, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                edge_id, new_source, new_target, e["relation"],
                e["properties"], e["created_at"],
            ),
        )
        copied += 1
    return copied
