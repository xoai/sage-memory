"""M5 T0 — Migration 008 + Worker maybe_prune() tests.

Migration 008 does TWO things atomically:
  Part A: adds `worker_state` singleton table (last_prune_at REAL).
  Part B: relaxes extraction_queue.memory_id NOT NULL (rev1-review
          CRITICAL #1 — required so dedup tasks with NULL memory_id
          can be INSERTed).

Plus: Worker.maybe_prune() reads + updates worker_state.last_prune_at
to enforce the 30-day prune retention contract from ADR-001 + ADR-003
no more than once per 24h.
"""

from __future__ import annotations

import sqlite3
import time
import uuid

import pytest

from sage_memory.db import _migrate
from sage_memory.worker import Worker


def _through_006(fresh_db, tmp_migrations_dir, copy_production_migrations):
    """Helper: apply 001-006 (everything pre-007) to a fresh DB."""
    copy_production_migrations(
        "001_initial.sql", "002_edges.sql", "003_memory_health.sql", "004_chunks.sql",
        "005_entities.sql", "006_embedding_meta.sql",
        "007_extraction_queue.sql",
    )
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)


def _through_007(fresh_db, tmp_migrations_dir, copy_production_migrations):
    """Helper: apply 001-007."""
    copy_production_migrations(
        "001_initial.sql", "002_edges.sql", "003_memory_health.sql", "004_chunks.sql",
        "005_entities.sql", "006_embedding_meta.sql",
        "007_extraction_queue.sql", "008_worker_state.sql",
    )
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)


# ─── Migration 008 schema tests ───────────────────────────────────


def test_migration_007_creates_worker_state_table(
    fresh_db, tmp_migrations_dir, copy_production_migrations,
):
    """007 Part A: worker_state singleton table with NULL initial state."""
    _through_007(fresh_db, tmp_migrations_dir, copy_production_migrations)

    tables = {row[0] for row in fresh_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "worker_state" in tables

    rows = fresh_db.execute("SELECT id, last_prune_at FROM worker_state").fetchall()
    assert len(rows) == 1, "worker_state must be a singleton (CHECK id=1)"
    assert rows[0]["id"] == 1
    assert rows[0]["last_prune_at"] is None


def test_migration_007_relaxes_extraction_queue_memory_id(
    fresh_db, tmp_migrations_dir, copy_production_migrations,
):
    """007 Part B: extraction_queue.memory_id NOT NULL is relaxed so
    dedup tasks (memory_id=NULL) can be INSERTed. Rev1-review CRIT #1.

    Verifies the fix by:
      (a) NULL-INSERT on a 006-only fresh DB FAILS with IntegrityError.
      (b) NULL-INSERT on a 007-applied fresh DB SUCCEEDS.
    """
    # (a) 006-only fresh DB: NULL memory_id violates NOT NULL.
    _through_006(fresh_db, tmp_migrations_dir, copy_production_migrations)
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.execute(
            "INSERT INTO extraction_queue "
            "(id, memory_id, task_type, status, created_at) "
            "VALUES (?, NULL, 'dedup', 'pending', ?)",
            (uuid.uuid4().hex, time.time()),
        )
    fresh_db.rollback()


def test_migration_007_null_memory_id_insert_succeeds_post_007(
    fresh_db, tmp_migrations_dir, copy_production_migrations,
):
    """Companion to the relax test: post-007, NULL memory_id works."""
    _through_007(fresh_db, tmp_migrations_dir, copy_production_migrations)
    fresh_db.execute(
        "INSERT INTO extraction_queue "
        "(id, memory_id, task_type, status, created_at) "
        "VALUES (?, NULL, 'dedup', 'pending', ?)",
        (uuid.uuid4().hex, time.time()),
    )
    fresh_db.commit()
    rows = fresh_db.execute(
        "SELECT memory_id, task_type FROM extraction_queue"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["memory_id"] is None
    assert rows[0]["task_type"] == "dedup"


def test_migration_007_preserves_existing_extraction_queue_rows(
    fresh_db, tmp_migrations_dir, copy_production_migrations,
):
    """Part B rebuild pattern must NOT lose any pre-existing rows.

    Apply 001-006; insert 3 rows with non-NULL memory_id; apply 007;
    assert all 3 rows survive.
    """
    _through_006(fresh_db, tmp_migrations_dir, copy_production_migrations)

    # Insert a parent memory (FK to memories table from 001).
    now = time.time()
    mid = uuid.uuid4().hex
    fresh_db.execute(
        "INSERT INTO memories (id, title, content, content_hash, "
        "created_at, updated_at, accessed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (mid, "t", "c", mid, now, now, now),
    )

    pre_ids = []
    for _ in range(3):
        tid = uuid.uuid4().hex
        pre_ids.append(tid)
        fresh_db.execute(
            "INSERT INTO extraction_queue "
            "(id, memory_id, task_type, status, attempts, created_at) "
            "VALUES (?, ?, 'extract', 'pending', 0, ?)",
            (tid, mid, now),
        )
    fresh_db.commit()

    # Now apply 007 (carries the rebuild).
    copy_production_migrations("008_worker_state.sql")
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)

    post_ids = [
        row["id"] for row in fresh_db.execute(
            "SELECT id FROM extraction_queue ORDER BY id"
        )
    ]
    assert set(post_ids) == set(pre_ids), (
        "Migration 008 Part B rebuild lost rows"
    )


def test_migration_007_idempotent(
    fresh_db, tmp_migrations_dir, copy_production_migrations,
):
    """Applying 007 twice produces no errors + identical observable state."""
    _through_007(fresh_db, tmp_migrations_dir, copy_production_migrations)
    # Apply again (production runner skips by user_version; we exercise
    # the raw SQL via a second _migrate call on the same dir).
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)

    # worker_state still singleton
    ws_rows = fresh_db.execute("SELECT * FROM worker_state").fetchall()
    assert len(ws_rows) == 1

    # extraction_queue still functional + NULL-permitting
    fresh_db.execute(
        "INSERT INTO extraction_queue "
        "(id, memory_id, task_type, status, created_at) "
        "VALUES (?, NULL, 'dedup', 'pending', ?)",
        (uuid.uuid4().hex, time.time()),
    )
    fresh_db.commit()


# ─── Worker.maybe_prune() lifecycle tests ─────────────────────────


def _insert_done_task(conn, *, processed_at):
    """Helper: insert a 'done' task with given processed_at."""
    conn.execute(
        "INSERT INTO extraction_queue "
        "(id, memory_id, task_type, status, attempts, "
        " created_at, processed_at) "
        "VALUES (?, NULL, 'dedup', 'done', 1, ?, ?)",
        (uuid.uuid4().hex, processed_at - 1, processed_at),
    )


@pytest.fixture
def worker_db_through_007(
    tmp_path, fresh_db, tmp_migrations_dir, copy_production_migrations,
):
    """A migrated DB file (path) ready for Worker + maybe_prune tests."""
    _through_007(fresh_db, tmp_migrations_dir, copy_production_migrations)
    fresh_db.commit()
    fresh_db.close()
    # Worker opens its own connection on the same file.
    db_file = next(tmp_path.glob("*.db"))
    return db_file


def test_maybe_prune_first_run_reads_null_state_and_updates(
    worker_db_through_007,
):
    """First run: last_prune_at IS NULL → prune runs, 5 aged-31-day
    rows deleted, last_prune_at set to ≈ now."""
    conn = sqlite3.connect(str(worker_db_through_007))
    conn.row_factory = sqlite3.Row
    try:
        now = time.time()
        # Insert 5 done tasks aged 31 days
        for _ in range(5):
            _insert_done_task(conn, processed_at=now - 31 * 86400)
        conn.commit()

        # Confirm pre-state
        pre_state = conn.execute(
            "SELECT last_prune_at FROM worker_state"
        ).fetchone()
        assert pre_state["last_prune_at"] is None
        assert conn.execute(
            "SELECT COUNT(*) FROM extraction_queue"
        ).fetchone()[0] == 5
    finally:
        conn.close()

    worker = Worker(str(worker_db_through_007))
    did_run = worker.maybe_prune()
    assert did_run is True

    conn = sqlite3.connect(str(worker_db_through_007))
    conn.row_factory = sqlite3.Row
    try:
        post_state = conn.execute(
            "SELECT last_prune_at FROM worker_state"
        ).fetchone()
        assert post_state["last_prune_at"] is not None
        # Within 5 seconds of now (clock skew tolerance)
        assert abs(post_state["last_prune_at"] - time.time()) < 5.0
        remaining = conn.execute(
            "SELECT COUNT(*) FROM extraction_queue"
        ).fetchone()[0]
        assert remaining == 0, "all 5 aged rows should be deleted"
    finally:
        conn.close()


def test_maybe_prune_skips_within_24h_window_reads_state(
    worker_db_through_007,
):
    """last_prune_at = now - 3600 (1 hour ago) → maybe_prune is a no-op."""
    conn = sqlite3.connect(str(worker_db_through_007))
    conn.row_factory = sqlite3.Row
    try:
        now = time.time()
        # Set recent prune timestamp
        conn.execute(
            "UPDATE worker_state SET last_prune_at = ? WHERE id = 1",
            (now - 3600,),
        )
        # Insert an aged row that WOULD be pruned if prune ran
        _insert_done_task(conn, processed_at=now - 31 * 86400)
        conn.commit()
    finally:
        conn.close()

    worker = Worker(str(worker_db_through_007))
    did_run = worker.maybe_prune()
    assert did_run is False, "within-24h window must skip"

    conn = sqlite3.connect(str(worker_db_through_007))
    conn.row_factory = sqlite3.Row
    try:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM extraction_queue"
        ).fetchone()[0]
        assert remaining == 1, "skipped prune must not delete"
        # last_prune_at unchanged (within tolerance)
        state = conn.execute(
            "SELECT last_prune_at FROM worker_state"
        ).fetchone()
        assert abs(state["last_prune_at"] - (time.time() - 3600)) < 5.0
    finally:
        conn.close()


def test_maybe_prune_runs_after_24h_window(worker_db_through_007):
    """last_prune_at = now - 90000 (25 hours ago) → prune runs."""
    conn = sqlite3.connect(str(worker_db_through_007))
    conn.row_factory = sqlite3.Row
    try:
        now = time.time()
        conn.execute(
            "UPDATE worker_state SET last_prune_at = ? WHERE id = 1",
            (now - 90000,),
        )
        for _ in range(3):
            _insert_done_task(conn, processed_at=now - 31 * 86400)
        conn.commit()
    finally:
        conn.close()

    worker = Worker(str(worker_db_through_007))
    did_run = worker.maybe_prune()
    assert did_run is True

    conn = sqlite3.connect(str(worker_db_through_007))
    conn.row_factory = sqlite3.Row
    try:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM extraction_queue"
        ).fetchone()[0]
        assert remaining == 0
        state = conn.execute(
            "SELECT last_prune_at FROM worker_state"
        ).fetchone()
        assert abs(state["last_prune_at"] - time.time()) < 5.0
    finally:
        conn.close()


def test_maybe_prune_retains_recent_rows(worker_db_through_007):
    """First-run maybe_prune executes but the 30-day retention floor
    protects rows aged 29 days. last_prune_at is still updated
    (the prune ran; it just deleted 0 rows)."""
    conn = sqlite3.connect(str(worker_db_through_007))
    conn.row_factory = sqlite3.Row
    try:
        now = time.time()
        for _ in range(5):
            _insert_done_task(conn, processed_at=now - 29 * 86400)
        conn.commit()
    finally:
        conn.close()

    worker = Worker(str(worker_db_through_007))
    did_run = worker.maybe_prune()
    assert did_run is True

    conn = sqlite3.connect(str(worker_db_through_007))
    conn.row_factory = sqlite3.Row
    try:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM extraction_queue"
        ).fetchone()[0]
        assert remaining == 5, "29-day rows are under 30-day floor"
        state = conn.execute(
            "SELECT last_prune_at FROM worker_state"
        ).fetchone()
        assert state["last_prune_at"] is not None
    finally:
        conn.close()
