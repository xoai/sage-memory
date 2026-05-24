"""M1.6 — /health endpoint smoke test.

GET /health on the SSE transport returns 200 with
``{"status": "ok", "version": <sage-memory version>}``. The route is
registered via ``FastMCP.custom_route`` in ``server_fastmcp.build_mcp_app``;
it's reachable on SSE + streamable-HTTP transports but absent from
stdio (no HTTP surface). Tested via SSE since that's the canonical
network transport per ADR-007.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from sage_memory import __version__ as _sage_version


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


def test_health_endpoint_returns_ok_envelope(tmp_path: Path):
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
        r = httpx.get(f"http://{host}:{port}/health", timeout=10.0)
        assert r.status_code == 200, (
            f"/health must return 200; got {r.status_code} body={r.text!r}"
        )
        payload = r.json()
        assert payload.get("status") == "ok", (
            f"/health envelope: {payload!r}"
        )
        assert payload.get("version") == _sage_version, (
            f"version mismatch: payload={payload!r} "
            f"sage_version={_sage_version!r}"
        )
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
