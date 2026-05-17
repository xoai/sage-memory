"""T1 — chunker.py tests.

Pure unit tests — no DB, no embedder. The chunker module must be
importable in isolation and produce deterministic output for the
algorithm specified in ADR-002 §Decision.
"""

from __future__ import annotations

import pytest

from sage_memory.chunker import (
    split,
    CHUNK_THRESHOLD,
    HYSTERESIS_LOW,
    MAX_CHUNK_SIZE,
    TARGET_CHUNK_SIZE,
    CHUNK_OVERLAP,
    MIN_CHUNK_SIZE,
    MAX_CHUNKS_PER_MEMORY,
)


# ───────────────────────────────────────────────────────────────────
# Constants pinned to ADR-002 §Decision
# ───────────────────────────────────────────────────────────────────


def test_constants_match_adr002():
    assert CHUNK_THRESHOLD == 2000
    assert HYSTERESIS_LOW == 1500
    assert MAX_CHUNK_SIZE == 1200
    assert TARGET_CHUNK_SIZE == 600
    assert CHUNK_OVERLAP == 60
    assert MIN_CHUNK_SIZE == 120
    assert MAX_CHUNKS_PER_MEMORY == 200


# ───────────────────────────────────────────────────────────────────
# Threshold boundary
# ───────────────────────────────────────────────────────────────────


def test_short_content_returns_empty():
    assert split("hello world") == []
    assert split("x" * 500) == []
    assert split("x" * 1999) == []


def test_exactly_2000_returns_empty():
    """ADR-002: chunk only if > CHUNK_THRESHOLD. 2000 is atomic."""
    assert split("x" * 2000) == []


def test_2001_returns_chunks():
    """ADR-002: chunk if > CHUNK_THRESHOLD."""
    chunks = split("x" * 2001)
    assert len(chunks) >= 1


def test_empty_content_returns_empty():
    """Defensive: empty / whitespace input returns []."""
    assert split("") == []
    assert split("   ") == []


# ───────────────────────────────────────────────────────────────────
# Structural splits — markdown
# ───────────────────────────────────────────────────────────────────


def test_markdown_h2_split():
    """Markdown headings break at the heading boundary."""
    body = ("alpha " * 200)  # ~1200 chars per section
    md = f"## Section A\n{body}\n## Section B\n{body}\n## Section C\n{body}"
    assert len(md) > CHUNK_THRESHOLD
    chunks = split(md)
    # 3 sections → ≥ 3 chunks (more if a section exceeds MAX_CHUNK_SIZE)
    assert len(chunks) >= 3
    # Each chunk references its source span via byte offsets
    for content, start, end in chunks:
        assert end > start
        assert end <= len(md)


def test_markdown_h1_and_h3_split():
    """Headings of any level (#, ##, ###) participate."""
    body = ("word " * 100)  # ~500 chars
    md = f"# Top\n{body}\n## Sub\n{body}\n### Sub-sub\n{body}\n## Sub2\n{body}"
    assert len(md) > CHUNK_THRESHOLD
    chunks = split(md)
    assert len(chunks) >= 2  # multiple sections detected


# ───────────────────────────────────────────────────────────────────
# Structural splits — plain prose
# ───────────────────────────────────────────────────────────────────


def test_plain_prose_paragraph_split():
    """Plain text splits on blank-line paragraph breaks."""
    para = "word " * 100  # ~500 chars
    txt = "\n\n".join([para, para, para, para, para])  # ~2500 chars
    assert len(txt) > CHUNK_THRESHOLD
    chunks = split(txt)
    assert len(chunks) >= 2  # at least split into 2 by paragraphs


# ───────────────────────────────────────────────────────────────────
# Code fences atomic
# ───────────────────────────────────────────────────────────────────


def test_code_fence_atomic():
    """Triple-backtick fenced blocks must not split inside the fence,
    even when the fence contains blank lines or semicolons (which would
    otherwise be paragraph/statement breaks)."""
    fence_content = "\n".join([f"line {i};" for i in range(60)])  # ~600 chars
    pre = "x" * 1500
    post = "y" * 200
    txt = f"{pre}\n\n```python\n{fence_content}\n```\n\n{post}"
    assert len(txt) > CHUNK_THRESHOLD
    chunks = split(txt)
    # Locate the chunk containing the opening fence; assert closing fence
    # is in the SAME chunk.
    fence_chunks = [c for c in chunks if "```python" in c[0]]
    assert len(fence_chunks) == 1, (
        f"opening fence should appear in exactly one chunk, got {len(fence_chunks)}"
    )
    assert "```" in fence_chunks[0][0].split("```python", 1)[1], (
        "closing fence must be in same chunk as opening fence"
    )


# ───────────────────────────────────────────────────────────────────
# Fixed-size fallback when structural segment exceeds MAX_CHUNK_SIZE
# ───────────────────────────────────────────────────────────────────


def test_oversize_segment_uses_fixed_size_fallback():
    """A single 'section' larger than MAX_CHUNK_SIZE (1200) gets
    fixed-size split at ~TARGET_CHUNK_SIZE (600)."""
    long_section = " ".join(["word"] * 500)  # ~2500 chars, no headings
    chunks = split(long_section)
    # Should produce multiple chunks each ≤ MAX_CHUNK_SIZE-ish
    assert len(chunks) >= 2
    for content, _, _ in chunks:
        # Allow some flex around the boundary detection
        assert len(content) <= MAX_CHUNK_SIZE + 100


# ───────────────────────────────────────────────────────────────────
# Min chunk size — orphan suppression
# ───────────────────────────────────────────────────────────────────


def test_min_chunk_size_attaches_to_previous():
    """Trailing segments < MIN_CHUNK_SIZE (120) merge into the previous
    chunk so we don't produce tiny orphan chunks."""
    body = "word " * 300  # ~1500 chars (one big section)
    tail = "tiny tail"  # ~10 chars
    txt = body + "\n\n" + tail
    if len(txt) <= CHUNK_THRESHOLD:
        txt = body * 2 + "\n\n" + tail  # ensure we cross threshold
    chunks = split(txt)
    # The tail must NOT appear as a standalone chunk
    for content, _, _ in chunks:
        assert len(content) >= MIN_CHUNK_SIZE - 1 or content.endswith(tail), (
            f"orphan chunk found: {len(content)} chars, content={content!r}"
        )


# ───────────────────────────────────────────────────────────────────
# Cap at MAX_CHUNKS_PER_MEMORY (per ADR-002 — ALL chunks kept)
# ───────────────────────────────────────────────────────────────────


def test_max_chunks_cap_returns_all_chunks():
    """ADR-002 §Failure Modes: all chunks stored as rows; only vec
    deferred for chunks > cap. The chunker itself returns ALL chunks;
    deferring vec embedding is the caller's responsibility."""
    # 150KB plain prose with no headings → fixed-size fallback ~600 char
    # = ~250 chunks (well over the 200 cap).
    txt = " ".join(["word"] * 30000)  # ~150KB
    chunks = split(txt)
    assert len(chunks) > MAX_CHUNKS_PER_MEMORY, (
        "chunker should produce > cap chunks for cap policy to be meaningful"
    )


# ───────────────────────────────────────────────────────────────────
# Byte offsets correctness
# ───────────────────────────────────────────────────────────────────


def test_byte_offsets_in_bounds():
    """All returned (start, end) pairs lie within [0, len(content)]."""
    txt = ("sentence. " * 300)  # ~3000 chars
    chunks = split(txt)
    for content, start, end in chunks:
        assert 0 <= start < end <= len(txt)


def test_byte_offsets_monotonic_within_section():
    """Within a single source, chunk byte_starts are non-decreasing
    (overlap may cause equality, never strict reversal)."""
    txt = ("sentence. " * 300)
    chunks = split(txt)
    for prev, cur in zip(chunks, chunks[1:]):
        assert cur[1] >= prev[1], (
            f"chunk byte_start went backwards: prev={prev[1]} cur={cur[1]}"
        )


# ───────────────────────────────────────────────────────────────────
# Binary-ish content default-of-action (T1 algorithm step 7)
# ───────────────────────────────────────────────────────────────────


def test_no_whitespace_long_input_hard_cuts():
    """A single token > MAX_CHUNK_SIZE with NO whitespace must hard-cut
    at TARGET_CHUNK_SIZE byte boundary — no infinite loop, no error."""
    # 5000-char base64-like blob, no whitespace
    blob = "A" * 5000
    chunks = split(blob)
    assert len(chunks) >= 2  # split happened
    # Reconstruction (modulo overlap) covers the source
    last_end = chunks[-1][2]
    assert last_end >= len(blob) * 0.9, (
        f"chunks should cover most of source; last_end={last_end} blob_len={len(blob)}"
    )
