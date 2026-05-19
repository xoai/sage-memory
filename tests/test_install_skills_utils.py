"""Task 2 — Unit tests for install_skills/{markers,paths,prompt}.py.

Per plan: marker round-trip (with mid-block edits), version-line
exclusion from byte-equality, XDG_CONFIG_HOME override, non-TTY
detection, "no project markers" return value.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sage_memory.install_skills import markers, paths, prompt


# ───── markers.py ─────

def test_format_block_wraps_with_begin_end_and_version():
    block = markers.format_block("memory", "0.8.0", "Hello, world.\n")
    assert block.startswith("<!-- sage-memory:skill:memory:begin -->\n")
    assert block.endswith("<!-- sage-memory:skill:memory:end -->")
    assert "<!-- sage-memory version: 0.8.0 -->" in block
    assert "Hello, world." in block


def test_find_block_returns_none_when_absent():
    assert markers.find_block("no markers here", "memory") is None


def test_find_block_locates_begin_to_end_inclusive():
    text = (
        "leading content\n\n"
        "<!-- sage-memory:skill:memory:begin -->\n"
        "<!-- sage-memory version: 0.8.0 -->\n"
        "body\n"
        "<!-- sage-memory:skill:memory:end -->\n"
        "\ntrailing content"
    )
    span = markers.find_block(text, "memory")
    assert span is not None
    begin, end = span
    block = text[begin:end]
    assert block.startswith("<!-- sage-memory:skill:memory:begin -->")
    assert block.endswith("<!-- sage-memory:skill:memory:end -->")
    # Outside-marker content is preserved by replace operations
    assert "leading content" not in block
    assert "trailing content" not in block


def test_find_block_unrelated_skill_name_does_not_match():
    text = (
        "<!-- sage-memory:skill:ontology:begin -->\n"
        "body\n"
        "<!-- sage-memory:skill:ontology:end -->"
    )
    assert markers.find_block(text, "memory") is None


def test_replace_or_append_creates_on_empty_input():
    out = markers.replace_or_append(
        "", "memory", markers.format_block("memory", "0.8.0", "body\n")
    )
    assert "skill:memory:begin" in out
    assert "body" in out


def test_replace_or_append_appends_with_two_leading_newlines_when_no_marker():
    text = "existing user content"
    out = markers.replace_or_append(
        text, "memory", markers.format_block("memory", "0.8.0", "body\n")
    )
    assert out.startswith("existing user content\n\n<!-- sage-memory:skill:memory:begin")


def test_replace_or_append_replaces_existing_block_preserving_outside_content():
    old_block = markers.format_block("memory", "0.7.0", "old body\n")
    text = f"before content\n\n{old_block}\n\nafter content"
    new_block = markers.format_block("memory", "0.8.0", "new body\n")
    out = markers.replace_or_append(text, "memory", new_block)
    assert "before content" in out
    assert "after content" in out
    assert "old body" not in out
    assert "new body" in out
    assert "version: 0.8.0" in out


def test_replace_or_append_handles_truncated_block_as_append():
    """Begin marker present without end marker → treat as missing,
    append a fresh block. (Per spec §"Block operations on install"
    case 4.)"""
    text = "<!-- sage-memory:skill:memory:begin -->\nincomplete..."
    new_block = markers.format_block("memory", "0.8.0", "body\n")
    out = markers.replace_or_append(text, "memory", new_block)
    # Original truncated content stays; new block appended
    assert "incomplete..." in out
    assert out.count("<!-- sage-memory:skill:memory:end -->") == 1


def test_extract_body_returns_content_between_markers_excluding_version_line():
    block = markers.format_block("memory", "0.8.0", "the body\n")
    body = markers.extract_body(block, "memory")
    assert body == "the body\n"


def test_bodies_equal_ignores_version_line_differences():
    block_v0 = markers.format_block("memory", "0.7.0", "same body\n")
    block_v1 = markers.format_block("memory", "0.8.0", "same body\n")
    assert markers.bodies_equal(block_v0, block_v1, name="memory")


def test_bodies_equal_returns_false_when_body_differs():
    block_a = markers.format_block("memory", "0.8.0", "body A\n")
    block_b = markers.format_block("memory", "0.8.0", "body B\n")
    assert not markers.bodies_equal(block_a, block_b, name="memory")


def test_extract_body_normalizes_crlf_line_endings():
    """A git-on-Windows checkout with autocrlf would produce CRLF
    file content. The body-equality check must normalize so cross-
    platform installs don't diff every run."""
    lf_block = markers.format_block("memory", "0.8.0", "line one\nline two\n")
    crlf_block = lf_block.replace("\n", "\r\n")
    assert markers.bodies_equal(lf_block, crlf_block, name="memory")


def test_find_block_skips_mid_line_literal_end_marker():
    """A user could paste a literal end marker mid-paragraph inside
    an edited block body. find_block must require markers to be
    line-anchored, so a mid-line `:end -->` is ignored and the true
    end marker (the one on its own line) wins. Without this guard,
    replace_or_append would discard everything between the spurious
    end and the real end on the next install."""
    text = (
        "<!-- sage-memory:skill:memory:begin -->\n"
        "user-injected <!-- sage-memory:skill:memory:end --> mid-body\n"
        "more content (must survive)\n"
        "<!-- sage-memory:skill:memory:end -->\n"
    )
    span = markers.find_block(text, "memory")
    assert span is not None
    block = text[span[0]:span[1]]
    # The block contains the mid-line literal AND ends at the real
    # (line-anchored) end marker — both end-marker substrings appear.
    assert block.count("<!-- sage-memory:skill:memory:end -->") == 2
    # The user's content between the spurious and real end markers is
    # preserved inside the block, not silently truncated.
    assert "more content (must survive)" in block


def test_replace_or_append_preserves_user_content_with_mid_line_marker():
    """End-to-end round-trip guard for the literal-end-marker bug."""
    text = (
        "before user content\n\n"
        "<!-- sage-memory:skill:memory:begin -->\n"
        "<!-- sage-memory version: 0.7.0 -->\n"
        "user note: this discusses <!-- sage-memory:skill:memory:end --> markers\n"
        "<!-- sage-memory:skill:memory:end -->\n\n"
        "after user content\n"
    )
    new_block = markers.format_block("memory", "0.8.0", "new body\n")
    out = markers.replace_or_append(text, "memory", new_block)
    # Outside-marker user content is preserved exactly
    assert "before user content" in out
    assert "after user content" in out
    # The new block replaces ONLY the marker-delimited region
    assert "new body" in out
    # No content beyond the real end marker was silently dropped
    assert out.endswith("after user content\n")


# ───── paths.py ─────

@pytest.mark.parametrize("agent,expected_suffix", [
    ("claude-code", "/.claude/skills"),
    ("cursor", "/.cursor/rules"),
    ("codex", "/.codex/AGENTS.md"),
    ("gemini", "/.gemini/GEMINI.md"),
])
def test_global_target_resolves_under_home(agent, expected_suffix, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    target = paths.global_target(agent)
    assert str(target).endswith(expected_suffix), (
        f"{agent} → {target} did not end with {expected_suffix}"
    )
    assert str(target).startswith(str(tmp_path))


def test_global_target_opencode_uses_xdg_config_home_when_set(monkeypatch, tmp_path):
    custom = tmp_path / "custom-xdg"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(custom))
    target = paths.global_target("opencode")
    assert str(target).startswith(str(custom)), (
        f"opencode --global should land under XDG_CONFIG_HOME={custom}; got {target}"
    )
    assert str(target).endswith("/opencode/AGENTS.md")


def test_global_target_opencode_falls_back_to_home_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    target = paths.global_target("opencode")
    assert str(target) == str(tmp_path / ".config" / "opencode" / "AGENTS.md")


def test_global_target_unknown_agent_raises():
    with pytest.raises(ValueError, match="unknown agent"):
        paths.global_target("unknown-agent")


@pytest.mark.parametrize("agent,expected_suffix", [
    ("claude-code", ".claude/skills"),
    ("cursor", ".cursor/rules"),
    ("codex", "AGENTS.md"),
    ("gemini", "GEMINI.md"),
    ("opencode", "AGENTS.md"),
])
def test_project_target_relative_to_cwd(agent, expected_suffix, tmp_path):
    target = paths.project_target(agent, tmp_path)
    assert str(target).endswith(expected_suffix)
    assert str(target).startswith(str(tmp_path))


def test_warn_if_no_project_markers_returns_warning_for_empty_dir(tmp_path):
    msg = paths.warn_if_no_project_markers(tmp_path)
    assert msg is not None
    assert "no project markers" in msg.lower()


def test_warn_if_no_project_markers_returns_none_when_git_present(tmp_path):
    (tmp_path / ".git").mkdir()
    assert paths.warn_if_no_project_markers(tmp_path) is None


@pytest.mark.parametrize("marker", [
    "pyproject.toml", "package.json", "Cargo.toml", "go.mod"
])
def test_warn_if_no_project_markers_returns_none_for_lang_markers(marker, tmp_path):
    (tmp_path / marker).touch()
    assert paths.warn_if_no_project_markers(tmp_path) is None


# ───── prompt.py ─────

def test_is_tty_returns_bool():
    assert isinstance(prompt.is_tty(), bool)


def test_is_tty_false_when_stdin_not_a_tty(monkeypatch):
    class _NonTTYStdin:
        def isatty(self): return False
    monkeypatch.setattr("sys.stdin", _NonTTYStdin())
    assert prompt.is_tty() is False


def test_render_unified_diff_includes_path_and_both_sides():
    out = prompt.render_unified_diff(
        current="line one\nline two\n",
        bundled="line one\nLINE TWO\n",
        path="some/file.md",
    )
    assert "some/file.md" in out
    assert "-line two" in out
    assert "+LINE TWO" in out


def test_decision_enum_values():
    assert prompt.Decision.OVERWRITE.value == "o"
    assert prompt.Decision.KEEP.value == "k"
    assert prompt.Decision.SKIP.value == "s"


def test_prompt_conflict_accepts_o_k_s(monkeypatch, capsys):
    """User typing 'o' → OVERWRITE; 'k' → KEEP; 's' → SKIP."""
    for char, expected in [
        ("o", prompt.Decision.OVERWRITE),
        ("k", prompt.Decision.KEEP),
        ("s", prompt.Decision.SKIP),
    ]:
        responses = iter([char])
        monkeypatch.setattr("builtins.input", lambda _="": next(responses))
        result = prompt.prompt_conflict(
            Path("/tmp/x.md"), current="a\n", bundled="b\n",
        )
        assert result == expected, f"input '{char}' should map to {expected}"


def test_prompt_conflict_rejects_invalid_input_and_re_prompts(monkeypatch):
    """Invalid input → re-prompt until valid response."""
    responses = iter(["", "x", "yes", "o"])
    monkeypatch.setattr("builtins.input", lambda _="": next(responses))
    result = prompt.prompt_conflict(
        Path("/tmp/x.md"), current="a\n", bundled="b\n",
    )
    assert result == prompt.Decision.OVERWRITE
