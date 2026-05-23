"""Filesystem walker for the codebase scanner.

Yields ``(rel_path, language_tag, grammar_name, query_id)`` tuples for
every file under ``root`` whose extension is known. Behavior contract
from spec rev 3 §"File-walker + gitignore":

- ``.gitignore`` is honored when ``git`` is on PATH and ``root`` is
  inside a git work-tree, unless ``include_ignored=True``.
- ``SKIP_DIRS`` is always pruned, even when ``include_ignored=True``.
- ``.h`` files are resolved via the per-directory C/C++ heuristic in
  ``_languages.resolve_h_file`` — populated once per directory and
  cached for the duration of one walk.
- Unknown extensions are silently skipped (no log spam).
- Fallback paths: ``git`` missing, ``git ls-files`` times out, or
  exits non-zero → fall back to SKIP_DIRS-only pruning.

The walker has NO tree-sitter dependency; it is safe to import without
the ``[codebase]`` extra installed.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Iterable, Iterator

from ._languages import EXT_MAP, resolve_h_file


logger = logging.getLogger(__name__)


SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", "bower_components",
    "dist", "build", "target", "vendor",
    ".venv", ".tox", "venv", "env",
    ".gradle", ".idea", ".vscode",
    "obj", "bin",
})

GIT_LS_FILES_TIMEOUT_S = 10


def _git_tracked_files(root: Path) -> set[str] | None:
    """Return POSIX-style relative paths git lists as tracked or
    untracked-but-not-ignored under ``root``. ``None`` means "git
    unavailable / not a repo / fallback to SKIP_DIRS only".
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=GIT_LS_FILES_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        logger.info("git not on PATH; falling back to hardcoded skip-list")
        return None
    except subprocess.TimeoutExpired:
        logger.warning(
            "git ls-files timed out after %ds; falling back to hardcoded skip-list",
            GIT_LS_FILES_TIMEOUT_S,
        )
        return None

    if result.returncode != 0:
        logger.warning(
            "git ls-files exited %d; falling back to hardcoded skip-list",
            result.returncode,
        )
        return None

    return {line for line in result.stdout.splitlines() if line.strip()}


def walk(
    root: Path | str,
    languages: Iterable[str] | None = None,
    include_ignored: bool = False,
) -> Iterator[tuple[Path, str, str, str]]:
    """Walk ``root`` yielding ``(rel_path, language_tag, grammar_name,
    query_id)`` tuples.

    Parameters
    ----------
    root:
        Directory to scan.
    languages:
        Optional restriction to a subset of language tags
        (e.g. ``["py", "ts"]``). ``None`` means "all known languages".
    include_ignored:
        When ``True``, ``.gitignore`` filtering is skipped entirely.
        ``SKIP_DIRS`` is still pruned.
    """
    root_path = Path(root).resolve()
    lang_filter = frozenset(languages) if languages else None

    tracked: set[str] | None = None
    if not include_ignored:
        tracked = _git_tracked_files(root_path)

    h_cache: dict[Path, tuple[str, str, str]] = {}

    # os.walk with sorted dirs/files gives deterministic ordering across
    # OSes — important for the `.h` heuristic cache and for stable diffs
    # in the scan summary output.
    for dirpath_str, dirnames, filenames in os.walk(root_path, topdown=True):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        filenames.sort()

        dirpath = Path(dirpath_str)
        for fname in filenames:
            fpath = dirpath / fname
            rel = fpath.relative_to(root_path)
            rel_posix = rel.as_posix()

            if tracked is not None and rel_posix not in tracked:
                continue

            ext = fpath.suffix.lower()
            if ext in EXT_MAP:
                triple = EXT_MAP[ext]
            elif ext == ".h":
                triple = h_cache.get(dirpath)
                if triple is None:
                    triple = resolve_h_file(dirpath)
                    h_cache[dirpath] = triple
            else:
                continue

            language_tag, grammar_name, query_id = triple
            if lang_filter is not None and language_tag not in lang_filter:
                continue

            yield rel, language_tag, grammar_name, query_id
