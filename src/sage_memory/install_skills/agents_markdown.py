"""Shared renderer for `AGENTS.md`-style targets (codex, opencode).

Produces a marker-delimited block per skill so re-installs can locate
and replace the exact prior block without touching surrounding user
content. Relative `references/*` markdown links in the SKILL.md body
are rewritten to absolute paths pointing at the bundled wheel
location, and a "Bundled resources" footer lists every reference
file's absolute path for tools that don't follow markdown links.
"""

from __future__ import annotations

import re
from pathlib import Path

from sage_memory.install_skills import markers


_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n?", re.DOTALL)

# Markdown link target: `[text](references/foo.md)`.
_REL_MD_LINK_RE = re.compile(r"\]\(((?:references|scripts)/[^)]+)\)")

# Backtick-quoted relative path: `` `references/foo.md` ``. SKILL.md
# files commonly cite reference paths in bullet lists this way.
_REL_BACKTICK_RE = re.compile(r"`((?:references|scripts)/[^`]+)`")


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1).lstrip("\n")


def _rewrite_relative_links(body: str, skill_dir: Path) -> str:
    """Rewrite both `[text](references/foo.md)` and `` `references/foo.md` ``
    to absolute paths under the bundled skill_dir, so users reading
    AGENTS.md / GEMINI.md can resolve the references.

    Refuses to rewrite a path that resolves outside `skill_dir` (a `..`
    segment would escape the bundle); such links are left unchanged so
    a malicious-looking link can't appear in the rendered block as if
    sage-memory endorsed it.
    """
    skill_root = skill_dir.resolve()

    def _resolve_safely(rel: str) -> Path | None:
        absolute = (skill_dir / rel).resolve()
        try:
            absolute.relative_to(skill_root)
        except ValueError:
            return None
        return absolute

    def _md(m: re.Match) -> str:
        absolute = _resolve_safely(m.group(1))
        return m.group(0) if absolute is None else f"]({absolute})"

    def _tick(m: re.Match) -> str:
        absolute = _resolve_safely(m.group(1))
        return m.group(0) if absolute is None else f"`{absolute}`"

    body = _REL_MD_LINK_RE.sub(_md, body)
    body = _REL_BACKTICK_RE.sub(_tick, body)
    return body


def _render_bundled_resources_footer(skill_dir: Path) -> str:
    """List absolute paths of every file in references/ and scripts/
    so tools that don't resolve markdown links still have pointers."""
    extra_dirs = [d for d in (skill_dir / "references", skill_dir / "scripts")
                  if d.is_dir()]
    if not extra_dirs:
        return ""
    lines = ["", "### Bundled resources", ""]
    for d in extra_dirs:
        for p in sorted(d.rglob("*")):
            if p.is_file():
                lines.append(f"- `{p.resolve()}`")
    return "\n".join(lines) + "\n"


def render_block(*, skill_name: str, version: str, skill_dir: Path) -> str:
    """Return the marker-delimited block for one skill.

    Output is a complete formatted block (begin marker → version line →
    body → end marker) ready to drop into AGENTS.md / GEMINI.md /
    OpenCode AGENTS.md via `markers.replace_or_append`.
    """
    skill_md = (skill_dir / "SKILL.md").read_text()
    body = _strip_frontmatter(skill_md)
    body = _rewrite_relative_links(body, skill_dir)
    footer = _render_bundled_resources_footer(skill_dir)
    full_body = body.rstrip() + "\n\n" + footer if footer else body
    return markers.format_block(name=skill_name, version=version, body=full_body)
