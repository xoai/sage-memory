"""Advisory-lock primitive backed by the ``scan_locks`` table.

Two callers (``acquire_scan_lock`` and ``release_scan_lock``) plus a
600-second stale-row recovery mechanism. The ``scan_locks`` schema
from migration 009:

    CREATE TABLE scan_locks (
        lock_name  TEXT PRIMARY KEY,
        pid        INTEGER NOT NULL,
        started_at REAL NOT NULL
    );

The PRIMARY KEY on ``lock_name`` is what makes ``INSERT OR ABORT``
into a fast-fail mutex — a second acquire (same process OR another)
hits the IntegrityError path at commit time and returns False. This
gives us both **mutual exclusion** AND **no-nested-acquires** (rev 3
Minor #3) without extra bookkeeping.

The lock is **project-scoped** (rev 3 Minor #4): each project's
SQLite DB has its own ``scan_locks`` table reached via
``get_db("project")``. Concurrent scans on DIFFERENT projects are NOT
blocked — that would be over-eager. Same-project concurrent scans
ARE rejected, which is the entire point.

The existing ``dedup --sync`` flow uses ``BEGIN IMMEDIATE`` for
write-coordination — that BLOCKS the loser; we want a FAST-FAIL
"someone else is running, try again later" signal here, hence a
purpose-built advisory lock instead of overloading the transaction
machinery.
"""

from __future__ import annotations

import os
import sqlite3
import time


_LOCK_NAME = "scan"

# How long before a lock row is assumed to be from a crashed prior
# scan and gets reclaimed. 10 minutes is generous; a real scan on a
# 5000-file repo completes in ~30s on 2024-era hardware (per spec
# rev 2 line 179-183). A wider window would let an actually-running
# scan get crashed-over by a hung sibling.
_STALE_THRESHOLD_S = 600


def acquire_scan_lock(conn: sqlite3.Connection) -> bool:
    """Try to acquire the project's scan lock.

    Returns
    -------
    True
        The lock is now held by this process; caller MUST call
        :func:`release_scan_lock` in a finally-block.
    False
        Another scan is already running on this project (or the same
        process tried to acquire twice — re-entrancy is rejected per
        rev 3 Minor #3).

    Stale-row recovery: a row whose ``started_at`` is older than
    ``_STALE_THRESHOLD_S`` is assumed to be from a crashed prior
    scan; deleted before the INSERT is attempted so this acquire
    succeeds.
    """
    # Stale recovery happens BEFORE the INSERT so the same call that
    # finds a stale row can also claim it. Doing it in a separate
    # transaction would race with a parallel acquire.
    now = time.time()
    conn.execute(
        "DELETE FROM scan_locks WHERE lock_name = ? AND started_at < ?",
        (_LOCK_NAME, now - _STALE_THRESHOLD_S),
    )
    try:
        conn.execute(
            "INSERT OR ABORT INTO scan_locks (lock_name, pid, started_at) "
            "VALUES (?, ?, ?)",
            (_LOCK_NAME, os.getpid(), now),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # PRIMARY KEY collision — another scan owns the lock. The
        # stale-recovery DELETE above opened an implicit transaction
        # with sqlite3's default deferred isolation; rollback so the
        # connection (shared via db._connections caching) is not
        # left holding a write lock that blocks WAL checkpointing
        # and silently piggybacks onto unrelated future commits.
        conn.rollback()
        return False


def release_scan_lock(conn: sqlite3.Connection) -> None:
    """Release the project's scan lock if it's held by this process.

    The ``pid = ?`` predicate is what makes this safe to call even
    when ``acquire_scan_lock`` returned False — we never accidentally
    release a lock owned by another process. ``DELETE`` against a
    non-matching row is a no-op.
    """
    conn.execute(
        "DELETE FROM scan_locks WHERE lock_name = ? AND pid = ?",
        (_LOCK_NAME, os.getpid()),
    )
    conn.commit()
