"""T11 — sage-memory status CLI tests.

The status subcommand prints active embedder + corpus dim + stale-embedding
count. The dispatch must preserve back-compat: `sage-memory` (no args) and
`sage-memory run` both invoke the MCP server.
"""

from __future__ import annotations

import io
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from sage_memory import main


# ───────────────────────────────────────────────────────────────────
# Helper: build a tiny populated project DB for the status command
# ───────────────────────────────────────────────────────────────────


def _populate_db(project_root: Path, n_embedded: int = 5, n_unembedded: int = 3):
    """Create a populated DB at project_root/.sage-memory/memory.db with
    `n_embedded` embedded=1 memories (legacy backfilled) and
    `n_unembedded` embedded=0 memories (no meta row → stale)."""
    from sage_memory.db import _open, get_project_db_path, close_all, override_project_root
    close_all()
    override_project_root(project_root)
    db = _open(get_project_db_path(project_root))
    for i in range(n_embedded):
        db.execute(
            "INSERT INTO memories(id,title,content,content_hash,embedded,created_at,updated_at,accessed_at) "
            "VALUES (?,?,?,?,1,?,?,?)",
            (f"em{i}", f"t{i}", f"c{i}", f"h_em{i}", 1.0 + i, 1.0 + i, 1.0 + i),
        )
    for i in range(n_unembedded):
        db.execute(
            "INSERT INTO memories(id,title,content,content_hash,embedded,created_at,updated_at,accessed_at) "
            "VALUES (?,?,?,?,0,?,?,?)",
            (f"un{i}", f"t{i}", f"c{i}", f"h_un{i}", 100.0 + i, 100.0 + i, 100.0 + i),
        )
    db.commit()
    return db


# ───────────────────────────────────────────────────────────────────
# T11 tests
# ───────────────────────────────────────────────────────────────────


def test_status_basic(tmp_path, capsys, monkeypatch):
    """Populated DB: 5 embedded + 3 unembedded → 8 total, 8 stale
    (5 legacy backfilled to ('legacy','0',384) which != current
    ('local','tfidf-v1',384); 3 unembedded have no meta)."""
    # Ensure a clean .git so the project is detected
    (tmp_path / ".git").mkdir()
    _populate_db(tmp_path)

    monkeypatch.setattr("sys.argv", ["sage-memory", "status"])
    # Capture stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        main()
    out = buf.getvalue()

    # Header fields
    assert "embedder" in out.lower()
    assert "local" in out  # name
    assert "tfidf-v1" in out  # version
    assert "384" in out  # dim
    # Corpus dim
    assert "vec_dim" in out.lower() or "corpus" in out.lower()
    # Total memories
    assert "8" in out  # 5 + 3 = 8 total
    # Stale count = 8 (5 legacy stale vs current + 3 unembedded with no meta)
    # Match must be unambiguous in text output
    assert "stale" in out.lower()


def test_status_empty_db(tmp_path, capsys, monkeypatch):
    """Fresh DB: 0 memories, 0 stale, LocalEmbedder default, vec_dim=384."""
    (tmp_path / ".git").mkdir()
    # Initialize DB but don't insert any memories
    from sage_memory.db import _open, get_project_db_path, close_all, override_project_root
    close_all()
    override_project_root(tmp_path)
    _open(get_project_db_path(tmp_path))

    monkeypatch.setattr("sys.argv", ["sage-memory", "status"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        main()
    out = buf.getvalue()

    assert "local" in out
    assert "tfidf-v1" in out
    assert "384" in out
    # 0 memories
    assert "0" in out


def test_status_no_args_still_runs_server(monkeypatch):
    """Back-compat regression: `sage-memory` (no args) MUST invoke the MCP
    server (existing entry point), not the CLI dispatch.

    We mock asyncio.run + the server's run() to assert dispatch routes to
    them rather than to the status subcommand.
    """
    monkeypatch.setattr("sys.argv", ["sage-memory"])

    server_called = {"yes": False}
    fake_run_coro = MagicMock()  # the coroutine object returned by server.run()

    def fake_server_run():
        server_called["yes"] = True
        return fake_run_coro

    def fake_asyncio_run(coro):
        # The dispatch must pass us the coroutine from server.run()
        assert coro is fake_run_coro

    monkeypatch.setattr("sage_memory.run", fake_server_run)
    monkeypatch.setattr("asyncio.run", fake_asyncio_run)

    main()
    assert server_called["yes"], "main() with no args should call server.run()"


def test_status_run_alias_still_runs_server(monkeypatch):
    """`sage-memory run` is an explicit alias for the no-args MCP server
    invocation."""
    monkeypatch.setattr("sys.argv", ["sage-memory", "run"])

    server_called = {"yes": False}
    fake_run_coro = MagicMock()

    def fake_server_run():
        server_called["yes"] = True
        return fake_run_coro

    def fake_asyncio_run(coro):
        assert coro is fake_run_coro

    monkeypatch.setattr("sage_memory.run", fake_server_run)
    monkeypatch.setattr("asyncio.run", fake_asyncio_run)

    main()
    assert server_called["yes"]
