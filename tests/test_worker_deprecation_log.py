"""Task 8 — Worker deprecation log (0.9.0 → removal in 1.0.0).

Per spec §Worker deprecation note: emit an INFO-level message once
per process when the background worker starts. Subsequent
`Worker.start()` calls in the same process must NOT re-emit.
`cli_worker --status` does NOT enter the run loop and must NOT emit.
"""

from __future__ import annotations

import logging

import pytest

from sage_memory import worker as _worker_mod
from sage_memory.worker import Worker


# Ensure each test starts from a clean module-level flag state.
@pytest.fixture(autouse=True)
def _reset_deprecation_flag():
    _worker_mod._deprecation_logged = False
    yield
    _worker_mod._deprecation_logged = False


# ───── Log emission ─────

def test_worker_start_logs_deprecation_once(tmp_path, caplog):
    """First Worker.start() in a process emits the deprecation INFO."""
    db_path = tmp_path / "wm.db"
    db_path.touch()
    w = Worker(db_path=db_path, poll_interval_ms=10000)
    with caplog.at_level(logging.INFO, logger="sage_memory.worker"):
        w.start()
        w.stop()
    messages = [r.message for r in caplog.records]
    assert any(
        "deprecated" in m and "1.0.0" in m for m in messages
    ), f"expected deprecation INFO log; got:\n{messages}"


def test_second_start_does_not_re_emit(tmp_path, caplog):
    """Idempotent: a second start() (after stop) does not re-log."""
    db_path = tmp_path / "wm.db"
    db_path.touch()

    w1 = Worker(db_path=db_path, poll_interval_ms=10000)
    with caplog.at_level(logging.INFO, logger="sage_memory.worker"):
        w1.start()
        w1.stop()
    n_first = sum(
        1 for r in caplog.records
        if "deprecated" in r.message and "1.0.0" in r.message
    )
    assert n_first == 1

    caplog.clear()
    w2 = Worker(db_path=db_path, poll_interval_ms=10000)
    with caplog.at_level(logging.INFO, logger="sage_memory.worker"):
        w2.start()
        w2.stop()
    n_second = sum(
        1 for r in caplog.records
        if "deprecated" in r.message and "1.0.0" in r.message
    )
    assert n_second == 0, "deprecation must log only once per process"


def test_cli_worker_status_does_not_emit_deprecation(tmp_path, capsys):
    """`sage-memory worker --status` inspects queue rows directly and
    does NOT instantiate a Worker / enter the run loop. The deprecation
    log must not appear in its output."""
    import subprocess
    import sys
    import os

    # Build a project with a .git so cli_status finds it.
    (tmp_path / ".git").mkdir()

    env = {**os.environ, "HOME": str(tmp_path / "home")}
    (tmp_path / "home").mkdir(exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-m", "sage_memory", "worker", "--status"],
        capture_output=True, text=True, cwd=tmp_path, env=env,
        timeout=10,
    )
    # `worker --status` exits 0 with status info.
    assert result.returncode == 0, (
        f"worker --status failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "deprecated" not in combined.lower(), (
        "deprecation log must not surface in --status output; got:\n"
        + combined
    )


# ───── Existing worker tests unaffected ─────
# (covered by test_worker.py — 17 tests must still pass)
