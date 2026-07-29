"""P0-3 — Transport security tests (SM-SEC-01/02/03).

Written tests-first per 04-spec-phase0-critical.md §P0-3: every test
in this module FAILS against pre-P0-3 code.

Covers:
  - refuse-start: non-loopback http/sse without a token (SM-SEC-01)
  - bearer auth: no header / wrong token / right token (SM-SEC-01)
  - Host allowlist + Origin validation → 403 (SM-SEC-02)
  - set_project scoping: allowed subtree, outside, sibling-prefix,
    sensitive dirs (SM-SEC-03)
  - stdio + loopback zero-config preserved (invariant 9)

Memory-informed cases (sage-wiki [LRN:gotcha] — same hardening task
on the sibling project): blank host binds ALL interfaces and must be
treated as non-loopback; the Host allowlist is a browser defense so
direct hostname/IP access needs an explicit --allowed-host (startup
hint asserted here).
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

from sage_memory import cli_serve
from sage_memory import db as _db


_TOKEN = "test-token-p0-3"


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


# ─── Loopback classification (unit) ───────────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_spellings_are_loopback(host):
    assert cli_serve._is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "", "::", "example.com", "10.0.0.5"])
def test_wildcard_blank_and_public_hosts_are_not_loopback(host):
    """Blank/wildcard bind = ALL interfaces — a memory-recorded gotcha
    from the sibling project's identical task: treating "" as loopback
    starts UNAUTHENTICATED on every interface."""
    assert cli_serve._is_loopback_host(host) is False


# ─── Refuse-start enforcement (unit, no sockets) ──────────────────


def _flags(**overrides):
    f = cli_serve._Flags()
    for k, v in overrides.items():
        setattr(f, k, v)
    return f


def test_non_loopback_without_token_refuses(monkeypatch):
    monkeypatch.delenv("SAGE_MEMORY_TOKEN", raising=False)
    err = cli_serve._enforce_transport_security(
        _flags(transport="sse", host="0.0.0.0"), token=None,
    )
    assert err is not None
    assert "--token" in err and "SAGE_MEMORY_TOKEN" in err


def test_non_loopback_with_token_passes():
    assert cli_serve._enforce_transport_security(
        _flags(transport="http", host="0.0.0.0"), token=_TOKEN,
    ) is None


def test_loopback_without_token_passes():
    """Zero-config local use is preserved (invariant 9)."""
    assert cli_serve._enforce_transport_security(
        _flags(transport="sse", host="127.0.0.1"), token=None,
    ) is None


def test_stdio_is_exempt_from_token_rule():
    """stdio is a pipe to a local parent process, not a network surface."""
    assert cli_serve._enforce_transport_security(
        _flags(transport="stdio", host="127.0.0.1"), token=None,
    ) is None


def test_env_token_wins_when_flag_empty(monkeypatch):
    monkeypatch.setenv("SAGE_MEMORY_TOKEN", _TOKEN)
    flags = _flags(token=None)
    assert cli_serve._resolve_token(flags) == _TOKEN
    flags = _flags(token="flag-token")
    assert cli_serve._resolve_token(flags) == "flag-token"


def test_allowed_hosts_from_flag_and_env(monkeypatch):
    monkeypatch.setenv("SAGE_ALLOWED_HOSTS", "env.example.com")
    flags = _flags(allowed_hosts=["flag.example.com"])
    hosts = cli_serve._resolve_allowed_hosts(flags)
    assert "flag.example.com" in hosts and "env.example.com" in hosts


# ─── Refuse-start end-to-end (subprocess) ─────────────────────────


def test_serve_non_loopback_no_token_exits_with_clear_error(tmp_path):
    (tmp_path / ".git").mkdir()
    proc = subprocess.run(
        [
            sys.executable, "-m", "sage_memory", "serve",
            "--transport", "sse", "--host", "0.0.0.0",
            "--port", str(_free_port()),
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        env={k: v for k, v in os.environ.items()
             if k != "SAGE_MEMORY_TOKEN"},
    )
    assert proc.returncode == 2, (
        f"expected exit 2; got {proc.returncode} "
        f"stderr={proc.stderr[-500:]!r}"
    )
    assert "--token" in proc.stderr and "SAGE_MEMORY_TOKEN" in proc.stderr


# ─── Live auth + Host/Origin middleware (subprocess, loopback+token)


@pytest.fixture
def secured_server(tmp_path):
    (tmp_path / ".git").mkdir()
    port = _free_port()
    host = "127.0.0.1"
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "sage_memory", "serve",
            "--transport", "sse", "--host", host, "--port", str(port),
            "--token", _TOKEN,
        ],
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ},
    )
    try:
        _wait_for_port(host, port)
        yield f"http://{host}:{port}"
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def test_no_authorization_header_returns_401(secured_server):
    r = httpx.get(f"{secured_server}/health", timeout=10.0)
    assert r.status_code == 401


def test_wrong_token_returns_401(secured_server):
    r = httpx.get(
        f"{secured_server}/health",
        headers={"Authorization": "Bearer wrong-token"},
        timeout=10.0,
    )
    assert r.status_code == 401


def test_right_token_returns_200(secured_server):
    r = httpx.get(
        f"{secured_server}/health",
        headers={"Authorization": f"Bearer {_TOKEN}"},
        timeout=10.0,
    )
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


def test_foreign_host_header_returns_403(secured_server):
    """DNS-rebinding defense: Host must be loopback or --allowed-host."""
    r = httpx.get(
        f"{secured_server}/health",
        headers={
            "Authorization": f"Bearer {_TOKEN}",
            "Host": "evil.com",
        },
        timeout=10.0,
    )
    assert r.status_code == 403


def test_cross_origin_header_returns_403(secured_server):
    r = httpx.get(
        f"{secured_server}/health",
        headers={
            "Authorization": f"Bearer {_TOKEN}",
            "Origin": "https://evil.com",
        },
        timeout=10.0,
    )
    assert r.status_code == 403


def test_loopback_origin_passes(secured_server):
    r = httpx.get(
        f"{secured_server}/health",
        headers={
            "Authorization": f"Bearer {_TOKEN}",
            "Origin": f"{secured_server}",
        },
        timeout=10.0,
    )
    assert r.status_code == 200


# ─── set_project scoping (SM-SEC-03) ──────────────────────────────


@pytest.fixture
def scoped_roots(tmp_path, monkeypatch):
    """Allowed root at tmp_path/'proj'; reset set_project state after."""
    monkeypatch.setattr(_db, "_active_project", None)
    monkeypatch.setattr(_db, "_active_project_set", False)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("SAGE_ALLOWED_ROOTS", str(proj))
    yield proj
    _db._active_project = None
    _db._active_project_set = False


def test_set_project_inside_allowed_root_ok(scoped_roots):
    sub = scoped_roots / "sub"
    sub.mkdir()
    result = _db.set_project(str(sub))
    assert result.get("status") == "active", f"envelope: {result!r}"


def test_set_project_outside_allowed_root_rejected(scoped_roots, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    result = _db.set_project(str(outside))
    assert "error" in result, f"envelope: {result!r}"


def test_set_project_sibling_prefix_rejected(scoped_roots, tmp_path):
    """Separator normalisation proof: /tmp/.../proj-secret shares the
    string prefix /tmp/.../proj but is NOT inside it. A str.startswith
    check would wrongly allow it."""
    sibling = tmp_path / "proj-secret"
    sibling.mkdir()
    result = _db.set_project(str(sibling))
    assert "error" in result, (
        f"sibling-prefix path must be rejected; envelope: {result!r}"
    )


def test_set_project_sensitive_dir_rejected_even_under_allowed_root(
    scoped_roots, monkeypatch, tmp_path
):
    """~/.ssh must be rejected even if SAGE_ALLOWED_ROOTS covers ~.
    Uses a fake HOME so the test is hermetic (CI has no ~/.ssh)."""
    fake_home = tmp_path / "fake-home"
    ssh = fake_home / ".ssh"
    ssh.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("SAGE_ALLOWED_ROOTS", str(fake_home))
    result = _db.set_project(str(ssh))
    assert "error" in result, (
        f"sensitive dir must be rejected; envelope: {result!r}"
    )
