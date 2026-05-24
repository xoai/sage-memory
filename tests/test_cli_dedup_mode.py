"""T4 — `sage-memory dedup --mode {entity,memory}` CLI flag.

`--mode entity` (default) preserves exact existing M5 behavior;
`--mode memory` short-circuits to a forward-only stub message
documenting that memory-level dedup runs synchronously in
`sage_memory_store` (the actual feature this cycle ships), with
a future `--backfill` flag as follow-on.

Dispatch ordering: the mode-memory short-circuit MUST run BEFORE
the existing `--provider stub` + `--sync` validation so conflicting
combinations print the forward-only message rather than exiting 2.
"""

from __future__ import annotations

import os
import sys
from io import StringIO

import pytest

from sage_memory.cli_dedup import _parse_flags, run_dedup


# ─── (1) --mode entity matches default behavior ────────────────────


def test_mode_entity_matches_default_behavior(tmp_path, monkeypatch, capsys):
    """With LLM key set and a project DB, `dedup --mode entity` and
    `dedup` (no flag) both enqueue a task and print its id."""
    from sage_memory.db import (
        _open, close_all, get_project_db_path, override_project_root,
    )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    (tmp_path / ".git").mkdir()
    close_all()
    override_project_root(tmp_path)
    _open(get_project_db_path(tmp_path))

    try:
        rc1 = run_dedup([])
        out1 = capsys.readouterr().out
        rc2 = run_dedup(["--mode", "entity"])
        out2 = capsys.readouterr().out
    finally:
        close_all()

    assert rc1 == 0 and rc2 == 0
    # Both invocations enqueue (the second hits the at-most-one
    # contract, so the wording differs: one says "enqueued task",
    # the other says "already pending"). Both succeed (rc=0); both
    # print a task id. Behavior is consistent — `--mode entity`
    # took the same dispatch path as the default.
    assert "task" in out1.lower()
    assert "task" in out2.lower()


# ─── (2) --mode memory short-circuits + prints forward-only stub ──


def test_mode_memory_short_circuits_with_stub_message(monkeypatch, capsys):
    """`--mode memory` exits 0 and prints the forward-only message.
    Crucially, NO LLM key is set in env — proves the short-circuit
    fires BEFORE the LLM-key gate at cli_dedup.py:99."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    rc = run_dedup(["--mode", "memory"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "forward-only in 0.12.0" in out
    assert "--backfill" in out


# ─── (2a) --mode memory wins over conflicting --provider stub ─────


def test_mode_memory_overrides_provider_stub_without_sync(monkeypatch, capsys):
    """`--mode memory --provider stub` would normally exit 2 for the
    "stub requires --sync" rule at cli_dedup.py:78. Instead, the
    mode-memory short-circuit fires first → exit 0 with the
    forward-only message. Validates dispatch ordering."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    rc = run_dedup(["--mode", "memory", "--provider", "stub"])
    out = capsys.readouterr().out

    assert rc == 0, "mode-memory short-circuit did NOT win over --provider/sync validation"
    assert "forward-only in 0.12.0" in out


# ─── (3) --mode <invalid> exits with hand-rolled error ────────────


def test_mode_invalid_exits_with_hand_rolled_error(capsys):
    """Hand-rolled parser rejects unknown mode values."""
    rc = run_dedup(["--mode", "bogus"])
    captured = capsys.readouterr()

    assert rc == 2
    assert "must be 'entity' or 'memory'" in captured.err


# ─── (4) --help documents both modes ──────────────────────────────


def test_help_documents_both_modes(capsys):
    rc = run_dedup(["--help"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "entity" in out
    assert "memory" in out


# ─── Parser-level: --mode requires a value ────────────────────────


def test_mode_without_value_fails(capsys):
    """`--mode` with no following argument is a parse error."""
    flags = _parse_flags(["--mode"])
    assert flags is None
    captured = capsys.readouterr()
    assert "--mode requires a value" in captured.err
