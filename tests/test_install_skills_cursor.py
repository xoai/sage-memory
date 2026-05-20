"""Task 5 — Adapter: cursor (.mdc with frontmatter).

Cursor reads rule files from `.cursor/rules/*.mdc`. Each .mdc has
required frontmatter (`description`, `globs`, `alwaysApply`) and a
markdown body. The adapter writes one .mdc per skill, named
`sage-<skill>.mdc`, with the skill body composed from the bundled
SKILL.md (frontmatter stripped, body kept as-is).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sage_memory.install_skills import FileResult, Status
from sage_memory.install_skills.agent_cursor import CursorAdapter


REPO = Path(__file__).resolve().parent.parent
BUNDLED_MEMORY = REPO / "src" / "sage_memory" / "skills" / "sage-memory"
BUNDLED_ONTOLOGY = REPO / "src" / "sage_memory" / "skills" / "sage-ontology"


@pytest.fixture
def adapter():
    return CursorAdapter()


# ───── Fresh install ─────

def test_fresh_install_creates_mdc_with_frontmatter(adapter, tmp_path):
    target = tmp_path / ".cursor" / "rules"
    results = adapter.install_to(
        target=target, skill_name="sage-memory", skill_dir=BUNDLED_MEMORY,
        version="0.8.0", dry_run=False, yes=False,
    )
    mdc = target / "sage-memory.mdc"
    assert mdc.is_file()
    text = mdc.read_text()

    # Required frontmatter fields
    assert text.startswith("---\n")
    head, _, body = text.partition("\n---\n")
    assert "description:" in head
    assert "globs:" in head
    assert "alwaysApply: false" in head
    # Body is non-empty
    assert body.strip()

    # Single CREATED result for the single file
    assert len(results) == 1
    assert results[0].status == Status.CREATED


def test_skill_body_has_skill_md_frontmatter_stripped(adapter, tmp_path):
    """The SKILL.md's YAML frontmatter is removed before being placed
    in the .mdc body — we don't want two frontmatter blocks."""
    target = tmp_path / ".cursor" / "rules"
    adapter.install_to(
        target=target, skill_name="sage-memory", skill_dir=BUNDLED_MEMORY,
        version="0.8.0", dry_run=False, yes=False,
    )
    text = (target / "sage-memory.mdc").read_text()
    # Split off OUR frontmatter (the first --- block)
    _, _, body = text.partition("\n---\n")
    # Body should NOT contain the SKILL.md frontmatter keys like "name:" or "description: >"
    # in a way that suggests a second YAML block. The body should be markdown content.
    body_start = body.lstrip()
    assert not body_start.startswith("---"), (
        "body must not begin with a second frontmatter block"
    )


def test_description_comes_from_skill_md_frontmatter(adapter, tmp_path):
    """The .mdc's `description:` should be derived from the bundled
    SKILL.md's `description:` field. We don't need verbatim equality,
    but it must be non-empty and a one-line string (no embedded newlines)."""
    target = tmp_path / ".cursor" / "rules"
    adapter.install_to(
        target=target, skill_name="sage-memory", skill_dir=BUNDLED_MEMORY,
        version="0.8.0", dry_run=False, yes=False,
    )
    text = (target / "sage-memory.mdc").read_text()
    head, _, _ = text.partition("\n---\n")
    desc_lines = [
        line for line in head.splitlines() if line.startswith("description:")
    ]
    assert len(desc_lines) == 1
    desc_value = desc_lines[0][len("description:"):].strip()
    assert desc_value
    # No literal newline characters (description is single-line in .mdc)
    assert "\n" not in desc_value


def test_fresh_install_creates_parent_dirs(adapter, tmp_path):
    target = tmp_path / "deep" / "nested" / ".cursor" / "rules"
    adapter.install_to(
        target=target, skill_name="sage-memory", skill_dir=BUNDLED_MEMORY,
        version="0.8.0", dry_run=False, yes=False,
    )
    assert (target / "sage-memory.mdc").is_file()


def test_three_skills_produce_three_mdc_files(adapter, tmp_path):
    target = tmp_path / ".cursor" / "rules"
    for skill, dir_ in [
        ("sage-memory", BUNDLED_MEMORY),
        ("sage-ontology", BUNDLED_ONTOLOGY),
        ("sage-self-learning",
         REPO / "src" / "sage_memory" / "skills" / "sage-self-learning"),
    ]:
        adapter.install_to(
            target=target, skill_name=skill, skill_dir=dir_,
            version="0.8.0", dry_run=False, yes=False,
        )
    mdc_files = sorted(target.glob("*.mdc"))
    assert [m.name for m in mdc_files] == [
        "sage-memory.mdc", "sage-ontology.mdc", "sage-self-learning.mdc",
    ]


# ───── Idempotency ─────

def test_reinstall_unchanged(adapter, tmp_path):
    target = tmp_path / ".cursor" / "rules"
    adapter.install_to(target=target, skill_name="sage-memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=False, yes=False)
    results = adapter.install_to(target=target, skill_name="sage-memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=False, yes=False)
    assert results[0].status == Status.UNCHANGED


# ───── Dry-run ─────

def test_dry_run_writes_nothing(adapter, tmp_path):
    target = tmp_path / ".cursor" / "rules"
    results = adapter.install_to(target=target, skill_name="sage-memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=True, yes=False)
    assert not (target / "sage-memory.mdc").exists()
    assert results[0].status == Status.WOULD_CREATE


# ───── Conflicts ─────

def test_conflict_overwrite_via_yes(adapter, tmp_path):
    target = tmp_path / ".cursor" / "rules"
    target.mkdir(parents=True)
    (target / "sage-memory.mdc").write_text("LOCAL EDIT")
    results = adapter.install_to(target=target, skill_name="sage-memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=False, yes=True)
    text = (target / "sage-memory.mdc").read_text()
    assert "LOCAL EDIT" not in text
    assert text.startswith("---\n")
    assert results[0].status == Status.OVERWRITTEN


def test_conflict_keep_via_prompt(adapter, tmp_path, monkeypatch):
    target = tmp_path / ".cursor" / "rules"
    target.mkdir(parents=True)
    (target / "sage-memory.mdc").write_text("LOCAL EDIT — keep me")
    from sage_memory.install_skills import prompt
    monkeypatch.setattr(
        prompt, "prompt_conflict",
        lambda *a, **kw: prompt.Decision.KEEP,
    )
    results = adapter.install_to(target=target, skill_name="sage-memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=False, yes=False)
    assert (target / "sage-memory.mdc").read_text() == "LOCAL EDIT — keep me"
    assert results[0].status == Status.KEPT


# ───── Refuse ─────

def test_symlink_at_target_refuses(adapter, tmp_path):
    target = tmp_path / ".cursor" / "rules"
    target.mkdir(parents=True)
    other = tmp_path / "elsewhere.mdc"
    other.write_text("x")
    try:
        (target / "sage-memory.mdc").symlink_to(other)
    except OSError:
        pytest.skip("filesystem refuses symlink creation")
    with pytest.raises(OSError, match="symlink"):
        adapter.install_to(target=target, skill_name="sage-memory",
            skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=False, yes=True)


# ───── Adapter registration ─────

def test_adapter_self_registers_on_import():
    from sage_memory import cli_install_skills as cli
    assert "cursor" in cli._ADAPTERS
    assert isinstance(cli._ADAPTERS["cursor"], CursorAdapter)
