"""M5 T2 — `sage-memory reindex` CLI subcommand.

Four modes per spec A3-A6:
  --re-embed --embedder <name>  Full reindex: backup + recreate
                                memories_vec/chunks_vec with new dim,
                                update corpus_meta.vec_dim, queue
                                reembed tasks.
  --embeddings                  Partial reindex: queue reembed only
                                for rows whose meta-dim != corpus dim.
  --memory-id <id>              Single memory + its chunks.
                                Composes with --re-embed and
                                --embeddings.
  --limit <N>                   Cap queue at N items.

Plus two backup-table maintenance subcommands per spec
§"Backup-table lifecycle":
  backup-list                   Print existing *_backup_<ts> tables.
  backup-drop <timestamp>       Idempotent drop.

Argparse-free dispatch (matches cli_worker.py pattern).
"""

from __future__ import annotations

import datetime
import logging
import sys
import uuid

from .db import get_project_db


logger = logging.getLogger("sage_memory.cli_reindex")


# Tier name → native dim. Authoritative mapping for `--embedder`.
_TIER_DIMS = {
    "local": 384,
    "fastembed": 384,
    "openai": 1536,
    "voyage": 512,
    "cohere": 1024,
}

_BACKUP_WARN_THRESHOLD = 5


_HELP_TEXT = """\
sage-memory reindex — corpus + embedding reindex operations

Usage:
  sage-memory reindex --re-embed --embedder <name>
      Full reindex: backup memories_vec + chunks_vec, recreate with
      the new embedder's native dim, update corpus_meta.vec_dim,
      queue reembed tasks for every memory + chunk.

  sage-memory reindex --embeddings
      Partial reindex: queue reembed only for rows whose
      memory_embedding_meta.dim != corpus_meta.vec_dim. Used to
      finish a previously-interrupted reindex.

  sage-memory reindex --memory-id <id> [--re-embed --embedder <name>]
      Reindex a single memory + its chunks. Composes with --re-embed
      and --embeddings.

  sage-memory reindex --limit <N>
      Cap the queued items at N. Applies to --re-embed and
      --embeddings; no-op on --memory-id.

  sage-memory reindex backup-list
      List *_backup_<timestamp> tables with row counts.

  sage-memory reindex backup-drop <timestamp>
      Drop both memories_vec_backup_<timestamp> and chunks_vec_backup
      _<timestamp>. Idempotent.

Embedder names: local, fastembed (both 384d), openai (1536d),
                voyage (512d), cohere (1024d).
"""


def run_reindex(argv: list[str]) -> int:
    """Entry point dispatched from `__init__.py:main()`. Returns exit
    code."""
    if not argv or argv[0] in ("-h", "--help"):
        print(_HELP_TEXT)
        return 0

    if argv[0] == "backup-list":
        return _backup_list()
    if argv[0] == "backup-drop":
        if len(argv) < 2:
            print(
                "sage-memory reindex backup-drop: missing <timestamp>\n",
                file=sys.stderr,
            )
            print(_HELP_TEXT, file=sys.stderr)
            return 2
        return _backup_drop(argv[1])

    flags = _parse_flags(argv)
    if flags is None:
        return 2

    if flags.re_embed:
        return _do_full_reembed(
            embedder_name=flags.embedder,
            memory_id=flags.memory_id,
            limit=flags.limit,
        )
    if flags.embeddings:
        return _do_partial_reembed(
            memory_id=flags.memory_id, limit=flags.limit,
        )
    if flags.memory_id:
        return _do_single_memory(memory_id=flags.memory_id)

    print(
        "sage-memory reindex: must specify a mode "
        "(--re-embed --embedder, --embeddings, --memory-id)\n",
        file=sys.stderr,
    )
    print(_HELP_TEXT, file=sys.stderr)
    return 2


# ─── Flag parsing (argparse-free) ─────────────────────────────────


class _Flags:
    re_embed: bool = False
    embedder: str | None = None
    embeddings: bool = False
    memory_id: str | None = None
    limit: int | None = None


def _parse_flags(argv: list[str]) -> _Flags | None:
    flags = _Flags()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--re-embed":
            flags.re_embed = True
            i += 1
        elif a == "--embedder":
            if i + 1 >= len(argv):
                print("--embedder requires a value\n", file=sys.stderr)
                print(_HELP_TEXT, file=sys.stderr)
                return None
            flags.embedder = argv[i + 1]
            i += 2
        elif a == "--embeddings":
            flags.embeddings = True
            i += 1
        elif a == "--memory-id":
            if i + 1 >= len(argv):
                print("--memory-id requires a value\n", file=sys.stderr)
                print(_HELP_TEXT, file=sys.stderr)
                return None
            flags.memory_id = argv[i + 1]
            i += 2
        elif a == "--limit":
            if i + 1 >= len(argv):
                print("--limit requires a value\n", file=sys.stderr)
                print(_HELP_TEXT, file=sys.stderr)
                return None
            try:
                flags.limit = int(argv[i + 1])
            except ValueError:
                print(
                    f"--limit must be an integer (got {argv[i + 1]!r})\n",
                    file=sys.stderr,
                )
                return None
            i += 2
        else:
            print(
                f"sage-memory reindex: unknown flag: {a}\n",
                file=sys.stderr,
            )
            print(_HELP_TEXT, file=sys.stderr)
            return None
    if flags.re_embed and not flags.embedder:
        print("--re-embed requires --embedder <name>\n", file=sys.stderr)
        print(_HELP_TEXT, file=sys.stderr)
        return None
    return flags


# ─── Mode implementations ─────────────────────────────────────────


def _ts_for_backup() -> str:
    """UTC timestamp for backup-table suffix. Per spec
    §"Backup-table lifecycle"."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S")


def _existing_backups(db) -> list[str]:
    """Return list of backup timestamps (sorted)."""
    rows = db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type IN ('table', 'view') "
        "  AND (name LIKE 'memories_vec_backup_%' "
        "       OR name LIKE 'chunks_vec_backup_%') "
        "ORDER BY name"
    ).fetchall()
    timestamps: set[str] = set()
    for r in rows:
        name = r["name"]
        if name.startswith("memories_vec_backup_"):
            timestamps.add(name[len("memories_vec_backup_"):])
        elif name.startswith("chunks_vec_backup_"):
            timestamps.add(name[len("chunks_vec_backup_"):])
    return sorted(timestamps)


def _do_full_reembed(
    *, embedder_name: str, memory_id: str | None, limit: int | None,
) -> int:
    if embedder_name not in _TIER_DIMS:
        print(
            f"sage-memory reindex: unknown embedder {embedder_name!r}; "
            f"valid: {sorted(_TIER_DIMS)}\n",
            file=sys.stderr,
        )
        return 2
    new_dim = _TIER_DIMS[embedder_name]
    db = get_project_db()
    if db is None:
        print(
            "sage-memory reindex: no project DB found "
            "(run inside a project root)\n",
            file=sys.stderr,
        )
        return 2

    # Warn if too many existing backups.
    backups = _existing_backups(db)
    if len(backups) > _BACKUP_WARN_THRESHOLD:
        print(
            f"warning: {len(backups)} existing backup table sets "
            f"({backups}). Consider `sage-memory reindex backup-drop`.",
            file=sys.stderr,
        )

    ts = _ts_for_backup()
    # Conflict handling: append _2, _3 if same-second collision.
    candidate = ts
    suffix = 2
    while candidate in backups:
        candidate = f"{ts}_{suffix}"
        suffix += 1
    ts = candidate

    db.execute("BEGIN IMMEDIATE")
    try:
        # Backup via CREATE-new + INSERT-SELECT (RENAME on virtual
        # tables is not reliable across sqlite-vec versions).
        for table in ("memories", "chunks"):
            src = f"{table}_vec"
            dst = f"{table}_vec_backup_{ts}"
            id_col = "memory_id" if table == "memories" else "chunk_id"
            # Probe original dim for the backup table.
            try:
                src_dim_row = db.execute(
                    f"SELECT embedding FROM {src} LIMIT 1"
                ).fetchone()
                src_dim = (
                    len(src_dim_row["embedding"]) // 4  # float32 bytes
                    if src_dim_row else _current_dim(db)
                )
            except Exception:
                src_dim = _current_dim(db)
            db.execute(
                f"CREATE VIRTUAL TABLE {dst} USING vec0("
                f"{id_col} TEXT PRIMARY KEY, embedding float[{src_dim}])"
            )
            db.execute(
                f"INSERT INTO {dst} ({id_col}, embedding) "
                f"SELECT {id_col}, embedding FROM {src}"
            )
            db.execute(f"DROP TABLE {src}")
            db.execute(
                f"CREATE VIRTUAL TABLE {src} USING vec0("
                f"{id_col} TEXT PRIMARY KEY, embedding float[{new_dim}])"
            )
        # Update corpus_meta atomically.
        db.execute(
            "UPDATE corpus_meta SET value = ? WHERE key = 'vec_dim'",
            (str(new_dim),),
        )
        # Queue reembed tasks.
        if memory_id is not None:
            mem_ids = [memory_id]
        else:
            mem_ids = [
                r["id"] for r in db.execute(
                    "SELECT id FROM memories"
                ).fetchall()
            ]
        chunk_ids = []
        if memory_id is not None:
            chunk_ids = [
                r["id"] for r in db.execute(
                    "SELECT id FROM chunks WHERE memory_id = ?",
                    (memory_id,),
                ).fetchall()
            ]
        else:
            chunk_ids = [
                r["id"] for r in db.execute(
                    "SELECT id FROM chunks"
                ).fetchall()
            ]
        queued = _enqueue_reembed_tasks(db, mem_ids, chunk_ids, limit)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    print(
        f"reindex --re-embed --embedder {embedder_name}: "
        f"backup={ts}, queued {queued} reembed task(s). "
        f"Run `sage-memory worker --status` to monitor."
    )
    return 0


def _current_dim(db) -> int:
    row = db.execute(
        "SELECT value FROM corpus_meta WHERE key = 'vec_dim'"
    ).fetchone()
    return int(row["value"]) if row else 384


def _do_partial_reembed(
    *, memory_id: str | None, limit: int | None,
) -> int:
    db = get_project_db()
    if db is None:
        print(
            "sage-memory reindex: no project DB found\n", file=sys.stderr,
        )
        return 2
    corpus_dim = _current_dim(db)

    # Find stale memories: meta missing OR meta.dim != corpus_dim.
    if memory_id is not None:
        # --memory-id overrides the meta-dim filter (always queues).
        mem_ids = [memory_id]
        chunk_ids = [
            r["id"] for r in db.execute(
                "SELECT id FROM chunks WHERE memory_id = ?",
                (memory_id,),
            ).fetchall()
        ]
    else:
        mem_ids = [
            r["id"] for r in db.execute(
                "SELECT m.id FROM memories m "
                "LEFT JOIN memory_embedding_meta mm "
                "  ON m.id = mm.memory_id "
                "WHERE mm.dim IS NULL OR mm.dim != ?",
                (corpus_dim,),
            ).fetchall()
        ]
        chunk_ids = [
            r["id"] for r in db.execute(
                "SELECT c.id FROM chunks c "
                "LEFT JOIN chunk_embedding_meta cm "
                "  ON c.id = cm.chunk_id "
                "WHERE cm.dim IS NULL OR cm.dim != ?",
                (corpus_dim,),
            ).fetchall()
        ]

    if not mem_ids and not chunk_ids:
        print(
            "reindex --embeddings: nothing to do — "
            f"all rows match corpus_meta.vec_dim={corpus_dim}. Exiting."
        )
        return 0

    queued = _enqueue_reembed_tasks(db, mem_ids, chunk_ids, limit)
    db.commit()
    print(
        f"reindex --embeddings: queued {queued} reembed task(s) "
        f"for stale rows."
    )
    return 0


def _do_single_memory(*, memory_id: str) -> int:
    """`--memory-id` without `--re-embed` or `--embeddings`: forces
    reembed of that one memory + its chunks."""
    db = get_project_db()
    if db is None:
        print(
            "sage-memory reindex: no project DB found\n", file=sys.stderr,
        )
        return 2
    mem_ids = [memory_id]
    chunk_ids = [
        r["id"] for r in db.execute(
            "SELECT id FROM chunks WHERE memory_id = ?", (memory_id,),
        ).fetchall()
    ]
    queued = _enqueue_reembed_tasks(db, mem_ids, chunk_ids, None)
    db.commit()
    print(
        f"reindex --memory-id {memory_id}: queued {queued} reembed task(s)."
    )
    return 0


def _enqueue_reembed_tasks(
    db, mem_ids: list[str], chunk_ids: list[str], limit: int | None,
) -> int:
    """Insert one `reembed` task per memory_id and per chunk_id.
    Returns count of rows inserted. `limit` (if set) caps the total."""
    now_unix = "unixepoch()"
    inserted = 0
    items = [(mid, mid) for mid in mem_ids] + [
        (cid, cid) for cid in chunk_ids  # for chunks we still write
                                          # memory_id field via lookup
    ]
    # Look up chunk → memory_id mapping in one query (so the FK
    # constraint isn't violated).
    chunk_to_mem: dict[str, str] = {}
    if chunk_ids:
        placeholders = ",".join("?" * len(chunk_ids))
        for r in db.execute(
            f"SELECT id, memory_id FROM chunks WHERE id IN ({placeholders})",
            chunk_ids,
        ).fetchall():
            chunk_to_mem[r["id"]] = r["memory_id"]

    # Queue memory-level tasks (memory_id = the memory itself).
    for mid in mem_ids:
        if limit is not None and inserted >= limit:
            break
        db.execute(
            "INSERT INTO extraction_queue "
            f"(id, memory_id, task_type, status, attempts, created_at) "
            f"VALUES (?, ?, 'reembed', 'pending', 0, {now_unix})",
            (uuid.uuid4().hex, mid),
        )
        inserted += 1
    # Queue chunk-level tasks (memory_id = parent memory).
    for cid in chunk_ids:
        if limit is not None and inserted >= limit:
            break
        parent = chunk_to_mem.get(cid)
        db.execute(
            "INSERT INTO extraction_queue "
            f"(id, memory_id, task_type, status, attempts, created_at) "
            f"VALUES (?, ?, 'reembed', 'pending', 0, {now_unix})",
            (uuid.uuid4().hex, parent),
        )
        inserted += 1
    return inserted


# ─── backup-list / backup-drop subcommands ────────────────────────


def _backup_list() -> int:
    db = get_project_db()
    if db is None:
        print(
            "sage-memory reindex: no project DB found\n", file=sys.stderr,
        )
        return 2
    backups = _existing_backups(db)
    if not backups:
        print("(no backup tables)")
        return 0
    print(f"{'timestamp':24} {'memories_rows':>14} {'chunks_rows':>13}")
    print("─" * 60)
    for ts in backups:
        mem_count = 0
        chunk_count = 0
        try:
            mem_count = db.execute(
                f"SELECT COUNT(*) AS n FROM memories_vec_backup_{ts}"
            ).fetchone()["n"]
        except Exception:
            mem_count = -1
        try:
            chunk_count = db.execute(
                f"SELECT COUNT(*) AS n FROM chunks_vec_backup_{ts}"
            ).fetchone()["n"]
        except Exception:
            chunk_count = -1
        print(f"{ts:24} {mem_count:>14} {chunk_count:>13}")
    return 0


def _backup_drop(timestamp: str) -> int:
    db = get_project_db()
    if db is None:
        print(
            "sage-memory reindex: no project DB found\n", file=sys.stderr,
        )
        return 2
    dropped_any = False
    for table in (
        f"memories_vec_backup_{timestamp}",
        f"chunks_vec_backup_{timestamp}",
    ):
        try:
            db.execute(f"DROP TABLE {table}")
            dropped_any = True
        except Exception:
            pass  # idempotent — table didn't exist
    db.commit()
    if dropped_any:
        print(f"dropped backup tables for timestamp {timestamp}")
    else:
        print(f"no backup tables found for timestamp {timestamp}")
    return 0
