# ADR-002 — Structural-first chunking

**Status:** Accepted
**Cited by:** `chunker.py`, `store.py`

## Context

ADR-001 gives long memories chunk rows with their own indexes. The
split strategy determines retrieval quality: naive fixed-size splits
break markdown structure and code blocks mid-unit.

## Decision (per `chunker.py`, "Constants pinned to ADR-002 §Decision")

Structural-first algorithm (`chunker_split` →
`list[(content, byte_start, byte_end)]`):

1. `len(content) <= CHUNK_THRESHOLD` (2000) → atomic, no chunks.
2. Format detect: markdown if any heading present, else plain.
3. Structural split: markdown at heading boundaries (`#`/`##`/`###`
   at line start); plain text on blank-line paragraph breaks. Code
   fences (triple-backtick) are **atomic** — never split inside.
4. Segments > `MAX_CHUNK_SIZE` get a fixed-size fallback at
   `TARGET_CHUNK_SIZE` with `CHUNK_OVERLAP` on whitespace breakpoints.
5. Trailing segments < `MIN_CHUNK_SIZE` merge into the previous
   (orphan suppression).
6. `MAX_CHUNKS_PER_MEMORY` is **not** enforced by the splitter: all
   chunks are stored as rows; only `chunks_vec` inserts past the cap
   are deferred (§Failure Modes) with a logged warning.
7. Binary-ish content (no whitespace breakpoint in the window):
   hard-cut at byte boundary — no infinite loop, no error.

Update hysteresis (`HYSTERESIS_LOW` = 1500 band around the threshold)
prevents re-chunk churn on small edits near the boundary; `force=`
bypasses for the in-band re-chunk path.

## Consequences

- Chunk hits fold back to parent memory at search time (ADR-004
  stage 4, dedup/rollup), so over-cap memories keep full FTS coverage
  even when vec coverage is deferred.
- The module is pure (no DB / no embedder deps), keeping the policy
  unit-testable.

## Gaps

Exact values of `MAX_CHUNK_SIZE` / `TARGET_CHUNK_SIZE` /
`CHUNK_OVERLAP` / `MIN_CHUNK_SIZE` are pinned in `chunker.py`; the
reasoning behind the specific numbers is not recorded.
