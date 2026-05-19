"""OpenCode adapter — installs marker-delimited skill blocks into
`AGENTS.md` (project-scoped) or `~/.config/opencode/AGENTS.md`
(global-scoped). Uses the same shared renderer as the Codex adapter.

When both `codex` and `opencode` are requested with `--project`, both
adapters target `./AGENTS.md`. Both run; the second pass detects that
the marker block's body is byte-equal to the bundled version and
reports UNCHANGED — so the file is opened twice but written once.
"""

from __future__ import annotations

from sage_memory.cli_install_skills import register_adapter
from sage_memory.install_skills._markdown_adapter_base import MarkdownBlockAdapter


class OpenCodeAdapter(MarkdownBlockAdapter):
    name = "opencode"


register_adapter("opencode", OpenCodeAdapter())
