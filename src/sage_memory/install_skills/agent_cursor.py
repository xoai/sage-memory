"""Cursor adapter — one `.mdc` per skill in `.cursor/rules/`.

Cursor reads rule files from `.cursor/rules/*.mdc`. Each .mdc has
required frontmatter (`description`, `globs`, `alwaysApply`) and a
markdown body. We write one file per skill named `sage-<skill>.mdc`,
deriving `description` from the bundled SKILL.md's own `description:`
field and using `globs: ["**/*"]` + `alwaysApply: false` so the rule
loads on demand rather than for every file.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from sage_memory.cli_install_skills import register_adapter
from sage_memory.install_skills import FileResult, Status, prompt


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def _split_skill_md(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body_text). If no frontmatter present,
    frontmatter is empty and the entire input is the body.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    body = text[m.end():]
    fm: dict[str, str] = {}
    current_key: str | None = None
    current_lines: list[str] = []

    def _flush():
        nonlocal current_key, current_lines
        if current_key is not None:
            fm[current_key] = " ".join(
                line.strip() for line in current_lines if line.strip()
            )
        current_key = None
        current_lines = []

    for line in raw.splitlines():
        # Top-level key (no leading whitespace, contains ":")
        if line and not line.startswith((" ", "\t")) and ":" in line:
            _flush()
            key, _, value = line.partition(":")
            current_key = key.strip()
            value = value.strip()
            # Folded scalar marker ">" or block scalar "|" — first line of
            # the value lives on subsequent indented lines.
            if value in (">", "|"):
                current_lines = []
            else:
                current_lines = [value] if value else []
        else:
            current_lines.append(line)
    _flush()
    return fm, body


def _render_mdc(skill_md_text: str) -> str:
    fm, body = _split_skill_md(skill_md_text)
    description = fm.get("description", "sage-memory skill")
    # Cursor's description must be single-line; collapse any whitespace.
    description = re.sub(r"\s+", " ", description).strip()
    head = (
        "---\n"
        f"description: {description}\n"
        'globs: ["**/*"]\n'
        "alwaysApply: false\n"
        "---\n"
    )
    return head + body.lstrip("\n")


class CursorAdapter:
    name = "cursor"

    def install_to(
        self,
        *,
        target: Path,
        skill_name: str,
        skill_dir: Path,
        version: str,
        dry_run: bool,
        yes: bool,
    ) -> list[FileResult]:
        dst = target / f"sage-{skill_name}.mdc"
        skill_md = (skill_dir / "SKILL.md").read_text()
        bundled = _render_mdc(skill_md)

        if dst.is_symlink():
            raise OSError(
                f"refusing to write through symlink at {dst} "
                f"(resolve or remove the symlink before re-running)"
            )

        if not dst.exists():
            if dry_run:
                return [FileResult(dst, Status.WOULD_CREATE)]
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(bundled)
            return [FileResult(dst, Status.CREATED)]

        current = dst.read_text()
        if current == bundled:
            return [FileResult(dst, Status.UNCHANGED)]

        if dry_run:
            return [FileResult(dst, Status.WOULD_OVERWRITE)]

        if yes:
            dst.write_text(bundled)
            return [FileResult(dst, Status.OVERWRITTEN)]

        decision = prompt.prompt_conflict(dst, current, bundled)
        if decision == prompt.Decision.OVERWRITE:
            dst.write_text(bundled)
            return [FileResult(dst, Status.OVERWRITTEN)]
        if decision == prompt.Decision.KEEP:
            return [FileResult(dst, Status.KEPT)]
        return [FileResult(dst, Status.SKIPPED)]


register_adapter("cursor", CursorAdapter())
