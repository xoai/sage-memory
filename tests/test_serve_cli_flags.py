"""M1.2 — `sage-memory serve` CLI flag parsing + dispatch wiring.

Covers the 5 contracts in plan.md (cycle 20260524-team-mcp-transports):
  1. Each of {--transport, --port, --host, --hub, --log-level} parses
     to its expected value.
  2. Invalid `--transport` exits 2 with a hand-rolled stderr message.
  3. Arg-less `run_serve([])` defaults to stdio.
  4. `--help` exits 0 with the usage text on stdout.
  5. `__init__.py:main()` dispatches the `serve` subcommand to
     ``cli_serve.run_serve`` so the network-transport surface is
     reachable from the installed entry point.
"""

from __future__ import annotations

import sys

import pytest


# ─── 1. Each flag valid value ─────────────────────────────────────


def test_serve_cli_parses_all_flags_to_expected_values():
    """Single combined argv exercises every flag's happy path."""
    from sage_memory.cli_serve import _parse_flags

    argv = [
        "--transport", "sse",
        "--port", "3334",
        "--host", "0.0.0.0",
        "--hub",
        "--log-level", "DEBUG",
    ]
    flags = _parse_flags(argv)
    assert flags is not None, "valid argv must parse"
    assert flags.transport == "sse"
    assert flags.port == 3334
    assert flags.host == "0.0.0.0"
    assert flags.hub is True
    assert flags.log_level == "DEBUG"


# ─── 2. Invalid transport → exit 2 ────────────────────────────────


def test_serve_cli_invalid_transport_exits_2(capsys):
    """Bogus --transport value: run_serve returns 2 with hand-rolled
    error on stderr (matches the cli_dedup pattern)."""
    from sage_memory.cli_serve import run_serve

    rc = run_serve(["--transport", "bogus"])
    captured = capsys.readouterr()
    assert rc == 2, f"expected exit 2, got {rc}; stderr={captured.err!r}"
    assert "transport" in captured.err.lower()
    assert "bogus" in captured.err


# ─── 3. Arg-less defaults to stdio ────────────────────────────────


def test_serve_cli_argless_defaults_to_stdio():
    """Empty argv → flags carry the stdio defaults from spec.md
    §"sage-memory serve" (transport=stdio, port=3333, host=127.0.0.1,
    hub=False, log_level=INFO)."""
    from sage_memory.cli_serve import _parse_flags

    flags = _parse_flags([])
    assert flags is not None
    assert flags.transport == "stdio"
    assert flags.port == 3333
    assert flags.host == "127.0.0.1"
    assert flags.hub is False
    assert flags.log_level == "INFO"


# ─── 4. --help exits 0 ────────────────────────────────────────────


def test_serve_cli_help_flag_exits_zero(capsys):
    """`run_serve(["--help"])` prints usage and returns 0 without
    touching the FastMCP factory."""
    from sage_memory.cli_serve import run_serve

    rc = run_serve(["--help"])
    captured = capsys.readouterr()
    assert rc == 0
    out = captured.out
    assert "sage-memory serve" in out
    assert "--transport" in out
    assert "--port" in out
    assert "--host" in out
    assert "--hub" in out
    assert "--log-level" in out


# ─── 5. Dispatch wiring (__init__.py:main → cli_serve.run_serve) ──


def test_dispatch_serve_subcommand_routes_to_run_serve(monkeypatch):
    """`sage-memory serve <argv>` from the installed entry point reaches
    `cli_serve.run_serve(argv)`. Backwards-compat: this is the on-ramp
    that makes `sage-memory serve --transport stdio` behaviorally
    equivalent to the arg-less invocation tested by
    `test_status_no_args_still_runs_server`."""
    import sage_memory.cli_serve as cli_serve_mod
    from sage_memory import main

    monkeypatch.setattr(
        "sys.argv", ["sage-memory", "serve", "--transport", "stdio"],
    )

    seen: dict[str, list[str]] = {}

    def _fake_run_serve(argv):
        seen["argv"] = list(argv)
        return 0

    monkeypatch.setattr(cli_serve_mod, "run_serve", _fake_run_serve)

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert seen == {"argv": ["--transport", "stdio"]}, (
        f"dispatch must hand argv after 'serve' to run_serve; got {seen}"
    )
