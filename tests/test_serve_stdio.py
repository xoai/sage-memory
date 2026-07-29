"""M1.3 — stdio transport regression test.

Spawns the installed ``sage-memory`` entry point as a subprocess and
speaks MCP over its stdin/stdout using the canonical ``mcp.client.stdio``
client. Validates that the FastMCP-dispatched stdio path (post-M1.1b)
produces the same handshake + tool-envelope shape that pre-migration
0.12.x served.

Two tests per plan.md M1.3:
  (a) stdio handshake completes — initialize + tools/list
  (b) sage_memory_store + sage_memory_search round-trip via stdio
      lands the new memory and surfaces it back through search

Uses a per-test tmp project root (cwd) so writes land in
``tmp_path/.sage-memory/sage.db`` and disappear with the fixture
teardown.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from mcp import ClientSession
from mcp.client.stdio import (
    StdioServerParameters,
    get_default_environment,
    stdio_client,
)
from mcp.types import LATEST_PROTOCOL_VERSION


def _server_params(cwd: Path) -> StdioServerParameters:
    # `python -m sage_memory` invokes __main__.py which calls main(),
    # matching the installed `sage-memory` entry point exactly. Using
    # sys.executable keeps the venv consistent with the test runtime.
    #
    # HOME is pointed at an isolated subdir so the subprocess's GLOBAL
    # DB (~/.sage-memory/sage.db) is fresh: sage_memory_search's default
    # scope searches project + global (search.py:get_all_dbs), and a
    # developer's real global DB fills limit=5 with unrelated memories,
    # outranking the freshly stored one (fails locally, passes in CI).
    # The subdir (not tmp_path itself) avoids db.set_project's
    # "cannot set home directory as project root" guard and the
    # cwd-IS-home project-detection guard.
    fake_home = cwd / "fake-home"
    fake_home.mkdir(exist_ok=True)
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "sage_memory"],
        cwd=str(cwd),
        env={**get_default_environment(), "HOME": str(fake_home)},
    )


@pytest.fixture
def stdio_project_root(tmp_path: Path) -> Path:
    """Tmp project root: marker dir so sage-memory's project detection
    settles on tmp_path. The subprocess opens the DB at
    tmp_path/.sage-memory/sage.db automatically."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_stdio_handshake_completes(stdio_project_root):
    """initialize → serverInfo says 'sage-memory' + tools/list returns
    the 9 sage_memory_* tools with their hand-crafted names."""

    async def _scenario() -> None:
        async with stdio_client(_server_params(stdio_project_root)) as (
            read, write,
        ):
            async with ClientSession(
                read, write,
                read_timeout_seconds=timedelta(seconds=30),
            ) as session:
                init = await session.initialize()
                # Server's protocolVersion tracks the MCP SDK version
                # the server is built against; LATEST_PROTOCOL_VERSION
                # from the same SDK is the canonical match.
                assert init.protocolVersion == LATEST_PROTOCOL_VERSION
                assert init.serverInfo.name == "sage-memory"

                listed = await session.list_tools()
                names = {t.name for t in listed.tools}
                # All 9 hand-crafted TOOLS surface verbatim through the
                # FastMCP-dispatched stdio path.
                expected = {
                    "sage_memory_set_project",
                    "sage_memory_store",
                    "sage_memory_search",
                    "sage_memory_update",
                    "sage_memory_delete",
                    "sage_memory_list",
                    "sage_memory_link",
                    "sage_memory_graph",
                    "sage_memory_scan_codebase",
                    # P2-1 (SM-CAP-01): additive code-graph tools
                    # (intentional 9 → 12 growth, invariant 4).
                    "sage_memory_code_path",
                    "sage_memory_code_affected",
                    "sage_memory_code_hubs",
                }
                assert names == expected, (
                    f"tools/list mismatch — missing: {expected - names}, "
                    f"unexpected: {names - expected}"
                )

    asyncio.run(_scenario())


def test_stdio_store_search_round_trip(stdio_project_root):
    """sage_memory_store writes a memory; sage_memory_search surfaces
    it back via the same stdio connection. Envelope shape matches the
    pre-migration contract (JSON-serialized dict inside the first
    TextContent)."""

    sentinel_title = "M1.3 stdio round-trip sentinel"
    sentinel_content = (
        "Verifies the FastMCP-dispatched stdio path round-trips a "
        "store + search call against a fresh tmp project DB."
    )

    async def _scenario() -> None:
        async with stdio_client(_server_params(stdio_project_root)) as (
            read, write,
        ):
            async with ClientSession(
                read, write,
                read_timeout_seconds=timedelta(seconds=60),
            ) as session:
                await session.initialize()

                # Pin the active project to the tmp root (avoids
                # falling back to the subprocess's resolved cwd).
                set_proj = await session.call_tool(
                    "sage_memory_set_project",
                    {"path": str(stdio_project_root)},
                )
                assert set_proj.content, "set_project must return content"
                set_envelope = json.loads(set_proj.content[0].text)
                # db.set_project returns {project, path, database, status}
                # — `status: "active"` is the success signal (no `success`
                # field on this envelope).
                assert set_envelope.get("status") == "active", (
                    f"set_project envelope: {set_envelope!r}"
                )

                # Store a memory. Envelope per store.py:186 is
                # {success, id, message, suggested_links}.
                store_result = await session.call_tool(
                    "sage_memory_store",
                    {
                        "content": sentinel_content,
                        "title": sentinel_title,
                        "tags": ["m1-3-regression"],
                    },
                )
                assert store_result.content
                store_envelope = json.loads(store_result.content[0].text)
                assert store_envelope.get("success") is True, (
                    f"store envelope: {store_envelope!r}"
                )
                memory_id = store_envelope.get("id")
                assert isinstance(memory_id, str) and memory_id, (
                    f"store envelope must include id; got {store_envelope!r}"
                )

                # Search surfaces it back. Envelope per search.py:438 is
                # {results, total, query, timings}.
                search_result = await session.call_tool(
                    "sage_memory_search",
                    {
                        "query": "stdio round-trip sentinel",
                        "limit": 5,
                    },
                )
                assert search_result.content
                search_envelope = json.loads(search_result.content[0].text)
                assert "results" in search_envelope, (
                    f"search envelope missing 'results': {search_envelope!r}"
                )
                results = search_envelope["results"]
                assert results, (
                    f"search must surface stored memory; envelope: "
                    f"{search_envelope!r}"
                )
                found = any(r.get("id") == memory_id for r in results)
                assert found, (
                    f"new memory {memory_id} not in search results; "
                    f"got {[r.get('id') for r in results]}"
                )

    asyncio.run(_scenario())


def test_stdio_envelope_includes_project_enrichment(stdio_project_root):
    """Regression for /review M1 CRITICAL #1: store/search/list/set_project
    envelopes must carry `_project` (current project name).

    Pre-M1.1b dispatch (server.py:572-577 of 0.12.x) added
    ``result["_project"] = get_project_name()`` after handler return
    for these four tools. The FastMCP-default dispatch path drops it
    silently. M1.1b's wrapper restores the field; this test pins it."""

    async def _scenario() -> None:
        async with stdio_client(_server_params(stdio_project_root)) as (
            read, write,
        ):
            async with ClientSession(
                read, write,
                read_timeout_seconds=timedelta(seconds=30),
            ) as session:
                await session.initialize()
                await session.call_tool(
                    "sage_memory_set_project",
                    {"path": str(stdio_project_root)},
                )
                store = await session.call_tool(
                    "sage_memory_store",
                    {
                        "content": (
                            "Verifies _project enrichment survives the "
                            "FastMCP dispatch wrapper."
                        ),
                        "title": "Project-enrichment regression",
                    },
                )
                env = json.loads(store.content[0].text)
                # The project name is derived from the project root's
                # directory name; we only assert presence + non-empty.
                assert "_project" in env, (
                    f"store envelope missing _project: {env!r}"
                )
                assert isinstance(env["_project"], str) and env["_project"], (
                    f"_project must be a non-empty string: {env!r}"
                )

                # Same check for sage_memory_search.
                search = await session.call_tool(
                    "sage_memory_search",
                    {"query": "Project-enrichment", "limit": 1},
                )
                search_env = json.loads(search.content[0].text)
                assert "_project" in search_env, (
                    f"search envelope missing _project: {search_env!r}"
                )

    asyncio.run(_scenario())
