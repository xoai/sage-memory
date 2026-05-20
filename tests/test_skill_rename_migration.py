"""Task 6 — 0.10.0 marker-block migration tests.

When a user upgrades from 0.9.0 / 0.8.0 to 0.10.0 and re-runs
`sage-memory install-skills <agent>`, any existing AGENTS.md /
GEMINI.md blocks named with the legacy bare identifiers (`memory`,
`ontology`, `self-learning`) are detected and removed before the new
prefixed-name block (`sage-memory`, etc.) is written. Migration is
invisible to the user.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sage_memory.install_skills import markers
from sage_memory.install_skills.agent_codex import CodexAdapter


REPO = Path(__file__).resolve().parent.parent
BUNDLED_MEMORY = (
    REPO / "src" / "sage_memory" / "skills" / "sage-memory"
)
BUNDLED_ONTOLOGY = (
    REPO / "src" / "sage_memory" / "skills" / "sage-ontology"
)


# ───── markers.delete_block_by_name ─────

def test_delete_block_by_name_removes_when_present():
    text = (
        "<!-- sage-memory:skill:memory:begin -->\n"
        "<!-- sage-memory version: 0.9.0 -->\n"
        "body content\n"
        "<!-- sage-memory:skill:memory:end -->\n"
    )
    out = markers.delete_block_by_name(text, "memory")
    assert "<!-- sage-memory:skill:memory:" not in out
    assert "body content" not in out


def test_delete_block_by_name_no_op_when_absent():
    text = "some unrelated content\n"
    assert markers.delete_block_by_name(text, "memory") == text


def test_delete_block_by_name_preserves_outside_content():
    text = (
        "before content\n\n"
        "<!-- sage-memory:skill:memory:begin -->\n"
        "<!-- sage-memory version: 0.9.0 -->\n"
        "body\n"
        "<!-- sage-memory:skill:memory:end -->\n\n"
        "after content\n"
    )
    out = markers.delete_block_by_name(text, "memory")
    assert "before content" in out
    assert "after content" in out
    assert "body" not in out


def test_delete_block_by_name_block_at_start_of_file():
    """File starts with the block, no preceding content."""
    text = (
        "<!-- sage-memory:skill:memory:begin -->\n"
        "<!-- sage-memory version: 0.9.0 -->\n"
        "body\n"
        "<!-- sage-memory:skill:memory:end -->\n\n"
        "after\n"
    )
    out = markers.delete_block_by_name(text, "memory")
    assert out == "after\n"


def test_delete_block_by_name_collapses_blank_line_padding():
    """Block surrounded by `\\n\\n` on each side → single `\\n\\n`
    between adjacent content, NOT `\\n\\n\\n`."""
    text = (
        "before\n\n"
        "<!-- sage-memory:skill:memory:begin -->\n"
        "<!-- sage-memory version: 0.9.0 -->\n"
        "body\n"
        "<!-- sage-memory:skill:memory:end -->\n\n"
        "after\n"
    )
    out = markers.delete_block_by_name(text, "memory")
    # Single blank line between "before" and "after"
    assert "before\n\nafter\n" in out


# ───── Adapter-level migration ─────

@pytest.fixture
def codex_adapter():
    return CodexAdapter()


def test_legacy_block_replaced_on_reinstall(codex_adapter, tmp_path):
    """User on 0.9.0 had a `memory` block in AGENTS.md. After upgrade
    to 0.10.0, re-running install for `sage-memory` removes the old
    block and writes the new-named block in its place."""
    target = tmp_path / "AGENTS.md"
    # Plant a legacy 0.9.0-shaped block
    target.write_text(
        "# User AGENTS.md\n\n"
        "<!-- sage-memory:skill:memory:begin -->\n"
        "<!-- sage-memory version: 0.9.0 -->\n"
        "old skill content\n"
        "<!-- sage-memory:skill:memory:end -->\n\n"
        "## User notes\n"
    )
    codex_adapter.install_to(
        target=target, skill_name="sage-memory", skill_dir=BUNDLED_MEMORY,
        version="0.10.0", dry_run=False, yes=False,
    )
    text = target.read_text()
    # Old-named markers are gone
    assert "<!-- sage-memory:skill:memory:begin -->" not in text
    assert "<!-- sage-memory:skill:memory:end -->" not in text
    # New-named block is present
    assert "<!-- sage-memory:skill:sage-memory:begin -->" in text
    assert "<!-- sage-memory:skill:sage-memory:end -->" in text
    # User content (before + after the original block) preserved
    assert "# User AGENTS.md" in text
    assert "## User notes" in text


def test_multiple_legacy_blocks_all_removed(codex_adapter, tmp_path):
    """If a user manually duplicated a legacy block (rare but
    possible), all duplicates are removed on re-install."""
    target = tmp_path / "AGENTS.md"
    legacy_block = (
        "<!-- sage-memory:skill:memory:begin -->\n"
        "<!-- sage-memory version: 0.9.0 -->\n"
        "old\n"
        "<!-- sage-memory:skill:memory:end -->\n"
    )
    target.write_text(
        f"before\n\n{legacy_block}\nmiddle\n\n{legacy_block}\nafter\n"
    )
    codex_adapter.install_to(
        target=target, skill_name="sage-memory", skill_dir=BUNDLED_MEMORY,
        version="0.10.0", dry_run=False, yes=False,
    )
    text = target.read_text()
    # ALL legacy markers gone — no orphan
    assert "<!-- sage-memory:skill:memory:begin -->" not in text
    assert text.count("<!-- sage-memory:skill:memory:end -->") == 0
    # User content between the legacy blocks preserved
    assert "before" in text
    assert "middle" in text
    assert "after" in text


def test_legacy_and_new_blocks_both_present(codex_adapter, tmp_path):
    """If a file has BOTH a legacy block AND a new-named block (e.g.,
    user installed 0.10.0 once already, then somehow re-introduced the
    legacy block), re-install removes the legacy and refreshes the new."""
    target = tmp_path / "AGENTS.md"
    # Use a different content for the existing new-named block so the
    # "find_block & bodies_equal" path triggers an overwrite (rather
    # than UNCHANGED).
    target.write_text(
        "<!-- sage-memory:skill:memory:begin -->\n"
        "<!-- sage-memory version: 0.9.0 -->\n"
        "legacy body\n"
        "<!-- sage-memory:skill:memory:end -->\n\n"
        "<!-- sage-memory:skill:sage-memory:begin -->\n"
        "<!-- sage-memory version: 0.10.0 -->\n"
        "stale new-named body\n"
        "<!-- sage-memory:skill:sage-memory:end -->\n"
    )
    codex_adapter.install_to(
        target=target, skill_name="sage-memory", skill_dir=BUNDLED_MEMORY,
        version="0.10.0", dry_run=False, yes=True,
    )
    text = target.read_text()
    assert "<!-- sage-memory:skill:memory:begin -->" not in text
    assert "<!-- sage-memory:skill:sage-memory:begin -->" in text
    assert "legacy body" not in text
    assert "stale new-named body" not in text  # refreshed


def test_migration_no_op_when_no_legacy_block(codex_adapter, tmp_path):
    """A file without any legacy block — fresh user, never had 0.9.0
    installed — install proceeds normally with no migration scrub
    side-effects."""
    target = tmp_path / "AGENTS.md"
    target.write_text("# Brand new AGENTS.md\n")
    codex_adapter.install_to(
        target=target, skill_name="sage-memory", skill_dir=BUNDLED_MEMORY,
        version="0.10.0", dry_run=False, yes=False,
    )
    text = target.read_text()
    assert "# Brand new AGENTS.md" in text
    assert "<!-- sage-memory:skill:sage-memory:begin -->" in text


def test_migration_applies_to_each_legacy_skill_independently(
    codex_adapter, tmp_path,
):
    """Migration is per-skill: install for `sage-memory` removes only
    `memory` legacy blocks; a `ontology` legacy block (if present)
    stays until that skill's install runs."""
    target = tmp_path / "AGENTS.md"
    target.write_text(
        "<!-- sage-memory:skill:memory:begin -->\n"
        "<!-- sage-memory version: 0.9.0 -->\n"
        "memory body\n"
        "<!-- sage-memory:skill:memory:end -->\n\n"
        "<!-- sage-memory:skill:ontology:begin -->\n"
        "<!-- sage-memory version: 0.9.0 -->\n"
        "ontology body\n"
        "<!-- sage-memory:skill:ontology:end -->\n"
    )
    codex_adapter.install_to(
        target=target, skill_name="sage-memory", skill_dir=BUNDLED_MEMORY,
        version="0.10.0", dry_run=False, yes=False,
    )
    text = target.read_text()
    # memory legacy gone, ontology legacy stays
    assert "<!-- sage-memory:skill:memory:begin -->" not in text
    assert "<!-- sage-memory:skill:ontology:begin -->" in text
