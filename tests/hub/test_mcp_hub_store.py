"""M3.6 — MCP ``sage_memory_store(hub_target=...)`` integration.

3 tests per plan.md M3.6 done-when:
  (a) hub_target → routes correctly to the named writable project
  (b) non-writable target → error envelope
  (c) no --hub flag → hub_target silently ignored (regular store fires)
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from sage_memory.db import (
    _open, close_all, get_project_db_path, override_project_root,
)
from sage_memory.embedder import (
    EMBEDDING_DIM, LocalEmbedder, set_embedder,
)
from sage_memory.hub import config as hub_config
from sage_memory.hub import ownership as hub_ownership
from sage_memory.server_fastmcp import build_mcp_app


class _TestEmbedder:
    name = "test-m36"
    version = "v1"
    dim = EMBEDDING_DIM
    quality = 0.9
    max_input_chars = 8192

    def embed(self, text):
        h = abs(hash(text)) % 100
        return [(h + i) / 100.0 for i in range(EMBEDDING_DIM)]


@pytest.fixture
def hub_with_writable_target(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    set_embedder(_TestEmbedder())
    hub_ownership.reset_module_state_for_tests()

    ops = tmp_path / "ops"
    ops.mkdir()
    (ops / ".git").mkdir()
    (ops / ".sage-memory").mkdir()
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / ".git").mkdir()
    (backend / ".sage-memory").mkdir()

    # An "active" project for the no-hub-flag case so the regular
    # store handler has a project DB to land in.
    active = tmp_path / "active"
    active.mkdir()
    (active / ".git").mkdir()
    (active / ".sage-memory").mkdir()
    close_all()
    override_project_root(active)
    _open(get_project_db_path(active))

    hub_path = tmp_path / ".sage-hub.yaml"
    cfg = hub_config.init(hub_path)
    cfg = hub_config.add_project(cfg, "ops", ops, writable=True)
    cfg = hub_config.add_project(cfg, "backend", backend, writable=False)
    hub_config.save(cfg, hub_path)
    monkeypatch.setattr(hub_config, "DEFAULT_HUB_PATH", hub_path)

    yield hub_path, ops, backend, active

    close_all()
    set_embedder(LocalEmbedder())
    hub_ownership.reset_module_state_for_tests()


def test_hub_target_routes_to_writable_project(hub_with_writable_target):
    """sage_memory_store(hub_target='ops', ...) → new memory lives in
    ops's DB, not the active project's."""
    hub_path, ops, _backend, active = hub_with_writable_target
    mcp = build_mcp_app(hub_enabled=True)

    sentinel = "M3.6 MCP routed-write sentinel"

    async def _scenario():
        return await mcp.call_tool(
            "sage_memory_store",
            {
                "content": sentinel,
                "title": "M3.6 routed",
                "hub_target": "ops",
            },
        )

    result = asyncio.run(_scenario())
    envelope = json.loads(result[0].text)
    assert envelope.get("success") is True, f"{envelope!r}"
    memory_id = envelope["id"]

    # Verify it landed in ops, NOT active.
    ops_conn = sqlite3.connect(
        f"file:{get_project_db_path(ops)}?mode=ro", uri=True,
    )
    try:
        row = ops_conn.execute(
            "SELECT id FROM memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row is not None, "routed memory must land in ops DB"
    finally:
        ops_conn.close()


def test_hub_target_non_writable_returns_error_envelope(
    hub_with_writable_target,
):
    hub_path, _ops, _backend, _active = hub_with_writable_target
    mcp = build_mcp_app(hub_enabled=True)

    async def _scenario():
        return await mcp.call_tool(
            "sage_memory_store",
            {
                "content": "this should be rejected",
                "title": "rejection",
                "hub_target": "backend",
            },
        )

    result = asyncio.run(_scenario())
    envelope = json.loads(result[0].text)
    # Either {success: false, message: ...} from store_to_project,
    # or {error: ...} from the wrapper if it raised. Both
    # acceptable as "rejected" — assert either way.
    assert envelope.get("success") is False or "error" in envelope, (
        f"non-writable target must be rejected; got {envelope!r}"
    )
    msg = envelope.get("message") or envelope.get("error") or ""
    assert "writable" in msg.lower()


def test_hub_target_ignored_without_hub_flag(hub_with_writable_target):
    """Without --hub, hub_target is silently dropped — regular per-project
    store fires against the active project's DB."""
    hub_path, _ops, _backend, active = hub_with_writable_target
    mcp = build_mcp_app(hub_enabled=False)

    async def _scenario():
        return await mcp.call_tool(
            "sage_memory_store",
            {
                "content": "Without hub mode, hub_target is dropped",
                "title": "no hub",
                "hub_target": "ops",  # ignored
            },
        )

    result = asyncio.run(_scenario())
    envelope = json.loads(result[0].text)
    assert envelope.get("success") is True, (
        f"hub_target without --hub must not block; got {envelope!r}"
    )
    memory_id = envelope["id"]

    # The memory must have landed in the ACTIVE project, NOT ops.
    active_conn = sqlite3.connect(
        f"file:{get_project_db_path(active)}?mode=ro", uri=True,
    )
    try:
        row = active_conn.execute(
            "SELECT id FROM memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row is not None, (
            "memory must land in active project when --hub is off"
        )
    finally:
        active_conn.close()
