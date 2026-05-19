"""Per-agent target path resolution.

`--global` resolves to user-level config; `--project` resolves
relative to a caller-provided cwd. No `platformdirs` dependency —
hand-rolled XDG-style logic instead. Windows is best-effort:
`os.path.expanduser("~")` handles the home dir, and
`XDG_CONFIG_HOME` is respected if set (rare on Windows but possible
under WSL or with explicit configuration).
"""

from __future__ import annotations

import os
from pathlib import Path

# Public agent identifiers. Keep in sync with the CLI choices.
AGENTS = ("claude-code", "codex", "gemini", "cursor", "opencode")

# Project-marker files that indicate "this is a project root". Used to
# decide whether to print the "no project markers" warning for
# `--project` installs in dirs that don't look like project roots.
PROJECT_MARKERS = (
    ".git",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "setup.py",
    "build.gradle",
    "build.gradle.kts",
    "pom.xml",
)


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or (_home() / ".config"))


def global_target(agent: str) -> Path:
    """Resolve the user-level install target for `agent`.

    Returns a directory for file-per-skill agents (claude-code, cursor)
    and a single file path for marker-block agents (codex, gemini,
    opencode).
    """
    home = _home()
    if agent == "claude-code":
        return home / ".claude" / "skills"
    if agent == "cursor":
        return home / ".cursor" / "rules"
    if agent == "codex":
        return home / ".codex" / "AGENTS.md"
    if agent == "gemini":
        return home / ".gemini" / "GEMINI.md"
    if agent == "opencode":
        return _xdg_config_home() / "opencode" / "AGENTS.md"
    raise ValueError(f"unknown agent: {agent}")


def project_target(agent: str, cwd: Path) -> Path:
    """Resolve the project-scoped install target relative to `cwd`."""
    if agent == "claude-code":
        return cwd / ".claude" / "skills"
    if agent == "cursor":
        return cwd / ".cursor" / "rules"
    if agent == "codex":
        return cwd / "AGENTS.md"
    if agent == "gemini":
        return cwd / "GEMINI.md"
    if agent == "opencode":
        return cwd / "AGENTS.md"
    raise ValueError(f"unknown agent: {agent}")


def warn_if_no_project_markers(cwd: Path) -> str | None:
    """Return a one-line warning string if `cwd` has none of the
    standard project markers, otherwise None.

    Used by the CLI to nudge users who run `--project` from a random
    directory that doesn't look like a project root. The decision is
    informational — we still install where they asked.
    """
    for marker in PROJECT_MARKERS:
        if (cwd / marker).exists():
            return None
    return f"warning: no project markers found in {cwd}; installing into this directory anyway"
