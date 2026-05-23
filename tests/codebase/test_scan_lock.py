"""T10b — advisory scan-lock primitives.

Covers ``acquire_scan_lock`` / ``release_scan_lock`` directly against
a migrated DB (no CLI / no subprocess) plus stale-row recovery. The
deterministic CLI-against-locked-DB test lives in
``test_cli_scan.py`` so it shares the CLI test infrastructure.
"""

from __future__ import annotations

import os
import time

import pytest

from sage_memory.codebase._lock import (
    acquire_scan_lock,
    release_scan_lock,
    _LOCK_NAME,
    _STALE_THRESHOLD_S,
)
from sage_memory.db import _migrate


ALL_MIGRATIONS = (
    "001_initial.sql",
    "002_edges.sql",
    "003_memory_health.sql",
    "004_chunks.sql",
    "005_entities.sql",
    "006_embedding_meta.sql",
    "007_extraction_queue.sql",
    "008_worker_state.sql",
    "009_code_symbols.sql",
)


@pytest.fixture
def migrated_db(fresh_db, tmp_migrations_dir, copy_production_migrations):
    copy_production_migrations(*ALL_MIGRATIONS)
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    return fresh_db


# ---------------------------------------------------------------------------
# Acquire / release
# ---------------------------------------------------------------------------


def test_acquire_then_release_succeeds(migrated_db) -> None:
    assert acquire_scan_lock(migrated_db) is True
    # Lock row landed with this process's pid.
    row = migrated_db.execute(
        "SELECT lock_name, pid FROM scan_locks WHERE lock_name = ?",
        (_LOCK_NAME,),
    ).fetchone()
    assert row is not None
    assert row["pid"] == os.getpid()

    release_scan_lock(migrated_db)
    assert migrated_db.execute(
        "SELECT COUNT(*) FROM scan_locks"
    ).fetchone()[0] == 0


def test_release_when_not_held_is_a_no_op(migrated_db) -> None:
    """Release before acquire — the DELETE matches no rows and the
    call must not raise. Important because the ``finally`` block in
    scan() always calls release, even when acquire returned False.
    """
    release_scan_lock(migrated_db)
    assert migrated_db.execute(
        "SELECT COUNT(*) FROM scan_locks"
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Re-entrancy rejection (rev 3 Minor #3)
# ---------------------------------------------------------------------------


def test_same_process_double_acquire_rejects(migrated_db) -> None:
    """Same process trying to acquire twice — the second call returns
    False. PRIMARY KEY on lock_name is what enforces this; we still
    record OUR pid in the row, so a subsequent release succeeds.
    """
    assert acquire_scan_lock(migrated_db) is True
    assert acquire_scan_lock(migrated_db) is False

    # The lock row still holds the original (this process's) pid.
    row = migrated_db.execute(
        "SELECT pid FROM scan_locks WHERE lock_name = ?",
        (_LOCK_NAME,),
    ).fetchone()
    assert row["pid"] == os.getpid()

    release_scan_lock(migrated_db)


def test_different_pid_rejects(migrated_db) -> None:
    """Simulate another process holding the lock by inserting a row
    with a different pid. Our acquire must return False.
    """
    other_pid = os.getpid() + 1  # arbitrary non-self pid
    migrated_db.execute(
        "INSERT INTO scan_locks (lock_name, pid, started_at) "
        "VALUES (?, ?, ?)",
        (_LOCK_NAME, other_pid, time.time()),
    )
    migrated_db.commit()

    assert acquire_scan_lock(migrated_db) is False

    # Our release_scan_lock filters by pid, so it must NOT delete
    # the other process's row.
    release_scan_lock(migrated_db)
    row = migrated_db.execute(
        "SELECT pid FROM scan_locks WHERE lock_name = ?",
        (_LOCK_NAME,),
    ).fetchone()
    assert row is not None
    assert row["pid"] == other_pid


# ---------------------------------------------------------------------------
# Stale-row recovery
# ---------------------------------------------------------------------------


def test_stale_lock_row_reclaimed_on_next_acquire(migrated_db) -> None:
    """A lock row older than ``_STALE_THRESHOLD_S`` is assumed to be
    from a crashed prior scan; the next acquire reclaims it and
    succeeds.
    """
    stale_started_at = time.time() - _STALE_THRESHOLD_S - 10
    migrated_db.execute(
        "INSERT INTO scan_locks (lock_name, pid, started_at) "
        "VALUES (?, ?, ?)",
        (_LOCK_NAME, 999_999_999, stale_started_at),
    )
    migrated_db.commit()

    assert acquire_scan_lock(migrated_db) is True
    row = migrated_db.execute(
        "SELECT pid FROM scan_locks WHERE lock_name = ?",
        (_LOCK_NAME,),
    ).fetchone()
    # Our pid replaced the stale one.
    assert row["pid"] == os.getpid()

    release_scan_lock(migrated_db)


def test_fresh_lock_within_threshold_not_reclaimed(migrated_db) -> None:
    """A lock row whose started_at is WITHIN the stale threshold
    must NOT be reclaimed — an actively running scan would otherwise
    be evicted by a concurrent acquire.
    """
    fresh_started_at = time.time() - 60  # 1 minute old, well within 600s
    migrated_db.execute(
        "INSERT INTO scan_locks (lock_name, pid, started_at) "
        "VALUES (?, ?, ?)",
        (_LOCK_NAME, 999_999_999, fresh_started_at),
    )
    migrated_db.commit()

    assert acquire_scan_lock(migrated_db) is False

    # The original row survives.
    row = migrated_db.execute(
        "SELECT pid FROM scan_locks WHERE lock_name = ?",
        (_LOCK_NAME,),
    ).fetchone()
    assert row["pid"] == 999_999_999


# ---------------------------------------------------------------------------
# Round-trip: acquire → release → re-acquire succeeds
# ---------------------------------------------------------------------------


def test_release_then_reacquire(migrated_db) -> None:
    assert acquire_scan_lock(migrated_db) is True
    release_scan_lock(migrated_db)
    assert acquire_scan_lock(migrated_db) is True
    release_scan_lock(migrated_db)


# ---------------------------------------------------------------------------
# Connection state — must not leak transactions on failed acquire
# ---------------------------------------------------------------------------


def test_failed_acquire_does_not_leak_transaction(migrated_db) -> None:
    """T11 fix: the stale-recovery DELETE inside acquire_scan_lock
    opens an implicit deferred transaction with sqlite3. When the
    subsequent INSERT OR ABORT hits a PRIMARY KEY collision, the
    handler must ``conn.rollback()`` before returning False — otherwise
    the connection (shared via ``db._connections`` cache) is left
    holding a write lock that blocks WAL checkpointing and silently
    couples to unrelated future commits.
    """
    # Plant a foreign lock so the next acquire fails.
    migrated_db.execute(
        "INSERT INTO scan_locks (lock_name, pid, started_at) "
        "VALUES (?, ?, ?)",
        (_LOCK_NAME, os.getpid() + 1, time.time()),
    )
    migrated_db.commit()
    assert migrated_db.in_transaction is False

    assert acquire_scan_lock(migrated_db) is False
    assert migrated_db.in_transaction is False, (
        "acquire_scan_lock leaked an open transaction"
    )
