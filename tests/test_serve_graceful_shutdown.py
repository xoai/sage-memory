"""M1.7 — graceful shutdown verification.

Spawns ``sage-memory serve --transport sse --port <random>``, sends
a real ``sage_memory_search`` call to confirm the server is fully
warm, then delivers SIGTERM and asserts:

  1. Process exits within 30s (uvicorn's bumped graceful-shutdown
     timeout per spec.md §"Graceful shutdown").
  2. The lifespan's ``__aexit__`` ``finally`` block ran ``flush_all_access``
     and ``close_all`` — observable via the ``shutdown: ...`` log
     lines added in server_fastmcp.server_lifespan.

Closes spec.md §"Graceful shutdown" promise. M1 has no ownership
asyncio.Tasks yet (those land in M3), so the shutdown ordering this
test exercises is the DB+access-flush path; M3 will extend the same
shutdown contract to heartbeat-task cancellation.
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


_GRACEFUL_DEADLINE_SECONDS = 30.0


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


def test_sigterm_triggers_lifespan_cleanup_under_deadline(tmp_path: Path):
    """SIGTERM → uvicorn graceful drain → lifespan finally → clean exit
    within 30s, with `shutdown:` log markers on stderr."""
    (tmp_path / ".git").mkdir()
    port = _free_port()
    host = "127.0.0.1"
    trace_file = tmp_path / "shutdown-trace.log"
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
        env={**os.environ, "SAGE_SHUTDOWN_TRACE": str(trace_file)},
    )

    try:
        _wait_for_port(host, port)
        url = f"http://{host}:{port}/sse"

        async def _warmup() -> None:
            # An actual search call ensures the lifespan + tool path
            # are both fully warm before SIGTERM arrives.
            async with sse_client(url) as (read, write):
                async with ClientSession(
                    read, write,
                    read_timeout_seconds=timedelta(seconds=30),
                ) as session:
                    await session.initialize()
                    await session.call_tool(
                        "sage_memory_set_project", {"path": str(tmp_path)},
                    )
                    res = await session.call_tool(
                        "sage_memory_search",
                        {"query": "graceful shutdown warmup", "limit": 1},
                    )
                    # Returning means the request landed; envelope shape
                    # doesn't matter for this test.
                    assert res.content

        asyncio.run(_warmup())

        # Now SIGTERM. Time the exit; uvicorn must drain + lifespan
        # finally must complete inside the deadline.
        send_time = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=_GRACEFUL_DEADLINE_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(
                f"server did not exit within "
                f"{_GRACEFUL_DEADLINE_SECONDS}s of SIGTERM"
            )
        elapsed = time.monotonic() - send_time

        # SIGTERM-on-Linux convention: process exit code reflects either
        # 0 (clean) or 130/143 (signal-handled). All three are accepted
        # graceful-shutdown outcomes; SIGKILL (-9 / 137) is NOT.
        assert proc.returncode in (0, -signal.SIGTERM, 143), (
            f"unexpected exit code: {proc.returncode!r} "
            f"(elapsed={elapsed:.2f}s)"
        )

        # The lifespan finally block writes one line per stage to
        # SAGE_SHUTDOWN_TRACE (set via env above). Reading that file
        # is independent of uvicorn's logging config — strictly more
        # reliable than parsing piped stderr.
        assert trace_file.exists(), (
            f"shutdown trace file not created; elapsed={elapsed:.2f}s"
        )
        stages = trace_file.read_text().splitlines()
        assert "flush_all_access" in stages, (
            f"flush_all_access stage missing from trace; "
            f"elapsed={elapsed:.2f}s; stages={stages!r}"
        )
        assert "close_all" in stages, (
            f"close_all stage missing from trace; "
            f"elapsed={elapsed:.2f}s; stages={stages!r}"
        )

    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
