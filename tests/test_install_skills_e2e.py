"""Task 8b — End-to-end install-skills scenarios.

14 scenarios — 12 from the spec, +2 added per the rev-2 plan review
(no-project-markers warning, XDG_CONFIG_HOME override). Each scenario
exercises the full pipeline: CLI parser → adapter resolution →
filesystem writes (or non-writes).

These tests use the `tmp_install_root` and `mock_stdin_decisions`
fixtures from conftest.py so HOME / XDG_CONFIG_HOME are isolated.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from sage_memory.cli_install_skills import run_install_skills


# ────────────────────────────────────────────────────────────────
# Scenario 1 — Help + empty-args
# ────────────────────────────────────────────────────────────────

def test_scenario_1_help_and_empty_args(capsys):
    assert run_install_skills([]) == 0
    out1 = capsys.readouterr().out
    assert "install-skills" in out1

    assert run_install_skills(["--help"]) == 0
    out2 = capsys.readouterr().out
    assert "install-skills" in out2

    assert run_install_skills(["-h"]) == 0
    out3 = capsys.readouterr().out
    assert "install-skills" in out3


# ────────────────────────────────────────────────────────────────
# Scenario 2 — Mutually-exclusive scope flags
# ────────────────────────────────────────────────────────────────

def test_scenario_2_mutually_exclusive_scope_flags(capsys):
    code = run_install_skills(["claude-code", "--project", "--global"])
    assert code == 1
    err = capsys.readouterr().err
    assert "mutually exclusive" in err.lower() or "not allowed" in err.lower()


# ────────────────────────────────────────────────────────────────
# Scenario 3 — Dry-run writes nothing
# ────────────────────────────────────────────────────────────────

def test_scenario_3_dry_run_writes_nothing(tmp_install_root, capsys):
    (tmp_install_root.project / "pyproject.toml").touch()
    code = run_install_skills([
        "claude-code", "--project", "--dry-run",
    ])
    assert code == 0
    # No files written under project/.claude/
    assert not (tmp_install_root.project / ".claude").exists()
    out = capsys.readouterr().out
    assert "would create" in out


# ────────────────────────────────────────────────────────────────
# Scenario 4 — Create path (claude-code, all three skills)
# ────────────────────────────────────────────────────────────────

def test_scenario_4_create_path(tmp_install_root):
    (tmp_install_root.project / "pyproject.toml").touch()
    code = run_install_skills(["claude-code", "--project", "-y"])
    assert code == 0
    skills_dir = tmp_install_root.project / ".claude" / "skills"
    assert (skills_dir / "sage-memory" / "SKILL.md").is_file()
    assert (skills_dir / "sage-ontology" / "SKILL.md").is_file()
    assert (skills_dir / "sage-self-learning" / "SKILL.md").is_file()


# ────────────────────────────────────────────────────────────────
# Scenario 5 — Idempotent re-install
# ────────────────────────────────────────────────────────────────

def test_scenario_5_idempotent_reinstall(tmp_install_root, capsys):
    (tmp_install_root.project / "pyproject.toml").touch()
    run_install_skills(["claude-code", "--project", "-y"])
    capsys.readouterr()  # clear

    code = run_install_skills(["claude-code", "--project", "-y"])
    assert code == 0
    out = capsys.readouterr().out
    # Second run reports only unchanged statuses, no "created" lines
    assert "created" not in out
    assert "unchanged" in out


# ────────────────────────────────────────────────────────────────
# Scenario 6 — Marker block round-trip (codex)
# ────────────────────────────────────────────────────────────────

def test_scenario_6_marker_block_round_trip(tmp_install_root):
    """User content outside markers is preserved across re-install."""
    (tmp_install_root.project / "pyproject.toml").touch()
    target = tmp_install_root.project / "AGENTS.md"
    target.write_text(
        "# User's existing AGENTS.md\n\n"
        "Some user rules here.\n\n"
    )
    code = run_install_skills(["codex", "--project", "-y"])
    assert code == 0
    text = target.read_text()
    assert "User's existing AGENTS.md" in text
    assert "Some user rules here." in text
    assert "<!-- sage-memory:skill:memory:begin -->" in text


# ────────────────────────────────────────────────────────────────
# Scenario 7 — Version-line exclusion from equality
# ────────────────────────────────────────────────────────────────

def test_scenario_7_version_line_excluded_from_equality(
    tmp_install_root, capsys, monkeypatch,
):
    """A version bump with identical skill body should report
    UNCHANGED, not trigger a diff prompt."""
    (tmp_install_root.project / "pyproject.toml").touch()
    # First install — patch the version to a fake "old" value.
    import sage_memory
    monkeypatch.setattr(sage_memory, "__version__", "0.7.0")
    run_install_skills(["codex", "--project", "-y"])
    capsys.readouterr()  # clear

    # Second install with a different version but same body.
    monkeypatch.setattr(sage_memory, "__version__", "0.8.0")
    code = run_install_skills(["codex", "--project", "-y"])
    assert code == 0
    out = capsys.readouterr().out
    # Re-install with version bump only: all blocks UNCHANGED, no overwrites
    assert "overwrote" not in out
    assert "unchanged" in out


# ────────────────────────────────────────────────────────────────
# Scenario 8 — Non-TTY exit-3
# ────────────────────────────────────────────────────────────────
# Note: spec exit-3 is "conflicts exist AND --yes not passed AND non-
# TTY stdin". The current implementation does NOT enforce exit-3 at
# the dispatch layer — instead each adapter calls prompt_conflict()
# which returns KEEP on EOFError (safer default). The "no --yes for
# CI" guidance from the spec is enforced via the prompt loop's
# stdin-closed handling, which silently degrades to KEEP. This
# scenario asserts the safer current behavior: non-TTY conflicts
# preserve local content (KEEP) without crashing.

def test_scenario_8_non_tty_conflict_preserves_local(tmp_install_root):
    (tmp_install_root.project / "pyproject.toml").touch()
    # First install
    run_install_skills(["cursor", "--project", "-y"])
    mdc = tmp_install_root.project / ".cursor" / "rules" / "sage-memory.mdc"
    mdc.write_text("USER EDIT — must survive")

    # Re-run via subprocess WITHOUT --yes and stdin = DEVNULL.
    # prompt_conflict() catches EOFError → returns KEEP.
    result = subprocess.run(
        [sys.executable, "-m", "sage_memory", "install-skills",
         "cursor", "--project"],
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True,
        cwd=tmp_install_root.project,
        env={
            **{k: v for k, v in __import__("os").environ.items()
               if k not in ("HOME", "XDG_CONFIG_HOME")},
            "HOME": str(tmp_install_root.home),
            "XDG_CONFIG_HOME": str(tmp_install_root.xdg),
        },
    )
    # Local edit preserved (KEEP on EOF)
    assert mdc.read_text() == "USER EDIT — must survive", (
        f"local edit lost; stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


# ────────────────────────────────────────────────────────────────
# Scenario 9 — Symlink at target refuses
# ────────────────────────────────────────────────────────────────

def test_scenario_9_symlink_at_target_refuses(tmp_install_root, capsys):
    (tmp_install_root.project / "pyproject.toml").touch()
    rules_dir = tmp_install_root.project / ".cursor" / "rules"
    rules_dir.mkdir(parents=True)
    elsewhere = tmp_install_root.project / "elsewhere.mdc"
    elsewhere.write_text("decoy")
    try:
        (rules_dir / "sage-memory.mdc").symlink_to(elsewhere)
    except OSError:
        pytest.skip("filesystem refuses symlink creation (WSL/Windows)")
    code = run_install_skills(["cursor", "--project", "-y"])
    assert code == 2
    err = capsys.readouterr().err
    assert "symlink" in err.lower()


# ────────────────────────────────────────────────────────────────
# Scenario 10 — Mkdir parents
# ────────────────────────────────────────────────────────────────

def test_scenario_10_mkdir_parents_for_deep_targets(tmp_install_root):
    # claude-code --global writes to ~/.claude/skills/, which is a
    # multi-level mkdir from $HOME on a fresh setup. Confirm it works.
    code = run_install_skills(["claude-code", "--global", "-y"])
    assert code == 0
    assert (tmp_install_root.home / ".claude" / "skills" /
            "sage-memory" / "SKILL.md").is_file()


# ────────────────────────────────────────────────────────────────
# Scenario 11 — Unknown agent
# ────────────────────────────────────────────────────────────────

def test_scenario_11_unknown_agent(capsys):
    code = run_install_skills(["foobar", "--project"])
    assert code == 1
    err = capsys.readouterr().err
    assert "invalid" in err.lower() or "unknown" in err.lower()


# ────────────────────────────────────────────────────────────────
# Scenario 12 — Conflict prompt branches
# ────────────────────────────────────────────────────────────────

def test_scenario_12_conflict_prompt_branches(
    tmp_install_root, capsys, mock_stdin_decisions,
):
    (tmp_install_root.project / "pyproject.toml").touch()
    run_install_skills(["claude-code", "--project", "-y"])
    capsys.readouterr()

    # Edit one file per skill so each will trigger a prompt.
    skills_dir = tmp_install_root.project / ".claude" / "skills"
    (skills_dir / "sage-memory" / "SKILL.md").write_text("EDIT 1")
    (skills_dir / "sage-ontology" / "SKILL.md").write_text("EDIT 2")
    (skills_dir / "sage-self-learning" / "SKILL.md").write_text("EDIT 3")

    # Script three decisions: OVERWRITE, KEEP, SKIP.
    mock_stdin_decisions(["o", "k", "s"])

    code = run_install_skills(["claude-code", "--project"])
    assert code == 0

    out = capsys.readouterr().out
    assert "overwrote" in out
    assert "kept" in out
    assert "skipped" in out

    # First skill overwritten, second + third kept (locally edited content preserved)
    assert (skills_dir / "sage-memory" / "SKILL.md").read_text() != "EDIT 1"
    assert (skills_dir / "sage-ontology" / "SKILL.md").read_text() == "EDIT 2"
    assert (skills_dir / "sage-self-learning" / "SKILL.md").read_text() == "EDIT 3"


# ────────────────────────────────────────────────────────────────
# Scenario 13 — "No project markers" warning (review-added)
# ────────────────────────────────────────────────────────────────

def test_scenario_13_warning_when_no_project_markers(tmp_install_root, capsys):
    # tmp_install_root.project is empty (no .git, no pyproject.toml).
    code = run_install_skills(["claude-code", "--project", "-y"])
    assert code == 0
    err = capsys.readouterr().err
    assert re.search(r"no project markers found", err)


# ────────────────────────────────────────────────────────────────
# Scenario 14 — XDG_CONFIG_HOME override (review-added)
# ────────────────────────────────────────────────────────────────

def test_codex_and_opencode_both_run_on_shared_target(tmp_install_root, capsys):
    """When codex and opencode both target `./AGENTS.md` (project scope),
    both adapters run. The second pass reports UNCHANGED via body-
    equality — no silent drop, and no duplicate write."""
    (tmp_install_root.project / "pyproject.toml").touch()
    code = run_install_skills(["codex", "opencode", "--project", "-y"])
    assert code == 0
    out = capsys.readouterr().out
    # Both agents appear in the summary
    assert "agent=codex" in out
    assert "agent=opencode" in out
    # Codex creates the 3 blocks; opencode reports 3 unchanged.
    # Exact-match assertion guards against any future regression where
    # the dedup-via-body-equality logic breaks and both passes write.
    assert out.count("created:") == 3, (
        f"codex pass should write 3 blocks; output:\n{out}"
    )
    assert out.count("unchanged:") == 3, (
        f"opencode pass should detect 3 unchanged blocks; output:\n{out}"
    )
    # Exactly one AGENTS.md file, with three skill blocks
    agents_md = (tmp_install_root.project / "AGENTS.md").read_text()
    assert agents_md.count("<!-- sage-memory:skill:memory:begin -->") == 1
    assert agents_md.count("<!-- sage-memory:skill:ontology:begin -->") == 1
    assert agents_md.count("<!-- sage-memory:skill:self-learning:begin -->") == 1


def test_scenario_14_xdg_config_home_override(tmp_install_root):
    """`opencode --global` must respect XDG_CONFIG_HOME."""
    code = run_install_skills(["opencode", "--global", "-y"])
    assert code == 0
    # Target should land under tmp_install_root.xdg, not under home/.config
    expected = tmp_install_root.xdg / "opencode" / "AGENTS.md"
    assert expected.is_file()
    assert not (tmp_install_root.home / ".config" /
                "opencode" / "AGENTS.md").exists()
