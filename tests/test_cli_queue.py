"""M5 T4 — `sage-memory queue prune` manual CLI tests.

Spec A11 second half — manual entry point complements T0's worker
auto-prune. Operator-initiated bypasses the 24h gate.
"""

from __future__ import annotations

import time
import uuid

import pytest

from sage_memory import cli_queue
from sage_memory.db import close_all, get_project_db, override_project_root
from sage_memory.worker import Worker


@pytest.fixture
def project_db(tmp_path):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".sage").mkdir()
    override_project_root(project_root)
    db = get_project_db()
    yield {"db": db, "path": tmp_path}
    close_all()
    override_project_root(None)


def _insert_done(db, *, processed_at):
    db.execute(
        "INSERT INTO extraction_queue "
        "(id, memory_id, task_type, status, attempts, "
        " created_at, processed_at) "
        "VALUES (?, NULL, 'dedup', 'done', 1, ?, ?)",
        (uuid.uuid4().hex, processed_at - 1, processed_at),
    )


def test_queue_prune_manual_deletes_aged_rows(project_db, capsys):
    """A11: manual `queue prune` deletes 30+ day aged rows +
    updates worker_state.last_prune_at."""
    db = project_db["db"]
    now = time.time()
    for _ in range(5):
        _insert_done(db, processed_at=now - 31 * 86400)
    db.commit()

    rc = cli_queue.run_queue(["prune"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "5" in out

    remaining = db.execute(
        "SELECT COUNT(*) AS n FROM extraction_queue"
    ).fetchone()["n"]
    assert remaining == 0

    state = db.execute(
        "SELECT last_prune_at FROM worker_state"
    ).fetchone()
    assert state["last_prune_at"] is not None
    assert abs(state["last_prune_at"] - time.time()) < 5.0


def test_queue_prune_manual_bypasses_24h_gate(project_db):
    """A11: manual command runs regardless of when auto-prune last
    ran. Calls worker.maybe_prune() first (which skips within window)
    then manual `queue prune` — manual still updates last_prune_at."""
    db = project_db["db"]
    now = time.time()
    # Insert aged rows
    for _ in range(3):
        _insert_done(db, processed_at=now - 31 * 86400)
    db.commit()
    close_all()

    # Worker auto-prune (first run runs and deletes).
    db_path = next(project_db["path"].glob("**/memory.db"))
    worker = Worker(str(db_path))
    did_run = worker.maybe_prune()
    assert did_run is True

    # Insert 2 more aged rows AFTER auto-prune ran.
    db = get_project_db()
    project_db["db"] = db  # update fixture handle so finalizer doesn't blow up
    for _ in range(2):
        _insert_done(db, processed_at=now - 31 * 86400)
    db.commit()

    # Worker auto-prune now in 24h window → would skip.
    did_run_again = worker.maybe_prune()
    assert did_run_again is False

    # But manual command still runs.
    rc = cli_queue.run_queue(["prune"])
    assert rc == 0
    remaining = db.execute(
        "SELECT COUNT(*) AS n FROM extraction_queue"
    ).fetchone()["n"]
    assert remaining == 0
