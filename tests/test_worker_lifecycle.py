"""T4 — server.py worker lifecycle + worker --status CLI.

Tests _needs_worker resolution + the CLI subcommand. Full async
stdio_server integration is out of scope; the lifecycle wiring is
verified via _needs_worker (the gating function) and start/stop
contracts already tested in test_worker.py.
"""

from __future__ import annotations

import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from sage_memory.db import (
    _open, get_project_db_path, close_all, override_project_root,
)
from sage_memory.embedder import (
    LocalEmbedder, set_embedder, EMBEDDING_DIM,
)


class _HighQualityTestEmbedder:
    name = "test-hq"
    version = "v1"
    dim = EMBEDDING_DIM
    quality = 0.9
    max_input_chars = 8192
    def embed(self, text):
        return [0.1] * EMBEDDING_DIM


@pytest.fixture
def project_root(tmp_path):
    """Project root (directory above .sage-memory/memory.db)."""
    (tmp_path / ".git").mkdir()
    close_all()
    override_project_root(tmp_path)
    set_embedder(_HighQualityTestEmbedder())
    path = get_project_db_path(tmp_path)
    _open(path)
    yield tmp_path
    close_all()
    set_embedder(LocalEmbedder())


@pytest.fixture
def db_file_hq(project_root):
    """File-backed DB path (under project_root/.sage-memory/memory.db)."""
    return get_project_db_path(project_root)


@pytest.fixture
def db_conn(db_file_hq):
    return _open(db_file_hq)


# ─── _needs_worker resolution ──────────────────────────────────


def test_needs_worker_with_llm_key(db_conn, monkeypatch):
    """LLM key set + corpus dim matches active embedder → True."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    from sage_memory.server import _needs_worker
    assert _needs_worker(db_conn) is True


def test_needs_worker_no_worker_when_free_path(db_conn, monkeypatch):
    """No LLM key + no stale embeddings → False (worker not started)."""
    for var in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]:
        monkeypatch.delenv(var, raising=False)
    from sage_memory.server import _needs_worker
    assert _needs_worker(db_conn) is False


def test_needs_worker_for_stale_memory_meta(db_conn, monkeypatch):
    """Memory row with embedded=1 but missing memory_embedding_meta →
    worker needed (reembed path)."""
    for var in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]:
        monkeypatch.delenv(var, raising=False)

    mid = "stale_mem_1"
    db_conn.execute(
        "INSERT INTO memories(id, title, content, content_hash, "
        "embedded, created_at, updated_at, accessed_at) "
        "VALUES (?, 't', 'c', 'h', 1, 1, 1, 1)",
        (mid,),
    )
    db_conn.commit()

    from sage_memory.server import _needs_worker
    assert _needs_worker(db_conn) is True


def test_needs_worker_for_stale_chunk_meta(db_conn, monkeypatch):
    """Chunk row without chunk_embedding_meta → worker needed."""
    for var in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]:
        monkeypatch.delenv(var, raising=False)

    mid = "mem_with_chunk"
    db_conn.execute(
        "INSERT INTO memories(id, title, content, content_hash, "
        "embedded, created_at, updated_at, accessed_at) "
        "VALUES (?, 't', 'c', 'h', 0, 1, 1, 1)",
        (mid,),
    )
    db_conn.execute(
        "INSERT INTO chunks (id, memory_id, chunk_index, content, "
        "byte_start, byte_end, created_at) "
        "VALUES (?, ?, 0, 'chunk content', 0, 12, 1)",
        ("chunk1", mid),
    )
    db_conn.commit()

    from sage_memory.server import _needs_worker
    assert _needs_worker(db_conn) is True


# ─── CLI worker --status ────────────────────────────────────────


def _run_cli(*args, env_extra=None):
    """Invoke `python -m sage_memory <args>` as a subprocess."""
    import os
    env = os.environ.copy()
    # Scrub keys for free-path behavior
    for var in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                "VOYAGE_API_KEY", "COHERE_API_KEY"]:
        env.pop(var, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "sage_memory", *args],
        capture_output=True, text=True, env=env, timeout=30,
    )


def test_cli_worker_help_text():
    result = _run_cli("worker", "--help")
    assert result.returncode == 0
    assert "worker" in result.stdout.lower()
    assert "status" in result.stdout.lower()


def test_cli_worker_status_exit_code_zero(project_root):
    """Empty queue + fresh DB → exit 0; output shows zeros for every
    status row."""
    result = _run_cli(
        "worker", "--status",
        env_extra={"SAGE_PROJECT_ROOT": str(project_root)},
    )
    assert result.returncode == 0
    out = result.stdout.lower()
    # Status headers always present; verify zeros are rendered.
    assert "pending" in out
    assert "0" in out


def test_cli_worker_status_with_failed_tasks(project_root, db_conn):
    """T9 polish: failed-task rows surface in CLI output with their
    task_type breakdown so the user can see what failed."""
    now = time.time()
    db_conn.execute(
        "INSERT INTO memories(id, title, content, content_hash, "
        "embedded, created_at, updated_at, accessed_at) "
        "VALUES ('M1', 't', 'c', 'h', 0, 1, 1, 1)"
    )
    db_conn.execute(
        "INSERT INTO extraction_queue (id, memory_id, task_type, "
        "status, attempts, created_at, last_error) "
        "VALUES (?, 'M1', 'dedup', 'failed', 1, ?, ?)",
        (uuid.uuid4().hex, now,
         "dedup not implemented (M5)"),
    )
    db_conn.commit()

    result = _run_cli(
        "worker", "--status",
        env_extra={"SAGE_PROJECT_ROOT": str(project_root)},
    )
    assert result.returncode == 0
    assert "failed" in result.stdout.lower()
    assert "dedup" in result.stdout.lower()


def test_cli_worker_status_populated(project_root, db_conn):
    """CLI counts queue rows by status/task_type."""
    # Insert one of each status
    now = time.time()
    rows = [
        (uuid.uuid4().hex, "extract", "pending"),
        (uuid.uuid4().hex, "extract", "running"),
        (uuid.uuid4().hex, "reembed", "done"),
        (uuid.uuid4().hex, "dedup", "failed"),
    ]
    # Need a real memory_id for the FK
    db_conn.execute(
        "INSERT INTO memories(id, title, content, content_hash, "
        "embedded, created_at, updated_at, accessed_at) "
        "VALUES ('M1', 't', 'c', 'h', 0, 1, 1, 1)"
    )
    for qid, tt, st in rows:
        db_conn.execute(
            "INSERT INTO extraction_queue (id, memory_id, task_type, "
            "status, attempts, created_at) "
            "VALUES (?, 'M1', ?, ?, 0, ?)",
            (qid, tt, st, now),
        )
    db_conn.commit()

    result = _run_cli(
        "worker", "--status",
        env_extra={"SAGE_PROJECT_ROOT": str(project_root)},
    )
    assert result.returncode == 0
    out = result.stdout
    # All four status labels appear (table headers, always present)
    assert "pending" in out
    assert "running" in out
    assert "done" in out
    assert "failed" in out
    # And the task-type breakdown surfaces actual rows (not zero totals)
    assert "extract" in out
    assert "reembed" in out
    assert "dedup" in out
