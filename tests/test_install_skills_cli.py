"""Task 3 — CLI parser + dispatch wiring tests.

Smoke tests for `sage-memory install-skills` parsing and validation.
Adapters land per task (4-7) — these tests use lazy-import behavior
to verify the dispatch fails cleanly when no adapter is registered.

The detailed E2E coverage (with real adapter installs) lives in
test_install_skills_e2e.py (Task 8b). This file is parser-only.
"""

from __future__ import annotations

import sys

import pytest

from sage_memory.cli_install_skills import run_install_skills


def _run(argv, capsys):
    code = run_install_skills(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_no_args_prints_help_exit_0(capsys):
    code, stdout, stderr = _run([], capsys)
    assert code == 0
    assert "install-skills" in stdout
    assert "--project" in stdout
    assert "--global" in stdout


def test_help_flag_prints_help_exit_0(capsys):
    code, stdout, _ = _run(["--help"], capsys)
    assert code == 0
    assert "install-skills" in stdout


def test_dash_h_alias_prints_help_exit_0(capsys):
    code, stdout, _ = _run(["-h"], capsys)
    assert code == 0
    assert "install-skills" in stdout


def test_missing_scope_flag_exits_1(capsys):
    code, _, stderr = _run(["claude-code"], capsys)
    assert code == 1
    assert "--project" in stderr or "--global" in stderr


def test_both_scope_flags_mutually_exclusive(capsys):
    code, _, stderr = _run(
        ["claude-code", "--project", "--global"], capsys,
    )
    assert code == 1
    assert (
        "mutually exclusive" in stderr.lower()
        or "not allowed" in stderr.lower()
    )


def test_unknown_agent_exits_1(capsys):
    code, _, stderr = _run(["nonsense", "--project"], capsys)
    assert code == 1
    # argparse may say "invalid choice" or our message says "unknown agent"
    assert "invalid" in stderr.lower() or "unknown" in stderr.lower()


def test_unknown_skill_filter_exits_1(capsys):
    code, _, stderr = _run(
        ["claude-code", "--project", "--skill", "nonsense"], capsys,
    )
    assert code == 1


def test_known_agent_without_adapter_emits_error(capsys, monkeypatch, tmp_path):
    """Until Tasks 4-7 land, the registry is empty. The CLI should
    surface a clear 'no adapter registered' error rather than crash."""
    monkeypatch.chdir(tmp_path)
    # Empty the adapter registry to simulate "before Task 4"
    import sage_memory.cli_install_skills as cli
    monkeypatch.setattr(cli, "_ADAPTERS", {})
    code, stdout, stderr = _run(["claude-code", "--project"], capsys)
    assert code == 1
    assert "no adapter registered" in stderr.lower()


def test_dispatch_wires_install_skills_subcommand(capsys, monkeypatch):
    """`python -m sage_memory install-skills --help` reaches the
    subcommand dispatch (vs. printing top-level help)."""
    monkeypatch.setattr(sys, "argv", ["sage-memory", "install-skills", "--help"])
    from sage_memory import main
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "install-skills" in out
