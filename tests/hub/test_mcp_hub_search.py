"""M2.5 — MCP ``sage_memory_search(hub_projects=...)`` integration.

4 tests per plan.md M2.5 done-when:
  (a) server --hub → tool description mentions hub_projects + schema
      includes it
  (b) without --hub → param ignored (silent drop; not an error)
  (c) with hub_projects=[a,b] AND --hub → fan-out fired
  (d) invalid hub project name → error envelope
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from sage_memory.db import (
    _open, close_all, get_project_db_path, override_project_root,
)
from sage_memory.embedder import (
    EMBEDDING_DIM, LocalEmbedder, set_embedder,
)
from sage_memory.hub import config as hub_config
from sage_memory.server_fastmcp import build_mcp_app


class _SeedEmbedder:
    name = "test-seed"
    version = "v1"
    dim = EMBEDDING_DIM
    quality = 0.9
    max_input_chars = 8192

    def embed(self, text):
        h = abs(hash(text)) % 100
        return [(h + i) / 100.0 for i in range(EMBEDDING_DIM)]


@pytest.fixture
def hub_with_two_projects(tmp_path, monkeypatch):
    """Tmp HOME + two seeded projects + a tmp hub config registering
    both. monkeypatches DEFAULT_HUB_PATH so the wrapper finds our
    fixture-managed config without touching the user's home."""
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    set_embedder(_SeedEmbedder())

    from sage_memory.store import store

    project_roots: list[Path] = []
    for label, content in [
        ("alpha", "Hub-search MCP test alpha shipping"),
        ("beta", "Hub-search MCP test beta shipping"),
    ]:
        root = tmp_path / label
        root.mkdir()
        (root / ".git").mkdir()
        close_all()
        override_project_root(root)
        _open(get_project_db_path(root))
        store(content=content, title=f"{label} mcp note", tags=["m2-5"])
        project_roots.append(root)

    hub_path = tmp_path / ".sage-hub.yaml"
    cfg = hub_config.init(hub_path)
    for root in project_roots:
        cfg = hub_config.add_project(cfg, root.name, root)
    hub_config.save(cfg, hub_path)

    # Point the lifespan + wrapper at our fixture-managed hub config.
    monkeypatch.setattr(hub_config, "DEFAULT_HUB_PATH", hub_path)

    yield hub_path, project_roots

    close_all()
    set_embedder(LocalEmbedder())


# ─── (a) Tool description + schema vary with --hub ────────────────


def test_search_tool_advertises_hub_projects_when_hub_enabled():
    mcp_with_hub = build_mcp_app(hub_enabled=True)
    mcp_no_hub = build_mcp_app(hub_enabled=False)

    async def _scenario():
        listed_hub = await mcp_with_hub.list_tools()
        listed_no_hub = await mcp_no_hub.list_tools()
        return listed_hub, listed_no_hub

    listed_hub, listed_no_hub = asyncio.run(_scenario())

    def _find(tools, name):
        return next(t for t in tools if t.name == name)

    search_hub = _find(listed_hub, "sage_memory_search")
    search_no_hub = _find(listed_no_hub, "sage_memory_search")

    assert "hub_projects" in search_hub.description, (
        f"--hub description must mention hub_projects; got: "
        f"{search_hub.description!r}"
    )
    assert "hub_projects" not in search_no_hub.description, (
        f"non-hub description must NOT mention hub_projects; got: "
        f"{search_no_hub.description!r}"
    )
    # Schema delta
    assert "hub_projects" in (
        search_hub.inputSchema.get("properties") or {}
    )
    assert "hub_projects" not in (
        search_no_hub.inputSchema.get("properties") or {}
    )


# ─── (b) Without --hub → hub_projects silently ignored ────────────


def test_hub_projects_param_ignored_without_hub_flag(
    hub_with_two_projects,
):
    """When the server runs WITHOUT --hub, passing hub_projects must
    not crash — it's silently dropped before the per-project handler
    runs. The handler receives only its declared kwargs."""
    hub_path, roots = hub_with_two_projects
    # Activate one project so the regular search has a DB to consult.
    close_all()
    override_project_root(roots[0])
    _open(get_project_db_path(roots[0]))

    mcp = build_mcp_app(hub_enabled=False)

    async def _scenario():
        result = await mcp.call_tool(
            "sage_memory_search",
            {"query": "shipping", "hub_projects": ["alpha", "beta"]},
        )
        return result

    result = asyncio.run(_scenario())
    envelope = json.loads(result[0].text)
    # Regular per-project search returns the standard envelope shape.
    assert "results" in envelope or "error" in envelope, (
        f"unexpected envelope: {envelope!r}"
    )
    assert "error" not in envelope, (
        f"hub_projects without --hub must NOT raise; got: {envelope!r}"
    )


# ─── (c) With --hub + hub_projects → fan-out fired ────────────────


def test_hub_projects_with_hub_flag_fans_out(hub_with_two_projects):
    hub_path, _roots = hub_with_two_projects
    mcp = build_mcp_app(hub_enabled=True)

    async def _scenario():
        result = await mcp.call_tool(
            "sage_memory_search",
            {"query": "shipping", "hub_projects": ["alpha", "beta"]},
        )
        return result

    result = asyncio.run(_scenario())
    envelope = json.loads(result[0].text)
    assert "results" in envelope, (
        f"hub fan-out envelope missing 'results': {envelope!r}"
    )
    sources = {r["source"] for r in envelope["results"]}
    # Both projects contributed.
    assert "alpha" in sources and "beta" in sources, (
        f"fan-out did not hit both projects; sources={sources!r}"
    )


# ─── (d) Invalid hub project name → error envelope ────────────────


def test_non_list_hub_projects_returns_clear_error_envelope(
    hub_with_two_projects,
):
    """Regression for /review M2 MAJOR #3: the passthrough metadata
    skips Pydantic validation, so a malformed payload (string instead
    of list) needs an explicit type guard. Without it, fan_out_search
    runs ``set("ops")`` and produces a baffling error about characters
    being unknown projects. With the guard, the user sees a clear
    "must be a list of strings" envelope."""
    mcp = build_mcp_app(hub_enabled=True)

    async def _scenario():
        return await mcp.call_tool(
            "sage_memory_search",
            {"query": "shipping", "hub_projects": "ops"},  # string, not list
        )

    result = asyncio.run(_scenario())
    envelope = json.loads(result[0].text)
    assert "error" in envelope, (
        f"non-list hub_projects must return error envelope; got: {envelope!r}"
    )
    assert "list" in envelope["error"].lower(), (
        f"error must mention list-type requirement; got: {envelope!r}"
    )


def test_invalid_hub_project_name_returns_error_envelope(
    hub_with_two_projects,
):
    """fan_out_search raises ValueError for unknown project names;
    the dispatch wrapper catches Exception and returns the pre-migration
    ``{"error": str(e)}`` envelope shape (not a ToolError that becomes
    isError=True)."""
    mcp = build_mcp_app(hub_enabled=True)

    async def _scenario():
        return await mcp.call_tool(
            "sage_memory_search",
            {"query": "shipping", "hub_projects": ["nonexistent-project"]},
        )

    result = asyncio.run(_scenario())
    envelope = json.loads(result[0].text)
    assert "error" in envelope, (
        f"invalid hub project must return error envelope; got: {envelope!r}"
    )
    assert "nonexistent-project" in envelope["error"], (
        f"error must name the offending project; got: {envelope!r}"
    )
