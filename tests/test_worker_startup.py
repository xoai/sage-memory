"""P1-3 — Worker startup ordering / crash-safety tests (SM-REL-01).

Written tests-first per 05-spec-phase1 §P1-3. The defect (observed in
CI logs as PytestUnhandledThreadExceptionWarning):

    worker.py:276 _startup_recovery
    → sqlite3.OperationalError: no such table: extraction_queue

``_open_worker_conn`` deliberately skipped migrations ("DB must
already be migrated"), but ``Worker(db_path)`` can be handed a path
whose file was just created empty by sqlite3.connect — the thread
then died unobserved.

Contracts pinned:
  - Worker against an UNMIGRATED DB migrates (or refuses loudly) —
    never an unhandled thread exception
  - A crashing run() body logs loudly AND records the reason in
    worker_state (no silent death)
  - Successful startup clears a previous crash marker
  - `worker --status` surfaces the crashed state
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest

from sage_memory.worker import Worker


@pytest.fixture
def unmigrated_db_path(tmp_path: Path) -> str:
    """A path whose DB file does not exist yet — the worker's first
    connection creates it EMPTY (no tables, user_version 0)."""
    return str(tmp_path / "fresh-worker.db")


def _excepthook_captured(monkeypatch):
    """Capture threading.excepthook invocations."""
    captured = []
    real_hook = threading.excepthook

    def _hook(args):
        captured.append(args)
        real_hook(args)

    monkeypatch.setattr(threading, "excepthook", _hook)
    return captured


def test_worker_migrates_unmigrated_db(
    unmigrated_db_path, monkeypatch,
):
    captured = _excepthook_captured(monkeypatch)
    w = Worker(unmigrated_db_path)
    # drain_once exercises the open → startup_recovery → queue path
    # synchronously — pre-fix it raised OperationalError.
    processed = w.drain_once(timeout_s=0.5)
    assert processed == 0
    assert captured == [], (
        f"unhandled thread exception(s): {[c.exc_value for c in captured]}"
    )
    # And the DB is now actually migrated.
    import sqlite3
    conn = sqlite3.connect(unmigrated_db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version >= 8, f"expected migrated DB; user_version={version}"


def test_run_crash_is_logged_and_marked(
    unmigrated_db_path, monkeypatch, caplog,
):
    """A top-level failure in the thread body must (a) log via
    logger.exception, (b) record the reason in worker_state, (c) NOT
    propagate to threading.excepthook (silent death is the bug)."""
    captured = _excepthook_captured(monkeypatch)

    def _boom(self, conn):
        raise RuntimeError("simulated startup crash")

    monkeypatch.setattr(Worker, "_claim_one", _boom)

    w = Worker(unmigrated_db_path, poll_interval_ms=50)
    with caplog.at_level(logging.ERROR, logger="sage_memory.worker"):
        w.start()
        deadline = time.time() + 5
        while time.time() < deadline and w.is_alive():
            time.sleep(0.05)
        # Thread should exit (crashed) rather than spin.
        assert not w.is_alive(), "crashed worker thread should exit"

    assert captured == [], (
        f"crash must not reach excepthook: "
        f"{[c.exc_value for c in captured]}"
    )
    assert any("simulated startup crash" in r.message for r in caplog.records), (
        f"crash not logged: {[r.message for r in caplog.records]}"
    )

    import sqlite3
    conn = sqlite3.connect(unmigrated_db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT last_error FROM worker_state WHERE id = 1"
    ).fetchone()
    conn.close()
    assert row is not None and row["last_error"], (
        "worker_state.last_error must record the crash reason"
    )
    assert "simulated startup crash" in row["last_error"]


def test_successful_start_clears_crash_marker(unmigrated_db_path, monkeypatch):
    import sqlite3
    # Pre-mark a crash.
    w = Worker(unmigrated_db_path)
    w.drain_once(timeout_s=0.3)  # migrates the DB
    conn = sqlite3.connect(unmigrated_db_path)
    conn.execute(
        "UPDATE worker_state SET last_error = 'old crash', "
        "last_error_at = 1.0 WHERE id = 1"
    )
    conn.commit()
    conn.close()

    w.drain_once(timeout_s=0.3)
    conn = sqlite3.connect(unmigrated_db_path)
    row = conn.execute(
        "SELECT last_error FROM worker_state WHERE id = 1"
    ).fetchone()
    conn.close()
    assert row[0] is None, (
        f"crash marker must clear on healthy startup; got {row[0]!r}"
    )


def test_worker_status_surfaces_crash(unmigrated_db_path, capsys):
    import sqlite3
    w = Worker(unmigrated_db_path)
    w.drain_once(timeout_s=0.3)  # migrate
    conn = sqlite3.connect(unmigrated_db_path)
    conn.execute(
        "UPDATE worker_state SET last_error = 'boom: disk on fire', "
        "last_error_at = 1234.0 WHERE id = 1"
    )
    conn.commit()
    conn.close()

    from sage_memory.cli_worker import print_worker_status
    # cli_worker reads via db.get_db() — point it at our DB by
    # setting an isolated project root around it.
    from sage_memory.db import _open, close_all
    close_all()
    conn2 = _open(Path(unmigrated_db_path))
    try:
        from unittest.mock import patch
        with patch("sage_memory.cli_worker.get_db", return_value=conn2):
            print_worker_status()
    finally:
        close_all()
    out = capsys.readouterr().out
    assert "boom: disk on fire" in out, (
        f"--status must surface the crash; got:\n{out}"
    )


# ─── Migration 011 (crash-state columns) ──────────────────────────


THROUGH_010 = (
    "001_initial.sql", "002_edges.sql", "003_memory_health.sql",
    "004_chunks.sql", "005_entities.sql", "006_embedding_meta.sql",
    "007_extraction_queue.sql", "008_worker_state.sql",
    "009_code_symbols.sql", "010_unresolved_relations_index.sql",
)


def test_011_applies_on_010_db(
    fresh_db, tmp_migrations_dir, copy_production_migrations,
):
    from sage_memory.db import _migrate
    copy_production_migrations(*THROUGH_010)
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    assert fresh_db.execute("PRAGMA user_version").fetchone()[0] == 10

    copy_production_migrations("011_worker_crash_state.sql")
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    assert fresh_db.execute("PRAGMA user_version").fetchone()[0] == 11
    cols = {
        r[1] for r in fresh_db.execute("PRAGMA table_info(worker_state)")
    }
    assert {"last_error", "last_error_at"} <= cols
