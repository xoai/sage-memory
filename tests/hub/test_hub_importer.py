"""M3.4 — hub.importer.import_from_source.

8 tests per plan.md M3.4 done-when:
  1. Full round-trip on small DB
  2. Idempotent re-import (content_hash dedup)
  3. Broken source → clear error + zero destination state
  4. Rollback on mid-import failure (no partial commit)
  5. Ownership released even on rollback (finally-block guarantee)
  6. Source bit-identical after import (read-only verify)
  7. memories + memories_vec + edges all copied
  8. Edge refs pointing at copied memories resolve correctly
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from sage_memory.db import (
    _open, close_all, get_project_db_path, override_project_root,
)
from sage_memory.embedder import (
    EMBEDDING_DIM, LocalEmbedder, set_embedder,
)
from sage_memory.hub import config as hub_config
from sage_memory.hub import importer as hub_importer
from sage_memory.hub import ownership as hub_ownership


class _TestEmbedder:
    name = "test-m34"
    version = "v1"
    dim = EMBEDDING_DIM
    quality = 0.9
    max_input_chars = 8192

    def embed(self, text):
        h = abs(hash(text)) % 100
        return [(h + i) / 100.0 for i in range(EMBEDDING_DIM)]


@pytest.fixture
def source_and_dest(tmp_path, monkeypatch):
    """Seed a source DB with 2 memories + 1 edge, register a writable
    destination project. Returns (hub_path, source_db_path, dest_root,
    src_memory_ids)."""
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    set_embedder(_TestEmbedder())
    hub_ownership.reset_module_state_for_tests()

    # Build the source project.
    src_root = tmp_path / "source"
    src_root.mkdir()
    (src_root / ".git").mkdir()
    (src_root / ".sage-memory").mkdir()

    close_all()
    override_project_root(src_root)
    _open(get_project_db_path(src_root))

    from sage_memory.store import store as _store
    from sage_memory.graph import link

    r1 = _store(content="Source memory one — runbook for auth", title="m1")
    r2 = _store(content="Source memory two — runbook for billing", title="m2")
    link(source_id=r1["id"], target_id=r2["id"], relation="related_to")
    src_memory_ids = [r1["id"], r2["id"]]

    source_db = get_project_db_path(src_root)
    close_all()  # ensure WAL flushed

    # Build the destination project (empty, writable in hub).
    dest_root = tmp_path / "dest"
    dest_root.mkdir()
    (dest_root / ".git").mkdir()
    (dest_root / ".sage-memory").mkdir()

    hub_path = tmp_path / ".sage-hub.yaml"
    cfg = hub_config.init(hub_path)
    cfg = hub_config.add_project(cfg, "dest", dest_root, writable=True)
    hub_config.save(cfg, hub_path)

    yield hub_path, source_db, dest_root, src_memory_ids

    close_all()
    set_embedder(LocalEmbedder())
    hub_ownership.reset_module_state_for_tests()


# ─── 1. Full round-trip on small DB ───────────────────────────────


def test_importer_round_trip_small_db(source_and_dest):
    hub_path, source_db, dest_root, src_ids = source_and_dest
    res = hub_importer.import_from_source(
        source_db, "dest", hub_path=hub_path,
    )
    assert res["success"] is True, f"{res!r}"
    assert res["imported"] == 2
    assert res["skipped"] == 0
    assert res["edges"] == 1


# ─── 2. Idempotent re-import (content_hash dedup) ────────────────


def test_importer_idempotent_re_import(source_and_dest):
    hub_path, source_db, dest_root, _ = source_and_dest

    res1 = hub_importer.import_from_source(source_db, "dest", hub_path=hub_path)
    assert res1["success"] is True
    assert res1["imported"] == 2

    res2 = hub_importer.import_from_source(source_db, "dest", hub_path=hub_path)
    assert res2["success"] is True
    assert res2["imported"] == 0, "re-import must dedup all 2 memories"
    assert res2["skipped"] == 2


# ─── 3. Broken source → clear error + zero destination state ────


def test_importer_broken_source_clear_error(source_and_dest, tmp_path):
    hub_path, _, dest_root, _ = source_and_dest
    res = hub_importer.import_from_source(
        tmp_path / "no-such.db", "dest", hub_path=hub_path,
    )
    assert res["success"] is False
    assert "source" in res["message"].lower()

    # Destination state untouched.
    dest_db = get_project_db_path(dest_root)
    if dest_db.exists():
        conn = sqlite3.connect(f"file:{dest_db}?mode=ro", uri=True)
        try:
            count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            assert count == 0, f"destination must be empty; got {count}"
        finally:
            conn.close()


# ─── 4. Rollback on mid-import failure (no partial commit) ──────


def test_importer_rolls_back_on_mid_failure(source_and_dest, monkeypatch):
    """Inject a failure mid-copy: monkeypatch _copy_edges to raise.
    Assert no memories landed (transaction rolled back)."""
    hub_path, source_db, dest_root, _ = source_and_dest

    def _boom(*a, **kw):
        raise RuntimeError("synthetic mid-import failure")

    monkeypatch.setattr(hub_importer, "_copy_edges", _boom)

    res = hub_importer.import_from_source(
        source_db, "dest", hub_path=hub_path,
    )
    assert res["success"] is False
    assert "rolled back" in res["message"].lower()

    # Destination memories table must be empty — full transaction rolled back.
    close_all()
    override_project_root(dest_root)
    dest_conn = _open(get_project_db_path(dest_root))
    count = dest_conn.execute(
        "SELECT COUNT(*) FROM memories"
    ).fetchone()[0]
    assert count == 0, (
        f"rollback failed — destination should have 0 memories, got {count}"
    )


# ─── 5. Ownership released even on rollback ─────────────────────


def test_importer_releases_ownership_on_rollback(
    source_and_dest, monkeypatch,
):
    """When the transient ownership token was acquired by the
    importer, it must be released even when the import fails."""
    hub_path, source_db, dest_root, _ = source_and_dest

    def _boom(*a, **kw):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(hub_importer, "_copy_edges", _boom)

    assert str(dest_root) not in hub_ownership.registry
    res = hub_importer.import_from_source(
        source_db, "dest", hub_path=hub_path,
    )
    assert res["success"] is False

    # Registry must be empty — the token we acquired transiently was
    # released in the finally block.
    assert str(dest_root) not in hub_ownership.registry, (
        f"ownership token leaked: {hub_ownership.registry!r}"
    )
    # Owner file deleted.
    owner_file = dest_root / ".sage-memory" / ".hub-owner.json"
    assert not owner_file.exists(), (
        "owner file must be deleted on release after rollback"
    )


# ─── 6. Source bit-identical after import ────────────────────────


def test_importer_does_not_modify_source(source_and_dest):
    hub_path, source_db, _, _ = source_and_dest

    before_hash = hashlib.sha256(source_db.read_bytes()).hexdigest()
    res = hub_importer.import_from_source(
        source_db, "dest", hub_path=hub_path,
    )
    assert res["success"] is True
    after_hash = hashlib.sha256(source_db.read_bytes()).hexdigest()
    assert after_hash == before_hash, (
        f"source DB modified by import; before={before_hash} after={after_hash}"
    )


# ─── 7. memories + memories_vec + edges all copied ───────────────


def test_importer_copies_memories_vec_and_edges(source_and_dest):
    hub_path, source_db, dest_root, src_ids = source_and_dest

    res = hub_importer.import_from_source(source_db, "dest", hub_path=hub_path)
    assert res["success"] is True

    dest_db = get_project_db_path(dest_root)
    conn = sqlite3.connect(f"file:{dest_db}?mode=ro", uri=True)
    # memories_vec is backed by the sqlite_vec extension; queries
    # against it require the extension to be loaded into THIS conn.
    import sqlite_vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    try:
        m_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        assert m_count == 2
        # Each memory has an embedding row.
        v_count = conn.execute(
            "SELECT COUNT(*) FROM memories_vec"
        ).fetchone()[0]
        assert v_count >= 2, (
            f"memories_vec count: expected >= 2 (one per embedded memory), got {v_count}"
        )
        e_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        assert e_count == 1
    finally:
        conn.close()


# ─── 8. Edge refs resolve in destination ─────────────────────────


def test_importer_id_collision_with_different_hash_is_handled(
    source_and_dest,
):
    """Regression for /review M3 CRITICAL #1.

    Pre-fix: a source memory whose id collides with a destination
    memory (but different content_hash, so dedup doesn't skip) would
    hit the UNIQUE(id) constraint at INSERT time and roll back the
    ENTIRE import on a single colliding row. Post-fix: the source
    row is skipped (destination's existing memory wins), the
    id_remap points the source id at the destination's existing
    memory so subsequent edges still resolve, and other rows in the
    import succeed normally."""
    hub_path, source_db, dest_root, _src_ids = source_and_dest

    # Seed the destination with a memory that shares an id with one
    # of the source's memories but has DIFFERENT content.
    # Pull the source's first memory id from source_db directly.
    src_conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    try:
        existing_src_row = src_conn.execute(
            "SELECT id FROM memories LIMIT 1"
        ).fetchone()
    finally:
        src_conn.close()
    colliding_id = existing_src_row[0]

    # Insert a destination row with the same id, different content.
    close_all()
    override_project_root(dest_root)
    dest_conn = _open(get_project_db_path(dest_root))
    dest_conn.execute(
        """INSERT INTO memories (
              id, title, content, tags, content_hash, embedded,
              created_at, updated_at, accessed_at, access_count, status
           ) VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 'active')""",
        (
            colliding_id, "different-content",
            "Destination has a different memory under the same id",
            "[]",
            "hash-different-from-source",
        ),
    )
    dest_conn.commit()
    close_all()

    res = hub_importer.import_from_source(
        source_db, "dest", hub_path=hub_path,
    )
    # Import succeeds. The colliding row is skipped (destination wins);
    # the other source memory is imported normally.
    assert res["success"] is True, (
        f"id-collision must not crash the import; got {res!r}"
    )
    assert res["skipped"] >= 1, (
        f"id-collision must count as skipped; got {res!r}"
    )


def test_importer_edges_follow_dedup_remap(source_and_dest):
    """Regression for /review M3 MAJOR #2.

    Pre-fix: when a source memory was dedup-skipped (destination
    already has equivalent content_hash), edges referencing that
    source memory were silently dropped because their source/target
    ids didn't resolve in the destination. Post-fix: the id_remap
    points the source memory's id at the destination's equivalent
    memory, so the edge survives the merge."""
    hub_path, source_db, dest_root, src_ids = source_and_dest

    # First import lands both memories + the edge. Confirm.
    res1 = hub_importer.import_from_source(
        source_db, "dest", hub_path=hub_path,
    )
    assert res1["success"] is True

    # Now seed a SECOND source DB with content matching the
    # destination's memories (same content_hash) plus an EXTRA edge
    # between them. Re-import: the memories dedup-skip but the
    # extra edge should land (via remap).
    second_src_root = source_db.parent.parent.parent / "second-src"
    second_src_root.mkdir()
    (second_src_root / ".git").mkdir()
    (second_src_root / ".sage-memory").mkdir()
    close_all()
    override_project_root(second_src_root)
    _open(get_project_db_path(second_src_root))
    from sage_memory.store import store as _store
    from sage_memory.graph import link
    # Same content as the destination already has (will dedup-skip).
    r1 = _store(content="Source memory one — runbook for auth", title="dup1")
    r2 = _store(content="Source memory two — runbook for billing", title="dup2")
    # An edge with a DIFFERENT relation than the one already imported.
    link(source_id=r1["id"], target_id=r2["id"], relation="m3_review_m2_remap")

    second_source = get_project_db_path(second_src_root)
    close_all()

    res2 = hub_importer.import_from_source(
        second_source, "dest", hub_path=hub_path,
    )
    assert res2["success"] is True
    assert res2["imported"] == 0, (
        f"both memories should dedup-skip; got {res2!r}"
    )
    assert res2["skipped"] == 2
    assert res2["edges"] == 1, (
        f"the new edge must follow the dedup remap; got {res2!r}"
    )

    # Confirm the new edge with the m3_review_m2_remap relation
    # actually lives in the destination.
    dest_db = get_project_db_path(dest_root)
    conn = sqlite3.connect(f"file:{dest_db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT source_id, target_id FROM edges WHERE relation = ?",
            ("m3_review_m2_remap",),
        ).fetchone()
        assert row is not None, "remapped edge missing in destination"
    finally:
        conn.close()


def test_importer_edge_refs_resolve(source_and_dest):
    hub_path, source_db, dest_root, src_ids = source_and_dest

    res = hub_importer.import_from_source(source_db, "dest", hub_path=hub_path)
    assert res["success"] is True

    dest_db = get_project_db_path(dest_root)
    conn = sqlite3.connect(f"file:{dest_db}?mode=ro", uri=True)
    try:
        edge = conn.execute("SELECT * FROM edges LIMIT 1").fetchone()
        assert edge is not None
        # source_id and target_id should both resolve in memories.
        s = conn.execute(
            "SELECT id FROM memories WHERE id = ?", (edge[1],),  # source_id
        ).fetchone()
        t = conn.execute(
            "SELECT id FROM memories WHERE id = ?", (edge[2],),  # target_id
        ).fetchone()
        assert s is not None and t is not None, (
            f"edge endpoints don't resolve: source_id={edge[1]}, "
            f"target_id={edge[2]}"
        )
        # And they should match the original source IDs.
        assert edge[1] in src_ids
        assert edge[2] in src_ids
    finally:
        conn.close()
