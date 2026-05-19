"""Task 9 — Golden-file help text + CHANGELOG entry checks.

Whitespace-sensitive byte-exact match for the `--help` output, and
substantive content checks on the `[0.8.0]` CHANGELOG entry. Distinct
from `test_install_skills_cli.py::test_no_args_prints_help_exit_0`,
which only does a loose "contains 'install-skills'" smoke check —
this file asserts the full help text matches a tracked golden.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from sage_memory.cli_install_skills import run_install_skills


REPO = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "golden" / "install-skills-help.txt"


def test_help_text_matches_golden(capsys):
    """`sage-memory install-skills --help` output is byte-exact against
    `tests/golden/install-skills-help.txt`. When intentional UX changes
    are made, regenerate the golden via:

        sage-memory install-skills --help > tests/golden/install-skills-help.txt
    """
    run_install_skills(["--help"])
    captured = capsys.readouterr().out
    expected = GOLDEN.read_text()
    assert captured == expected, (
        "help text drifted from golden. If this is intentional, regenerate:\n"
        "  sage-memory install-skills --help > tests/golden/install-skills-help.txt\n"
        f"\n--- diff (captured vs golden) ---\n"
        f"captured len={len(captured)} golden len={len(expected)}"
    )


def test_changelog_has_080_entry():
    """CHANGELOG must have a `[0.8.0]` entry that mentions both the new
    CLI and the skills/ path move."""
    content = (REPO / "CHANGELOG.md").read_text()
    assert "## [0.8.0]" in content, "CHANGELOG missing 0.8.0 entry"
    section = content.split("## [0.8.0]", 1)[1].split("\n## ", 1)[0]
    assert "install-skills" in section
    assert "src/sage_memory/skills" in section, (
        "0.8.0 must call out the skills/ path move per plan §Risks fallback"
    )


def test_pyproject_version_is_080():
    text = (REPO / "pyproject.toml").read_text()
    assert 'version = "0.8.0"' in text


def test_readme_has_install_skills_section():
    """README must document the new CLI."""
    text = (REPO / "README.md").read_text()
    assert "install-skills" in text
    assert "Installing skills into your agent" in text or (
        "### Installing skills" in text
    )
