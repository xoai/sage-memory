"""Task 7 — Adapter: gemini (GEMINI.md).

Gemini CLI consumes `GEMINI.md` with the same marker-block convention
used by codex/opencode for AGENTS.md. Adapter shares the renderer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sage_memory.install_skills import Status
from sage_memory.install_skills.agent_gemini import GeminiAdapter


REPO = Path(__file__).resolve().parent.parent
BUNDLED_MEMORY = REPO / "src" / "sage_memory" / "skills" / "memory"


@pytest.fixture
def adapter():
    return GeminiAdapter()


def test_fresh_install_creates_gemini_md(adapter, tmp_path):
    target = tmp_path / "GEMINI.md"
    results = adapter.install_to(
        target=target, skill_name="memory", skill_dir=BUNDLED_MEMORY,
        version="0.8.0", dry_run=False, yes=False,
    )
    assert target.is_file()
    text = target.read_text()
    assert "<!-- sage-memory:skill:memory:begin -->" in text
    assert results[0].status == Status.CREATED


def test_idempotent_reinstall(adapter, tmp_path):
    target = tmp_path / "GEMINI.md"
    adapter.install_to(target=target, skill_name="memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=False, yes=False)
    results = adapter.install_to(target=target, skill_name="memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=False, yes=False)
    assert results[0].status == Status.UNCHANGED


def test_self_registers():
    from sage_memory import cli_install_skills as cli
    assert "gemini" in cli._ADAPTERS
