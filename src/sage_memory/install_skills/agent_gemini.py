"""Gemini CLI adapter — installs marker-delimited skill blocks into
`GEMINI.md` (project-scoped) or `~/.gemini/GEMINI.md` (global-scoped).
Uses the same shared renderer as the AGENTS.md adapters.
"""

from __future__ import annotations

from sage_memory.cli_install_skills import register_adapter
from sage_memory.install_skills._markdown_adapter_base import MarkdownBlockAdapter


class GeminiAdapter(MarkdownBlockAdapter):
    name = "gemini"


register_adapter("gemini", GeminiAdapter())
