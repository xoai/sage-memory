"""M1.5 — streamable-HTTP transport smoke test.

Mirrors the M1.4 SSE shape but talks to FastMCP's streamable-HTTP
endpoint (default mount: ``/mcp``) via
``mcp.client.streamable_http.streamable_http_client``. Both transports
share the same FastMCP factory + lifespan, so once SSE works the
HTTP path is a thin wrapper — this test guards against accidental
divergence (e.g., mount-path drift after an SDK upgrade).

Two tests per plan.md M1.5:
  (a) connect + tools/list
  (b) call sage_memory_store + verify response envelope
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except (ConnectionRefusedError, OSError) as e:
            last_err = e
            time.sleep(0.1)
    raise TimeoutError(
        f"server at {host}:{port} not reachable within {timeout}s "
        f"(last error: {last_err!r})"
    )


@pytest.fixture
def http_server(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    port = _free_port()
    host = "127.0.0.1"
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "sage_memory", "serve",
            "--transport", "http",
            "--host", host,
            "--port", str(port),
        ],
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ},
    )
    try:
        _wait_for_port(host, port)
        yield host, port, tmp_path
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def test_http_handshake_and_tools_list(http_server):
    """streamable-HTTP connect + initialize + tools/list returns the
    same 9 tools as stdio + SSE."""
    host, port, _ = http_server
    url = f"http://{host}:{port}/mcp"

    async def _scenario() -> None:
        async with streamable_http_client(url) as (read, write, _get_session):
            async with ClientSession(
                read, write,
                read_timeout_seconds=timedelta(seconds=30),
            ) as session:
                init = await session.initialize()
                assert init.serverInfo.name == "sage-memory"

                listed = await session.list_tools()
                names = {t.name for t in listed.tools}
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
                }
                assert names == expected, (
                    f"tools/list mismatch — missing: {expected - names}, "
                    f"unexpected: {names - expected}"
                )

    asyncio.run(_scenario())


def test_http_store_call_round_trip(http_server):
    """Call sage_memory_store over streamable-HTTP; envelope shape
    matches the pre-migration contract."""
    host, port, project_root = http_server
    url = f"http://{host}:{port}/mcp"

    async def _scenario() -> None:
        async with streamable_http_client(url) as (read, write, _get_session):
            async with ClientSession(
                read, write,
                read_timeout_seconds=timedelta(seconds=60),
            ) as session:
                await session.initialize()

                set_proj = await session.call_tool(
                    "sage_memory_set_project",
                    {"path": str(project_root)},
                )
                set_env = json.loads(set_proj.content[0].text)
                assert set_env.get("status") == "active", (
                    f"set_project envelope: {set_env!r}"
                )

                store_result = await session.call_tool(
                    "sage_memory_store",
                    {
                        "content": (
                            "M1.5 streamable-HTTP smoke: verify the "
                            "FastMCP /mcp endpoint round-trips a tool call."
                        ),
                        "title": "M1.5 HTTP smoke",
                        "tags": ["m1-5-regression"],
                    },
                )
                env = json.loads(store_result.content[0].text)
                assert env.get("success") is True, (
                    f"store envelope: {env!r}"
                )
                assert isinstance(env.get("id"), str) and env["id"], (
                    f"store envelope must include id; got {env!r}"
                )

    asyncio.run(_scenario())
