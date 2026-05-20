"""Task 4 — Adapter: claude-code (file-per-skill + references/ tree).

Claude Code skills are directories: `<target>/sage-<skill>/SKILL.md`
plus a `references/` subdir copied recursively. This adapter copies
files verbatim with per-file conflict resolution.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sage_memory.install_skills import FileResult, Status
from sage_memory.install_skills.agent_claude_code import ClaudeCodeAdapter


REPO = Path(__file__).resolve().parent.parent
BUNDLED_MEMORY = REPO / "src" / "sage_memory" / "skills" / "sage-memory"
BUNDLED_ONTOLOGY = REPO / "src" / "sage_memory" / "skills" / "sage-ontology"


@pytest.fixture
def adapter():
    return ClaudeCodeAdapter()


def _all_files(d: Path) -> list[Path]:
    return sorted(p for p in d.rglob("*") if p.is_file())


# ───── Fresh install ─────

def test_fresh_install_creates_skill_md(adapter, tmp_path):
    target = tmp_path / ".claude" / "skills"
    results = adapter.install_to(
        target=target, skill_name="sage-memory", skill_dir=BUNDLED_MEMORY,
        version="0.8.0", dry_run=False, yes=False,
    )
    skill_md = target / "sage-memory" / "SKILL.md"
    assert skill_md.is_file()
    # Bundled SKILL.md byte-identical to what was copied
    assert skill_md.read_bytes() == (BUNDLED_MEMORY / "SKILL.md").read_bytes()
    # Every result is CREATED on fresh install
    statuses = {r.status for r in results}
    assert statuses == {Status.CREATED}


def test_fresh_install_copies_references_dir_recursively(adapter, tmp_path):
    target = tmp_path / ".claude" / "skills"
    adapter.install_to(
        target=target, skill_name="sage-memory", skill_dir=BUNDLED_MEMORY,
        version="0.8.0", dry_run=False, yes=False,
    )
    installed = target / "sage-memory"
    bundled_files = {p.relative_to(BUNDLED_MEMORY) for p in _all_files(BUNDLED_MEMORY)}
    installed_files = {p.relative_to(installed) for p in _all_files(installed)}
    assert bundled_files == installed_files


def test_fresh_install_creates_parent_directories(adapter, tmp_path):
    target = tmp_path / "deep" / "nested" / "path" / ".claude" / "skills"
    assert not target.exists()
    adapter.install_to(
        target=target, skill_name="sage-memory", skill_dir=BUNDLED_MEMORY,
        version="0.8.0", dry_run=False, yes=False,
    )
    assert (target / "sage-memory" / "SKILL.md").is_file()


def test_fresh_install_copies_ontology_scripts_dir(adapter, tmp_path):
    """Ontology bundles a `scripts/` dir in addition to `references/`.
    The adapter must mirror the entire skill tree, not just markdown."""
    target = tmp_path / ".claude" / "skills"
    adapter.install_to(
        target=target, skill_name="sage-ontology", skill_dir=BUNDLED_ONTOLOGY,
        version="0.8.0", dry_run=False, yes=False,
    )
    assert (target / "sage-ontology" / "scripts" / "graph_check.py").is_file()


# ───── Idempotency ─────

def test_reinstall_unchanged_returns_unchanged_status(adapter, tmp_path):
    target = tmp_path / ".claude" / "skills"
    adapter.install_to(target=target, skill_name="sage-memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=False, yes=False)
    # Second run with no edits
    results = adapter.install_to(target=target, skill_name="sage-memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=False, yes=False)
    statuses = {r.status for r in results}
    assert statuses == {Status.UNCHANGED}


# ───── Dry-run ─────

def test_dry_run_writes_nothing_on_fresh_install(adapter, tmp_path):
    target = tmp_path / ".claude" / "skills"
    results = adapter.install_to(target=target, skill_name="sage-memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=True, yes=False)
    assert not (target / "sage-memory").exists()
    # Reports what WOULD happen — distinct from actual CREATED so the
    # CLI summary can render "would create" vs "created" accurately.
    statuses = {r.status for r in results}
    assert statuses == {Status.WOULD_CREATE}


# ───── Conflicts ─────

def test_conflict_overwrite_via_yes(adapter, tmp_path):
    target = tmp_path / ".claude" / "skills"
    # Plant a modified version
    installed = target / "sage-memory"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("LOCAL EDIT")
    # --yes → overwrite
    results = adapter.install_to(target=target, skill_name="sage-memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=False, yes=True)
    assert (installed / "SKILL.md").read_bytes() == (BUNDLED_MEMORY / "SKILL.md").read_bytes()
    skill_md_results = [r for r in results if r.path.name == "SKILL.md"]
    assert any(r.status == Status.OVERWRITTEN for r in skill_md_results)


def test_conflict_keep_via_prompt(adapter, tmp_path, monkeypatch):
    target = tmp_path / ".claude" / "skills"
    installed = target / "sage-memory"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("LOCAL EDIT — keep me")
    # Mock prompt to KEEP
    from sage_memory.install_skills import prompt
    monkeypatch.setattr(
        prompt, "prompt_conflict",
        lambda *a, **kw: prompt.Decision.KEEP,
    )
    results = adapter.install_to(target=target, skill_name="sage-memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=False, yes=False)
    # Local content preserved
    assert (installed / "SKILL.md").read_text() == "LOCAL EDIT — keep me"
    skill_md_results = [r for r in results if r.path.name == "SKILL.md"]
    assert any(r.status == Status.KEPT for r in skill_md_results)


def test_conflict_skip_via_prompt(adapter, tmp_path, monkeypatch):
    target = tmp_path / ".claude" / "skills"
    installed = target / "sage-memory"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("LOCAL EDIT")
    from sage_memory.install_skills import prompt
    monkeypatch.setattr(
        prompt, "prompt_conflict",
        lambda *a, **kw: prompt.Decision.SKIP,
    )
    results = adapter.install_to(target=target, skill_name="sage-memory",
        skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=False, yes=False)
    # Local content preserved (skip == keep for the file)
    assert (installed / "SKILL.md").read_text() == "LOCAL EDIT"
    skill_md_results = [r for r in results if r.path.name == "SKILL.md"]
    assert any(r.status == Status.SKIPPED for r in skill_md_results)


# ───── Refuse cases ─────

def test_symlink_at_target_refuses(adapter, tmp_path):
    target = tmp_path / ".claude" / "skills"
    installed = target / "sage-memory"
    installed.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("some content")
    try:
        (installed / "SKILL.md").symlink_to(elsewhere)
    except OSError:
        pytest.skip("filesystem refuses symlink creation (WSL/Windows)")
    with pytest.raises(OSError, match="symlink"):
        adapter.install_to(target=target, skill_name="sage-memory",
            skill_dir=BUNDLED_MEMORY, version="0.8.0", dry_run=False, yes=True)


# ───── Adapter registration ─────

def test_adapter_self_registers_on_import():
    from sage_memory import cli_install_skills as cli
    assert "claude-code" in cli._ADAPTERS
    assert isinstance(cli._ADAPTERS["claude-code"], ClaudeCodeAdapter)
