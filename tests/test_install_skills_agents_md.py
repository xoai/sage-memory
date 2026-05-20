"""Task 6 — Adapter: codex + opencode (shared AGENTS.md renderer).

Codex and OpenCode both consume `AGENTS.md`. The shared renderer
produces a marker-delimited block per skill that can be located and
replaced by re-installs without touching surrounding user content.
References-dir links inside the block are rewritten to absolute
resource paths pointing at the bundled skill files.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sage_memory.install_skills import FileResult, Status, markers
from sage_memory.install_skills.agent_codex import CodexAdapter
from sage_memory.install_skills.agent_opencode import OpenCodeAdapter
from sage_memory.install_skills.agents_markdown import render_block


REPO = Path(__file__).resolve().parent.parent
BUNDLED_MEMORY = REPO / "src" / "sage_memory" / "skills" / "sage-memory"
BUNDLED_ONTOLOGY = REPO / "src" / "sage_memory" / "skills" / "sage-ontology"


# ───── render_block (shared renderer) ─────

def test_render_block_includes_begin_end_markers():
    block = render_block(
        skill_name="sage-memory", version="0.8.0", skill_dir=BUNDLED_MEMORY,
    )
    assert "<!-- sage-memory:skill:sage-memory:begin -->" in block
    assert "<!-- sage-memory:skill:sage-memory:end -->" in block


def test_render_block_includes_version_line_inside_block():
    block = render_block(
        skill_name="sage-memory", version="0.8.0", skill_dir=BUNDLED_MEMORY,
    )
    assert "<!-- sage-memory version: 0.8.0 -->" in block


def test_render_block_strips_skill_md_frontmatter():
    block = render_block(
        skill_name="sage-memory", version="0.8.0", skill_dir=BUNDLED_MEMORY,
    )
    body = markers.extract_body(block, "sage-memory")
    # The body should not start with a YAML frontmatter block
    assert body is not None
    assert not body.lstrip().startswith("---")


def test_render_block_rewrites_references_links_to_absolute_paths():
    """Relative `references/foo.md` references (in markdown-link form
    or backtick-quoted form) must be replaced with absolute paths that
    resolve on disk."""
    block = render_block(
        skill_name="sage-memory", version="0.8.0", skill_dir=BUNDLED_MEMORY,
    )
    # No remaining relative refs in either form
    rel_links = re.findall(r"\]\(references/[^)]+\)", block)
    assert not rel_links, f"unrewritten markdown link refs: {rel_links}"
    rel_ticks = re.findall(r"`references/[^`]+`", block)
    assert not rel_ticks, f"unrewritten backtick refs: {rel_ticks}"

    # At least one absolute path to the bundled location appears in the
    # body (either as markdown link or as backtick code span)
    abs_pattern = re.escape(str(BUNDLED_MEMORY / "references"))
    assert re.search(rf"`{abs_pattern}/", block) or re.search(
        rf"\]\({abs_pattern}/", block
    ), f"expected at least one absolute path to {BUNDLED_MEMORY}/references/"


def test_render_block_has_bundled_resources_footer():
    block = render_block(
        skill_name="sage-memory", version="0.8.0", skill_dir=BUNDLED_MEMORY,
    )
    assert "Bundled resources" in block or "bundled resources" in block.lower()
    # Footer lists each reference file's absolute path
    refs_dir = BUNDLED_MEMORY / "references"
    for ref in refs_dir.iterdir():
        assert str(ref) in block, (
            f"footer must list {ref}"
        )


def test_path_traversal_links_are_not_rewritten(tmp_path):
    """A SKILL.md with `references/../../etc/passwd` must NOT have
    that path absolutized — the bundled skill_dir is the containment
    boundary."""
    fake_skill = tmp_path / "memory"
    (fake_skill / "references").mkdir(parents=True)
    (fake_skill / "references" / "good.md").write_text("legitimate ref")
    (fake_skill / "SKILL.md").write_text(
        "# Memory\n\n"
        "Body with [bad ref](references/../../../etc/passwd) and "
        "[good ref](references/good.md).\n"
    )
    block = render_block(
        skill_name="sage-memory", version="0.8.0", skill_dir=fake_skill,
    )
    # The good link IS rewritten to an absolute path
    assert "](references/good.md)" not in block
    assert str(fake_skill.resolve() / "references" / "good.md") in block
    # The original relative bad-link is preserved verbatim (NOT absolutized)
    assert "](references/../../../etc/passwd)" in block
    # CRITICAL: no markdown link or backtick code span points at an
    # absolute path matching /etc/passwd — the containment held.
    assert "](/etc/passwd)" not in block
    assert "`/etc/passwd`" not in block


def test_rewritten_link_targets_resolve_on_disk():
    """The absolute paths we wrote (markdown-link OR backtick form)
    must actually exist on disk."""
    block = render_block(
        skill_name="sage-memory", version="0.8.0", skill_dir=BUNDLED_MEMORY,
    )
    md_paths = re.findall(r"\]\((/[^)]+)\)", block)
    tick_paths = re.findall(r"`(/[^`]+)`", block)
    abs_paths = md_paths + tick_paths
    assert abs_paths, "expected at least one absolute path reference"
    for path_str in abs_paths:
        assert Path(path_str).exists(), f"rewritten path missing on disk: {path_str}"


# ───── CodexAdapter ─────

@pytest.fixture
def codex_adapter():
    return CodexAdapter()


def test_codex_fresh_install_creates_agents_md(codex_adapter, tmp_path):
    target = tmp_path / "AGENTS.md"
    results = codex_adapter.install_to(
        target=target, skill_name="sage-memory", skill_dir=BUNDLED_MEMORY,
        version="0.8.0", dry_run=False, yes=False,
    )
    assert target.is_file()
    text = target.read_text()
    assert "<!-- sage-memory:skill:sage-memory:begin -->" in text
    assert "<!-- sage-memory:skill:sage-memory:end -->" in text
    assert len(results) == 1
    assert results[0].status == Status.CREATED


def test_codex_appends_block_preserving_existing_content(codex_adapter, tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_text("# User's existing AGENTS.md\n\nSome rules here.\n")
    codex_adapter.install_to(
        target=target, skill_name="sage-memory", skill_dir=BUNDLED_MEMORY,
        version="0.8.0", dry_run=False, yes=False,
    )
    text = target.read_text()
    assert "User's existing AGENTS.md" in text
    assert "Some rules here." in text
    assert "<!-- sage-memory:skill:sage-memory:begin -->" in text


def test_codex_idempotent_reinstall(codex_adapter, tmp_path):
    target = tmp_path / "AGENTS.md"
    codex_adapter.install_to(target=target, skill_name="sage-memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=False, yes=False)
    results = codex_adapter.install_to(target=target, skill_name="sage-memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=False, yes=False)
    assert results[0].status == Status.UNCHANGED


def test_codex_version_bump_with_same_body_is_unchanged(codex_adapter, tmp_path):
    """Bumping sage-memory's version without changing the skill body
    must not trigger a diff prompt — version line is excluded from
    body-equality."""
    target = tmp_path / "AGENTS.md"
    codex_adapter.install_to(target=target, skill_name="sage-memory",
        skill_dir=BUNDLED_MEMORY, version="0.7.0", dry_run=False, yes=False)
    # Re-install with a different version string but identical body
    results = codex_adapter.install_to(target=target, skill_name="sage-memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=False, yes=False)
    assert results[0].status == Status.UNCHANGED, (
        f"version-only change should be UNCHANGED; got {results[0].status}"
    )


def test_codex_block_replace_preserves_surrounding_content(codex_adapter, tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_text(
        "before\n\n"
        "<!-- sage-memory:skill:sage-memory:begin -->\n"
        "<!-- sage-memory version: 0.7.0 -->\n"
        "old body content\n"
        "<!-- sage-memory:skill:sage-memory:end -->\n\n"
        "after\n"
    )
    codex_adapter.install_to(
        target=target, skill_name="sage-memory", skill_dir=BUNDLED_MEMORY,
        version="0.8.0", dry_run=False, yes=True,
    )
    text = target.read_text()
    assert text.startswith("before"), "before content preserved"
    assert "after" in text.splitlines()[-3:], "after content preserved"
    assert "old body content" not in text


def test_codex_three_skills_produce_three_blocks(codex_adapter, tmp_path):
    target = tmp_path / "AGENTS.md"
    for skill, dir_ in [
        ("sage-memory", BUNDLED_MEMORY),
        ("sage-ontology", BUNDLED_ONTOLOGY),
        ("sage-self-learning",
         REPO / "src" / "sage_memory" / "skills" / "sage-self-learning"),
    ]:
        codex_adapter.install_to(target=target, skill_name=skill,
            skill_dir=dir_, version="0.8.0", dry_run=False, yes=False)
    text = target.read_text()
    assert "<!-- sage-memory:skill:sage-memory:begin -->" in text
    assert "<!-- sage-memory:skill:sage-ontology:begin -->" in text
    assert "<!-- sage-memory:skill:sage-self-learning:begin -->" in text


def test_codex_dry_run_writes_nothing(codex_adapter, tmp_path):
    target = tmp_path / "AGENTS.md"
    results = codex_adapter.install_to(target=target, skill_name="sage-memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=True, yes=False)
    assert not target.exists()
    assert results[0].status == Status.WOULD_CREATE


def test_codex_self_registers():
    from sage_memory import cli_install_skills as cli
    assert "codex" in cli._ADAPTERS


# ───── OpenCodeAdapter ─────

@pytest.fixture
def opencode_adapter():
    return OpenCodeAdapter()


def test_opencode_uses_same_format_as_codex(opencode_adapter, tmp_path):
    """Both should produce byte-identical block content for the same
    skill (they share the renderer)."""
    target_oc = tmp_path / "opencode.md"
    target_cdx = tmp_path / "codex.md"
    opencode_adapter.install_to(target=target_oc, skill_name="sage-memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=False, yes=False)
    CodexAdapter().install_to(target=target_cdx, skill_name="sage-memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=False, yes=False)
    assert target_oc.read_text() == target_cdx.read_text()


def test_opencode_self_registers():
    from sage_memory import cli_install_skills as cli
    assert "opencode" in cli._ADAPTERS
