"""Structural-first text chunking for long memories.

Implements ADR-002 §Decision. Pure module — no DB / no embedder
dependencies. Returns `list[tuple[content, byte_start, byte_end]]`
relative to the source string.

Algorithm:
  1. If len(content) <= CHUNK_THRESHOLD: return [] (caller treats as atomic).
  2. Detect format: markdown if any heading present, else plain.
  3. Structural split:
       - markdown: split at heading boundaries (#, ##, ### at line start)
       - plain:    split on blank-line paragraph breaks
       - code fences (triple-backtick) are atomic — never split inside
  4. For each segment > MAX_CHUNK_SIZE: fixed-size fallback at
     TARGET_CHUNK_SIZE with CHUNK_OVERLAP on whitespace breakpoints.
  5. Merge trailing segments < MIN_CHUNK_SIZE into the previous (orphan suppression).
  6. Cap at MAX_CHUNKS_PER_MEMORY is NOT enforced here — the caller
     defers vec embedding for chunks beyond the cap but ALL chunks
     are returned and stored as rows (per ADR-002 §Failure Modes).
  7. Binary-ish content (no whitespace breakpoint within target window):
     hard-cut at byte boundary — no infinite loop, no error.
"""

from __future__ import annotations

import re

# Constants pinned to ADR-002 §Decision
CHUNK_THRESHOLD = 2000
HYSTERESIS_LOW = 1500
MAX_CHUNK_SIZE = 1200
TARGET_CHUNK_SIZE = 600
CHUNK_OVERLAP = 60
MIN_CHUNK_SIZE = 120
MAX_CHUNKS_PER_MEMORY = 200


_FENCE_RE = re.compile(r"^```", re.MULTILINE)
_HEADING_RE = re.compile(r"^(#{1,6})\s", re.MULTILINE)


def split(content: str, *, force: bool = False) -> list[tuple[str, int, int]]:
    """Split content into chunks. Each tuple is (chunk_text, byte_start, byte_end).

    Returns [] when content is too short to chunk (caller treats as atomic),
    UNLESS `force=True` (used by the hysteresis in-band re-chunk path
    where the caller knows the memory should stay chunked even at sub-
    threshold length per ADR-002 §Hysteresis).
    """
    if not content or not content.strip():
        return []
    if not force and len(content) <= CHUNK_THRESHOLD:
        return []

    # Phase A: find structural segment boundaries while keeping code-fenced
    # blocks atomic. We compute a list of (segment_text, segment_start) tuples.
    segments = _structural_split(content)

    # Phase B: each oversize segment gets fixed-size sub-split.
    chunks: list[tuple[str, int, int]] = []
    for seg_text, seg_start in segments:
        if len(seg_text) <= MAX_CHUNK_SIZE:
            chunks.append((seg_text, seg_start, seg_start + len(seg_text)))
        else:
            chunks.extend(_fixed_size_split(seg_text, seg_start))

    # Phase C: merge tiny trailing chunks into their predecessor.
    chunks = _suppress_orphans(chunks)
    return chunks


# ───────────────────────────────────────────────────────────────────
# Internals
# ───────────────────────────────────────────────────────────────────


def _structural_split(content: str) -> list[tuple[str, int]]:
    """Split into top-level segments respecting code fences. Returns
    [(segment_text, segment_start_offset), ...]."""
    fence_spans = _find_fence_spans(content)

    # If markdown headings exist outside any fence, split on those.
    heading_boundaries = [
        m.start() for m in _HEADING_RE.finditer(content)
        if not _within_any_span(m.start(), fence_spans)
    ]
    if heading_boundaries:
        boundaries = [0] + heading_boundaries + [len(content)]
    else:
        # Plain prose: split on blank-line paragraph breaks (outside fences).
        boundaries = [0]
        for m in re.finditer(r"\n\s*\n", content):
            if not _within_any_span(m.start(), fence_spans):
                boundaries.append(m.end())
        boundaries.append(len(content))
        boundaries = sorted(set(boundaries))

    segments: list[tuple[str, int]] = []
    for start, end in zip(boundaries, boundaries[1:]):
        seg = content[start:end]
        if seg.strip():
            # Leading whitespace stripped from text but offset preserved
            leading_ws = len(seg) - len(seg.lstrip())
            segments.append((seg[leading_ws:].rstrip("\n"), start + leading_ws))
    return segments


def _fixed_size_split(text: str, base_offset: int) -> list[tuple[str, int, int]]:
    """Fixed-size split at TARGET_CHUNK_SIZE with overlap, preferring
    whitespace/punctuation breakpoints. Falls back to a hard byte cut
    when no breakpoint exists in the search window."""
    out: list[tuple[str, int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        target_end = min(i + TARGET_CHUNK_SIZE, n)
        if target_end == n:
            out.append((text[i:n], base_offset + i, base_offset + n))
            break
        # Look for a whitespace/punct break in a window around target_end
        cut = _find_break(text, target_end, lookback=200)
        if cut <= i:  # no breakpoint in window — hard cut
            cut = target_end
        out.append((text[i:cut], base_offset + i, base_offset + cut))
        i = max(cut - CHUNK_OVERLAP, i + 1)
    return out


def _find_break(text: str, target: int, lookback: int) -> int:
    """Find a breakpoint <= target (within lookback chars), preferring
    paragraph > sentence > whitespace. Returns target if none found."""
    window_start = max(0, target - lookback)
    window = text[window_start:target]
    # Paragraph break (newline+newline)
    idx = window.rfind("\n\n")
    if idx != -1:
        return window_start + idx + 2
    # Sentence break
    for term in (". ", "! ", "? ", ".\n", "!\n", "?\n"):
        idx = window.rfind(term)
        if idx != -1:
            return window_start + idx + len(term)
    # Any whitespace
    idx = window.rfind(" ")
    if idx != -1:
        return window_start + idx + 1
    idx = window.rfind("\n")
    if idx != -1:
        return window_start + idx + 1
    return target  # hard cut


def _find_fence_spans(content: str) -> list[tuple[int, int]]:
    """Find all triple-backtick fenced regions. Returns list of
    (start, end) byte ranges INCLUDING the fence markers."""
    spans: list[tuple[int, int]] = []
    matches = list(_FENCE_RE.finditer(content))
    i = 0
    while i + 1 < len(matches):
        open_m = matches[i]
        close_m = matches[i + 1]
        # Close fence end = end of line containing the closing ```
        line_end = content.find("\n", close_m.end())
        if line_end == -1:
            line_end = len(content)
        spans.append((open_m.start(), line_end))
        i += 2
    return spans


def _within_any_span(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in spans)


def _suppress_orphans(chunks: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    """Merge any chunk < MIN_CHUNK_SIZE into its predecessor."""
    if not chunks:
        return chunks
    out: list[tuple[str, int, int]] = [chunks[0]]
    for content, start, end in chunks[1:]:
        if len(content) < MIN_CHUNK_SIZE and out:
            prev_text, prev_start, _ = out[-1]
            merged_text = prev_text + "\n" + content
            out[-1] = (merged_text, prev_start, end)
        else:
            out.append((content, start, end))
    return out
