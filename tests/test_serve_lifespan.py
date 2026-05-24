"""M1.1b — FastMCP lifespan bootstrap-before-tool-call + cleanup-on-exit.

Verifies the ordering contract documented in spec.md §"Graceful
shutdown" (cycle 20260524-team-mcp-transports). The lifespan is the
load-bearing replacement for the pre-M1.1b ``server.py:run()`` bootstrap
+ teardown sequence; this test guards that the swap preserves it:

  1. Embedder bootstrap runs BEFORE any tool call can execute.
  2. ``flush_all_access`` AND ``close_all`` both fire when the lifespan
     context exits (``__aexit__`` -> ``finally`` block).

Ordering is asserted via a recorded events list + ``caplog`` for the
embedder-bootstrap log line.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from sage_memory.db import (
    _open, close_all, get_project_db_path, override_project_root,
)
from sage_memory.embedder import (
    EMBEDDING_DIM, LocalEmbedder, set_embedder,
)


class _HighQualityTestEmbedder:
    """Mirrors the helper from tests/test_worker_lifecycle.py."""
    name = "test-hq"
    version = "v1"
    dim = EMBEDDING_DIM
    quality = 0.9
    max_input_chars = 8192

    def embed(self, text):  # noqa: D401 — test stub
        return [0.1] * EMBEDDING_DIM


@pytest.fixture
def project_root(tmp_path):
    (tmp_path / ".git").mkdir()
    close_all()
    override_project_root(tmp_path)
    set_embedder(_HighQualityTestEmbedder())
    path = get_project_db_path(tmp_path)
    _open(path)
    yield tmp_path
    close_all()
    set_embedder(LocalEmbedder())


def test_lifespan_bootstrap_before_tool_call_and_cleanup_after(
    project_root, monkeypatch, caplog,
):
    """The lifespan's bootstrap must complete before tools are callable,
    and ``flush_all_access`` + ``close_all`` must fire on context exit."""
    from sage_memory import db as dbmod
    from sage_memory import search as searchmod
    from sage_memory import server_fastmcp

    events: list[str] = []
    real_close_all = dbmod.close_all
    real_flush = searchmod.flush_all_access

    def spy_close_all():
        events.append("close_all")
        return real_close_all()

    def spy_flush():
        events.append("flush_all_access")
        return real_flush()

    monkeypatch.setattr(dbmod, "close_all", spy_close_all)
    monkeypatch.setattr(searchmod, "flush_all_access", spy_flush)

    caplog.set_level(logging.INFO, logger="sage-memory")

    async def _scenario() -> None:
        mcp = server_fastmcp.build_mcp_app()
        async with server_fastmcp.server_lifespan(mcp):
            # Inside the lifespan: bootstrap must already have fired.
            bootstrap_fired = any(
                "embedder:" in r.getMessage() and "active" in r.getMessage()
                for r in caplog.records
            )
            assert bootstrap_fired, (
                "embedder bootstrap log must appear before any tool call; "
                "captured records: "
                + repr([r.getMessage() for r in caplog.records])
            )
            events.append("inside_lifespan")
            # A real tool call should succeed against the live project DB.
            result = await mcp.call_tool(
                "sage_memory_list", {"limit": 1},
            )
            assert result is not None, "tool call should return a result"
            events.append("after_tool_call")

    asyncio.run(_scenario())

    # After lifespan exit, both teardown hooks must have fired.
    assert "flush_all_access" in events, (
        f"flush_all_access must fire on lifespan exit; events={events}"
    )
    assert "close_all" in events, (
        f"close_all must fire on lifespan exit; events={events}"
    )
    # Cleanup ordering: tool call window precedes both teardown hooks.
    assert events.index("after_tool_call") < events.index("flush_all_access")
    assert events.index("after_tool_call") < events.index("close_all")
