"""Interactive conflict prompt for the install-skills CLI.

When a target file exists and differs from the bundled version, we
show a unified diff and ask the user to pick:
  [o]verwrite  — write the bundled version
  [k]eep       — leave the existing file untouched
  [s]kip       — same as keep, but flagged in the summary

`--yes` bypasses this entirely (treats every conflict as overwrite).
Non-TTY stdin without `--yes` exits 3 with a clear error message —
handled at the CLI dispatch layer, not in this module.
"""

from __future__ import annotations

import difflib
import sys
from enum import Enum
from pathlib import Path


class Decision(Enum):
    OVERWRITE = "o"
    KEEP = "k"
    SKIP = "s"


def is_tty() -> bool:
    """True if stdin is a real terminal. The CLI uses this to refuse
    interactive prompts in non-interactive environments (CI, pipes)."""
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


def render_unified_diff(current: str, bundled: str, path: str) -> str:
    """Render a unified diff comparing current vs bundled content.

    The path appears in the `---`/`+++` headers so users can see at a
    glance which file is being prompted on.
    """
    return "".join(difflib.unified_diff(
        current.splitlines(keepends=True),
        bundled.splitlines(keepends=True),
        fromfile=f"{path} (current)",
        tofile=f"{path} (sage-memory bundled)",
        n=3,
    ))


def prompt_conflict(path: Path, current: str, bundled: str) -> Decision:
    """Print the diff and prompt for [o/k/s]. Loops on invalid input.

    Returns the user's choice as a Decision. Output goes to stdout
    so test harnesses can capture it cleanly.
    """
    diff = render_unified_diff(current, bundled, str(path))
    print(f"\nconflict: {path}")
    if diff:
        print(diff, end="")
    else:
        # No textual diff (e.g., only whitespace differs) — still prompt
        # so the user is aware.
        print("  (files differ but produce empty unified diff)")
    while True:
        try:
            raw = input("[o]verwrite / [k]eep current / [s]kip ? ").strip().lower()
        except EOFError:
            # Stdin closed mid-prompt — treat as KEEP (safer than overwrite).
            return Decision.KEEP
        if raw in ("o", "overwrite"):
            return Decision.OVERWRITE
        if raw in ("k", "keep"):
            return Decision.KEEP
        if raw in ("s", "skip"):
            return Decision.SKIP
        print(f"unrecognized: {raw!r} — please answer 'o', 'k', or 's'.")
