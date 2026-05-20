"""Marker-block format + locate/replace logic for AGENTS.md /
GEMINI.md style targets.

A "block" wraps SKILL.md content with begin/end markers so re-installs
can locate and replace exactly the right slice without touching
surrounding user content:

    <!-- sage-memory:skill:<name>:begin -->
    <!-- sage-memory version: 0.8.0 -->
    # <skill title>
    …content…
    <!-- sage-memory:skill:<name>:end -->

The version line lives inside the block (not in the markers), so it's
excluded from body-equality checks. That's what keeps re-installs
across version bumps idempotent: bumping 0.7.x → 0.8.x with no skill
changes shows "unchanged" rather than a spurious diff prompt.
"""

from __future__ import annotations

import re

BEGIN_TPL = "<!-- sage-memory:skill:{name}:begin -->"
END_TPL = "<!-- sage-memory:skill:{name}:end -->"
VERSION_TPL = "<!-- sage-memory version: {version} -->"


def format_block(name: str, version: str, body: str) -> str:
    """Wrap body with begin / version-line / end markers.

    Returned string has no trailing newline; callers concatenate with
    surrounding content.
    """
    begin = BEGIN_TPL.format(name=name)
    end = END_TPL.format(name=name)
    version_line = VERSION_TPL.format(version=version)
    body_stripped = body.rstrip("\n")
    return f"{begin}\n{version_line}\n{body_stripped}\n{end}"


def find_block(text: str, name: str) -> tuple[int, int] | None:
    """Locate the begin/end span for `name`. Returns (begin_start,
    end_end_exclusive) or None if either marker is absent.

    Markers must appear at the start of a line (preceded by `\\n` or
    at position 0). This rejects literal marker text accidentally
    pasted into a user-edited block body — if a user's content
    contains `<!-- sage-memory:skill:X:end -->` mid-paragraph, we
    skip past it and look for the real, line-anchored marker. Keeps
    round-trips safe even when SKILL.md bodies discuss markers.
    """
    begin = BEGIN_TPL.format(name=name)
    end = END_TPL.format(name=name)

    def _find_line_anchored(needle: str, start: int) -> int:
        # First, check position `start` itself (covers text starting with marker).
        if text.startswith(needle, start):
            return start
        # Otherwise, look for newline-anchored occurrences.
        anchor = "\n" + needle
        idx = text.find(anchor, start)
        return idx + 1 if idx >= 0 else -1

    begin_idx = _find_line_anchored(begin, 0)
    if begin_idx < 0:
        return None
    end_idx = _find_line_anchored(end, begin_idx + len(begin))
    if end_idx < 0:
        return None
    return (begin_idx, end_idx + len(end))


def extract_body(text: str, name: str) -> str | None:
    """Return the block's body — content between markers with the
    version line stripped. Preserves the body's trailing newline.
    None if no block found.

    Line endings are normalized to LF before extraction so a CRLF
    checkout (e.g. git autocrlf on Windows) produces the same body as
    an LF checkout — otherwise the body-equality check would diff
    every install across platforms.
    """
    text = text.replace("\r\n", "\n")
    span = find_block(text, name)
    if span is None:
        return None
    begin_str = BEGIN_TPL.format(name=name)
    end_str = END_TPL.format(name=name)
    block = text[span[0]:span[1]]
    after_begin = block[len(begin_str):].lstrip("\n")
    if after_begin.endswith(end_str):
        before_end = after_begin[:-len(end_str)]
    else:
        before_end = after_begin
    version_re = re.compile(r"^<!-- sage-memory version: [^>]+ -->\n")
    return version_re.sub("", before_end, count=1)


def bodies_equal(block_a: str, block_b: str, *, name: str) -> bool:
    """Compare the body content of two formatted blocks, ignoring the
    version line and the markers themselves.
    """
    a = extract_body(block_a, name)
    b = extract_body(block_b, name)
    if a is None or b is None:
        return False
    return a == b


def replace_or_append(text: str, name: str, new_block: str) -> str:
    """Replace the existing block (if present) or append `new_block`
    with two leading newlines (if absent).

    Truncated input (begin marker but no end marker) is treated as
    "no block" — the new block is appended, the truncated remnant is
    preserved verbatim. This keeps the operation lossless: if the
    user manually mangled the markers, we don't silently delete
    their content.
    """
    span = find_block(text, name)
    if span is None:
        if not text:
            return new_block
        # Ensure exactly one blank line separator between existing
        # content and the new block.
        return text.rstrip("\n") + "\n\n" + new_block
    begin, end = span
    return text[:begin] + new_block + text[end:]


def delete_block_by_name(text: str, name: str) -> str:
    """Remove ONE block named `name` from `text`. Returns `text`
    unchanged if no block found.

    Consumes up to 2 trailing newlines after the deleted block (the
    one separating the end marker from the next content, plus an
    optional blank-line padding line) so repeated migrations don't
    accumulate empty lines between sibling content.

    Used by `MarkdownBlockAdapter` to migrate legacy-named skill
    blocks (e.g., `<!-- sage-memory:skill:memory:begin -->` from
    pre-0.10.0 installs) into the new prefixed namespace on re-install.
    """
    span = find_block(text, name)
    if span is None:
        return text
    begin, end = span
    # Consume the newline that separates the end-marker from the next
    # content (always present in well-formed blocks emitted by
    # `format_block`).
    if end < len(text) and text[end] == "\n":
        end += 1
    # Consume one MORE newline if that produces a blank line —
    # collapses `before\n\n<block>\n\nafter` into `before\n\nafter`
    # rather than `before\n\n\nafter`.
    if end < len(text) and text[end] == "\n":
        end += 1
    return text[:begin] + text[end:]
