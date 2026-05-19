"""Shared install logic for AGENTS.md-style adapters (codex, opencode,
gemini). Each subclass differs only in its registered name and the
target-path convention enforced by the CLI dispatch; the install
mechanics are identical and live here.
"""

from __future__ import annotations

from pathlib import Path

from sage_memory.install_skills import FileResult, Status, markers, prompt
from sage_memory.install_skills.agents_markdown import render_block


class MarkdownBlockAdapter:
    """Base class: install one marker-delimited block per skill into a
    single target file. The target file is shared across multiple
    skills (one block per skill, located by name)."""

    name: str = ""

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
        if target.is_symlink():
            raise OSError(
                f"refusing to write through symlink at {target} "
                f"(resolve or remove the symlink before re-running)"
            )

        new_block = render_block(
            skill_name=skill_name, version=version, skill_dir=skill_dir,
        )

        if not target.exists():
            if dry_run:
                return [FileResult(target, Status.WOULD_CREATE)]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new_block + "\n")
            return [FileResult(target, Status.CREATED)]

        existing_text = target.read_text()
        existing_span = markers.find_block(existing_text, skill_name)

        if existing_span is None:
            if dry_run:
                return [FileResult(target, Status.WOULD_CREATE)]
            self._write(target, markers.replace_or_append(
                existing_text, skill_name, new_block,
            ))
            return [FileResult(target, Status.CREATED)]

        existing_block = existing_text[existing_span[0]:existing_span[1]]
        if markers.bodies_equal(existing_block, new_block, name=skill_name):
            return [FileResult(target, Status.UNCHANGED)]

        if dry_run:
            return [FileResult(target, Status.WOULD_OVERWRITE)]

        if yes:
            self._write(target, markers.replace_or_append(
                existing_text, skill_name, new_block,
            ))
            return [FileResult(target, Status.OVERWRITTEN)]

        decision = prompt.prompt_conflict(
            target, existing_block, new_block,
        )
        if decision == prompt.Decision.OVERWRITE:
            self._write(target, markers.replace_or_append(
                existing_text, skill_name, new_block,
            ))
            return [FileResult(target, Status.OVERWRITTEN)]
        if decision == prompt.Decision.KEEP:
            return [FileResult(target, Status.KEPT)]
        return [FileResult(target, Status.SKIPPED)]

    @staticmethod
    def _write(target: Path, text: str) -> None:
        """Ensure the rendered file ends with exactly one newline so a
        subsequent user-appended section (e.g. `## Notes`) starts on a
        fresh line, not glued to the end marker."""
        if not text.endswith("\n"):
            text = text + "\n"
        target.write_text(text)
