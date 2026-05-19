"""Codex CLI adapter — installs marker-delimited skill blocks into
`AGENTS.md` (project-scoped) or `~/.codex/AGENTS.md` (global-scoped).
"""

from __future__ import annotations

from sage_memory.cli_install_skills import register_adapter
from sage_memory.install_skills._markdown_adapter_base import MarkdownBlockAdapter


class CodexAdapter(MarkdownBlockAdapter):
    name = "codex"


register_adapter("codex", CodexAdapter())
