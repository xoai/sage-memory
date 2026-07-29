"""M1.4 — SSE transport smoke test.

Spawns ``sage-memory serve --transport sse --port <random>`` as a
subprocess, connects via ``mcp.client.sse.sse_client``, and verifies
both the handshake and a tool call land end-to-end. This is the first
test that exercises the FastMCP-managed network transport (uvicorn
under the hood) rather than the stdio dispatch.

Two tests per plan.md M1.4:
  (a) connect + tools/list — 9 sage_memory_* tools surface verbatim
  (b) call sage_memory_store over SSE + verify response envelope

A random free port avoids CI contention. Port resolution races with
process startup, so the test polls the TCP socket until uvicorn is
listening (with a generous timeout for cold embedder bootstrap on
slow CI hosts).
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
from mcp.client.sse import sse_client


def _free_port() -> int:
    """Ask the OS for an available port — bind, read, close."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> None:
    """Block until ``host:port`` accepts TCP connections or timeout."""
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
def sse_server(tmp_path: Path):
    """Launch `sage-memory serve --transport sse` as a subprocess; tear
    down on exit. Yields the (host, port, project_root) tuple."""
    (tmp_path / ".git").mkdir()
    port = _free_port()
    host = "127.0.0.1"
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "sage_memory", "serve",
            "--transport", "sse",
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
        # SIGTERM triggers FastMCP's graceful-shutdown path (lifespan
        # __aexit__ → close_all + flush_all_access). give it 10s to
        # drain, then SIGKILL as last resort so a hung server doesn't
        # block the test suite.
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def test_sse_handshake_and_tools_list(sse_server):
    """SSE connect + initialize + tools/list surfaces all 9 tools
    with hand-crafted names (same set the stdio path serves)."""
    host, port, _ = sse_server
    url = f"http://{host}:{port}/sse"

    async def _scenario() -> None:
        async with sse_client(url) as (read, write):
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


def test_sse_bind_to_zero_dot_zero_works(tmp_path):
    """Regression for /review M1 MAJOR #2: ``--host 0.0.0.0`` must
    actually bind 0.0.0.0 and accept connections (spec.md M1 acceptance
    bullet "--host 0.0.0.0 works"). Previously asserted only at
    flag-parsing level, leaving a regression in the actual bind logic
    invisible. Client connects via 127.0.0.1 — same machine, different
    bind addr — so the test runs cleanly in any CI without external
    network access.

    P0-3 (SM-SEC-01): non-loopback binds now REQUIRE a token (the
    refuse-start rule); the client authenticates with it. The
    unauthenticated variant is covered by
    test_serve_security.py::test_serve_non_loopback_no_token_exits_with_clear_error.
    """
    import os
    import signal as _signal
    import subprocess as _sp

    (tmp_path / ".git").mkdir()
    port = _free_port()
    token = "test-token-0.0.0.0-bind"
    proc = _sp.Popen(
        [
            sys.executable, "-m", "sage_memory", "serve",
            "--transport", "sse",
            "--host", "0.0.0.0",
            "--port", str(port),
            "--token", token,
        ],
        cwd=str(tmp_path),
        stdout=_sp.DEVNULL,
        stderr=_sp.DEVNULL,
        env={**os.environ},
    )
    try:
        _wait_for_port("127.0.0.1", port)

        async def _scenario() -> None:
            async with sse_client(
                f"http://127.0.0.1:{port}/sse",
                headers={"Authorization": f"Bearer {token}"},
            ) as (
                read, write,
            ):
                async with ClientSession(
                    read, write,
                    read_timeout_seconds=timedelta(seconds=30),
                ) as session:
                    init = await session.initialize()
                    assert init.serverInfo.name == "sage-memory"

        asyncio.run(_scenario())
    finally:
        if proc.poll() is None:
            proc.send_signal(_signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except _sp.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def test_sse_store_call_round_trip(sse_server):
    """Call sage_memory_store over SSE; verify the envelope shape
    matches the pre-migration contract (success + id fields)."""
    host, port, project_root = sse_server
    url = f"http://{host}:{port}/sse"

    async def _scenario() -> None:
        async with sse_client(url) as (read, write):
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
                            "M1.4 SSE smoke: verify FastMCP-managed "
                            "network transport round-trips a tool call."
                        ),
                        "title": "M1.4 SSE smoke",
                        "tags": ["m1-4-regression"],
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
