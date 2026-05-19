"""Claude Code adapter — file-per-skill install with `references/`
directory copied recursively.

Claude Code reads skills from `~/.claude/skills/<skill-name>/SKILL.md`
(global) or `.claude/skills/<skill-name>/SKILL.md` (project). The
sage-memory bundled skills are namespaced under `sage-<skill>` to avoid
colliding with user-authored skills of the same base name.

Files are copied byte-for-byte. Per-file conflict resolution: if the
destination differs from the bundled version, the user is prompted
(or `--yes` auto-overwrites). Symlinks at any destination cause the
install to refuse rather than silently follow them.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from sage_memory.cli_install_skills import register_adapter
from sage_memory.install_skills import FileResult, Status, prompt


class ClaudeCodeAdapter:
    name = "claude-code"

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
        install_dir = target / f"sage-{skill_name}"
        results: list[FileResult] = []

        # Iterate every file in the bundled skill tree.
        for src in sorted(p for p in skill_dir.rglob("*") if p.is_file()):
            rel = src.relative_to(skill_dir)
            dst = install_dir / rel
            results.append(self._install_one(src, dst, dry_run=dry_run, yes=yes))

        return results

    def _install_one(
        self, src: Path, dst: Path, *, dry_run: bool, yes: bool,
    ) -> FileResult:
        # Refuse if the destination already exists as a symlink.
        # `is_symlink()` returns True even if the link target is missing,
        # which is the safer semantics for this check.
        if dst.is_symlink():
            raise OSError(
                f"refusing to write through symlink at {dst} "
                f"(resolve or remove the symlink before re-running)"
            )

        bundled_bytes = src.read_bytes()

        if not dst.exists():
            if dry_run:
                return FileResult(dst, Status.WOULD_CREATE)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            return FileResult(dst, Status.CREATED)

        current_bytes = dst.read_bytes()
        if current_bytes == bundled_bytes:
            return FileResult(dst, Status.UNCHANGED)

        # Conflict. dry-run reports without prompting; --yes overwrites
        # unconditionally; otherwise we drop into the interactive
        # prompt.
        if dry_run:
            return FileResult(dst, Status.WOULD_OVERWRITE)

        if yes:
            shutil.copyfile(src, dst)
            return FileResult(dst, Status.OVERWRITTEN)

        # Render decode-safe content for the diff prompt. SKILL.md and
        # references/*.md are text; .py is text. We assume UTF-8 with
        # replacement for any rare binary content.
        current_text = current_bytes.decode("utf-8", errors="replace")
        bundled_text = bundled_bytes.decode("utf-8", errors="replace")
        decision = prompt.prompt_conflict(dst, current_text, bundled_text)

        if decision == prompt.Decision.OVERWRITE:
            shutil.copyfile(src, dst)
            return FileResult(dst, Status.OVERWRITTEN)
        if decision == prompt.Decision.KEEP:
            return FileResult(dst, Status.KEPT)
        # SKIP
        return FileResult(dst, Status.SKIPPED)


register_adapter("claude-code", ClaudeCodeAdapter())
