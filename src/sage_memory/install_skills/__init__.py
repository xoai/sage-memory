"""install-skills CLI implementation package.

Per-agent adapters live in `agent_*.py` siblings and self-register
into the `_ADAPTERS` dict imported by `cli_install_skills.py` at the
top-level package boundary. Shared utilities (`markers`, `paths`,
`prompt`) are imported by every adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from sage_memory.install_skills import markers, paths, prompt


class Status(Enum):
    """Per-file install result. Used by adapters to report what
    happened to each file they touched."""

    CREATED = "created"
    UNCHANGED = "unchanged"
    OVERWRITTEN = "overwrote"
    KEPT = "kept"
    SKIPPED = "skipped"
    WOULD_CREATE = "would create"
    WOULD_OVERWRITE = "would overwrite"


@dataclass(frozen=True)
class FileResult:
    """One file's install outcome. Returned in a list by
    `Adapter.install_to`."""

    path: Path
    status: Status


__all__ = ["markers", "paths", "prompt", "Status", "FileResult"]
