"""M5 T2 — `sage-memory reindex` CLI tests.

Spec A3-A6 + backup-list/backup-drop.

Tests build a project DB with `override_project_root()` so the CLI's
`get_project_db()` resolves to the tmp fixture.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from sage_memory import cli_reindex
from sage_memory.db import close_all, get_project_db, override_project_root


@pytest.fixture
def project_db(tmp_path, monkeypatch):
    """Initialise a project DB under tmp_path/.sage/memory.db.

    Apply all migrations + insert 2 memories + 3 chunks + meta rows
    so reindex queries have something to operate on.
    """
    project_root = tmp_path / "proj"
    project_root.mkdir()
    sage_dir = project_root / ".sage"
    sage_dir.mkdir()
    override_project_root(project_root)
    db = get_project_db()
    # Insert 2 memories
    now = time.time()
    mem1 = uuid.uuid4().hex
    mem2 = uuid.uuid4().hex
    for mid in (mem1, mem2):
        db.execute(
            "INSERT INTO memories (id, title, content, content_hash, "
            "created_at, updated_at, accessed_at) "
            "VALUES (?, 'T', 'C', ?, ?, ?, ?)",
            (mid, mid, now, now, now),
        )
    # Insert 3 chunks (2 on mem1, 1 on mem2)
    chunks = []
    for mid, idx_count in [(mem1, 2), (mem2, 1)]:
        for i in range(idx_count):
            cid = uuid.uuid4().hex
            chunks.append((cid, mid))
            db.execute(
                "INSERT INTO chunks (id, memory_id, chunk_index, "
                "content, byte_start, byte_end, created_at) "
                "VALUES (?, ?, ?, 'C', 0, 1, ?)",
                (cid, mid, i, now),
            )
    # Insert meta rows for memories (dim=384, current corpus dim)
    for mid in (mem1, mem2):
        db.execute(
            "INSERT INTO memory_embedding_meta "
            "(memory_id, model_name, model_version, dim, created_at) "
            "VALUES (?, 'local', 'v1', 384, ?)",
            (mid, now),
        )
    for cid, _mid in chunks:
        db.execute(
            "INSERT INTO chunk_embedding_meta "
            "(chunk_id, model_name, model_version, dim, created_at) "
            "VALUES (?, 'local', 'v1', 384, ?)",
            (cid, now),
        )
    db.commit()
    yield {"db": db, "mem1": mem1, "mem2": mem2, "chunks": chunks}
    close_all()
    override_project_root(None)


# ─── A3: --re-embed --embedder ────────────────────────────────────


def test_reindex_full_reembed_drops_recreates_queues(project_db):
    """A3: full reindex backs up + recreates vec tables + updates
    corpus_meta + queues reembed tasks for all memories and chunks."""
    db = project_db["db"]
    rc = cli_reindex.run_reindex(["--re-embed", "--embedder", "openai"])
    assert rc == 0

    # corpus_meta updated
    row = db.execute(
        "SELECT value FROM corpus_meta WHERE key = 'vec_dim'"
    ).fetchone()
    assert row["value"] == "1536"

    # 2 memories + 3 chunks = 5 reembed tasks
    tasks = db.execute(
        "SELECT COUNT(*) AS n FROM extraction_queue "
        "WHERE task_type = 'reembed' AND status = 'pending'"
    ).fetchone()
    assert tasks["n"] == 5

    # Backup tables exist (filter out sqlite-vec shadow tables —
    # vec0 creates *_info / *_chunks / *_rowids / *_vector_chunks00
    # alongside the main name).
    backup_tables = [
        r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE (name LIKE 'memories_vec_backup_%' "
            "       OR name LIKE 'chunks_vec_backup_%') "
            "  AND name NOT LIKE '%_info' "
            "  AND name NOT LIKE '%_chunks' "
            "  AND name NOT LIKE '%_rowids' "
            "  AND name NOT LIKE '%_vector_chunks%'"
        )
    ]
    assert len(backup_tables) == 2  # memories + chunks


# ─── A4: --embeddings (partial reindex) ───────────────────────────


def test_reindex_embeddings_queues_only_stale_rows(project_db):
    """A4: only rows whose meta-dim != corpus_dim get queued.

    Set one memory's meta to dim=1024 (stale vs corpus 384); the other
    matches → only the stale one gets queued."""
    db = project_db["db"]
    mem1 = project_db["mem1"]
    db.execute(
        "UPDATE memory_embedding_meta SET dim = 1024 WHERE memory_id = ?",
        (mem1,),
    )
    db.commit()

    rc = cli_reindex.run_reindex(["--embeddings"])
    assert rc == 0

    queued = db.execute(
        "SELECT memory_id FROM extraction_queue "
        "WHERE task_type = 'reembed' AND status = 'pending'"
    ).fetchall()
    queued_ids = {r["memory_id"] for r in queued}
    assert mem1 in queued_ids
    # mem2 is current → not queued
    assert project_db["mem2"] not in queued_ids


def test_reindex_embeddings_clean_state_exits_zero(project_db, capsys):
    """A4: when all rows match corpus_meta, exits 0 with 'nothing
    to do' message + no queue inserts."""
    db = project_db["db"]
    pre = db.execute(
        "SELECT COUNT(*) AS n FROM extraction_queue"
    ).fetchone()["n"]

    rc = cli_reindex.run_reindex(["--embeddings"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "nothing to do" in captured.out.lower()

    post = db.execute(
        "SELECT COUNT(*) AS n FROM extraction_queue"
    ).fetchone()["n"]
    assert post == pre


# ─── A5: --memory-id ──────────────────────────────────────────────


def test_reindex_memory_id_single_row(project_db):
    """A5: --memory-id queues exactly that memory + its chunks."""
    db = project_db["db"]
    mem1 = project_db["mem1"]
    # mem1 has 2 chunks → 1 memory + 2 chunks = 3 tasks
    rc = cli_reindex.run_reindex(["--memory-id", mem1])
    assert rc == 0

    queued = db.execute(
        "SELECT COUNT(*) AS n FROM extraction_queue "
        "WHERE task_type = 'reembed'"
    ).fetchone()["n"]
    assert queued == 3


def test_reindex_memory_id_overrides_embeddings_filter(project_db):
    """A5: --memory-id with --embeddings queues that memory even if
    its meta matches corpus_dim (explicit ID wins)."""
    db = project_db["db"]
    mem1 = project_db["mem1"]
    # mem1's meta matches corpus (384=384) → normally NOT queued by
    # --embeddings, but --memory-id overrides.
    rc = cli_reindex.run_reindex(
        ["--embeddings", "--memory-id", mem1]
    )
    assert rc == 0

    queued = db.execute(
        "SELECT memory_id FROM extraction_queue "
        "WHERE task_type = 'reembed'"
    ).fetchall()
    queued_ids = {r["memory_id"] for r in queued}
    assert mem1 in queued_ids


def test_reindex_memory_id_composes_with_re_embed(project_db):
    """rev1-review minor #10: --re-embed --memory-id performs full
    backup+drop+recreate AND queues only that memory's reembed."""
    db = project_db["db"]
    mem1 = project_db["mem1"]
    rc = cli_reindex.run_reindex(
        ["--re-embed", "--embedder", "voyage", "--memory-id", mem1]
    )
    assert rc == 0

    # corpus_meta WAS updated (full backup+drop+recreate happened)
    row = db.execute(
        "SELECT value FROM corpus_meta WHERE key = 'vec_dim'"
    ).fetchone()
    assert row["value"] == "512"  # voyage tier

    # Only mem1 + its 2 chunks queued (not mem2)
    queued = db.execute(
        "SELECT DISTINCT memory_id FROM extraction_queue "
        "WHERE task_type = 'reembed' AND status = 'pending'"
    ).fetchall()
    queued_ids = {r["memory_id"] for r in queued}
    assert queued_ids == {mem1}, (
        f"--re-embed --memory-id should only queue mem1; got {queued_ids}"
    )


# ─── A6: --limit ──────────────────────────────────────────────────


def test_reindex_limit_caps_queue(project_db):
    """A6: --limit N caps total queued items at N."""
    db = project_db["db"]
    # Force 5 stale rows by mutating meta dims
    db.execute("UPDATE memory_embedding_meta SET dim = 999")
    db.execute("UPDATE chunk_embedding_meta SET dim = 999")
    db.commit()

    rc = cli_reindex.run_reindex(["--embeddings", "--limit", "2"])
    assert rc == 0

    queued = db.execute(
        "SELECT COUNT(*) AS n FROM extraction_queue "
        "WHERE task_type = 'reembed' AND status = 'pending'"
    ).fetchone()["n"]
    assert queued == 2


# ─── backup-list / backup-drop ────────────────────────────────────


def test_reindex_backup_list(project_db, capsys):
    """backup-list prints all backup tables with row counts."""
    db = project_db["db"]
    # Manually create backup tables for test
    db.execute(
        "CREATE VIRTUAL TABLE memories_vec_backup_20260517_120000 "
        "USING vec0(memory_id TEXT PRIMARY KEY, embedding float[384])"
    )
    db.execute(
        "CREATE VIRTUAL TABLE chunks_vec_backup_20260517_120000 "
        "USING vec0(chunk_id TEXT PRIMARY KEY, embedding float[384])"
    )
    db.commit()

    rc = cli_reindex.run_reindex(["backup-list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "20260517_120000" in out


def test_reindex_backup_drop_idempotent(project_db, capsys):
    """backup-drop removes both vec tables; re-running prints
    'no backup' and exits 0."""
    db = project_db["db"]
    db.execute(
        "CREATE VIRTUAL TABLE memories_vec_backup_20260517_120000 "
        "USING vec0(memory_id TEXT PRIMARY KEY, embedding float[384])"
    )
    db.execute(
        "CREATE VIRTUAL TABLE chunks_vec_backup_20260517_120000 "
        "USING vec0(chunk_id TEXT PRIMARY KEY, embedding float[384])"
    )
    db.commit()

    rc = cli_reindex.run_reindex(
        ["backup-drop", "20260517_120000"]
    )
    assert rc == 0
    assert "dropped" in capsys.readouterr().out.lower()

    # Second run: idempotent
    rc = cli_reindex.run_reindex(
        ["backup-drop", "20260517_120000"]
    )
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "no backup" in out


def test_reindex_more_than_5_backups_warns(
    project_db, capsys,
):
    """Pre-run check: if >5 existing backups, emit WARNING (does NOT
    block)."""
    db = project_db["db"]
    # Create 6 backup sets
    for hour in range(6):
        ts = f"20260517_1{hour}0000"
        db.execute(
            f"CREATE VIRTUAL TABLE memories_vec_backup_{ts} "
            f"USING vec0(memory_id TEXT PRIMARY KEY, embedding float[384])"
        )
        db.execute(
            f"CREATE VIRTUAL TABLE chunks_vec_backup_{ts} "
            f"USING vec0(chunk_id TEXT PRIMARY KEY, embedding float[384])"
        )
    db.commit()

    rc = cli_reindex.run_reindex(["--re-embed", "--embedder", "local"])
    assert rc == 0  # Warning is non-blocking
    err = capsys.readouterr().err.lower()
    assert "warning" in err
    assert "6" in err
