# ADR-004 — Retrieval pipeline: channels, RRF, rerank, config cascade

**Status:** Accepted; amended 2026-05-17 (rerank min-coverage gate)
**Cited by:** `search.py`, `graph_channel.py`, `expand.py`,
`config.py`

## Context

Keyword-only retrieval misses semantic matches; pure vector retrieval
misses exact-keyword intent; neither uses the entity graph. The
pipeline must stay free-path-viable ("no LLM required") and degrade
gracefully when channels have no data.

## Decision

### Channels and fusion

Three channels fused by **weighted Reciprocal Rank Fusion**
(`k = 60`; score `Σ weight / (k + rank)`, 1-indexed):

| Channel | Weight | Gate |
|---|---|---|
| `bm25` (FTS5 over title/content/tags + chunk legs) | 1.0 | always on |
| `vector` (sqlite-vec cosine) | `max(0.5 floor, embedder.quality)` (§"auto resolution") | skipped below embedder quality 0.6 |
| `graph` (entity-mediated proximity) | 0.7 | empty-table fast path |

Chunk channels share their memory-level weights; a memory hitting via
both its own FTS row and a chunk row accumulates both (intended).

The vec-weight floor keeps a hypothetical low-quality local embedder
from contributing noise; hosted tiers (q ≥ 0.85) are unaffected.

**Empty-graph invariant:** with an empty `entities` table the graph
channel does no work and 3-channel RRF degrades **byte-for-byte** to
the 2-channel result (free-path floor).

### Graph channel mechanics (`graph_channel.py`)

- Two-layer BFS, depth cap 2, result cap `limit × 5`:
  hop 1 entity-mediated (mentions → entities → relations → mentions
  → memories); hop 2 memory-direct (`edges`, outbound-only —
  asymmetric trust as a ranking signal).
- Relation weights: auto `{implements, contains, depends_on,
  derived_from: 1.0; mentions, references, relates_to: 0.7;
  contradicts, supersedes, alternative_to: 1.1}`; manual edges 1.2.
- Rank curves via `SAGE_GRAPH_RANK_CURVE`: `linear` (default),
  `harmonic`, `type-weighted` (bonuses: conflict trio 1.4, manual
  1.5, mention-family 0.6, unknown 0.8).
- `canonical_id` resolved on read via COALESCE (dedup never rewrites
  mentions); deleted memories filtered by INNER JOIN; visited set
  keeps the shallower depth.

### Query expansion (§"Strong-signal short-circuit")

- bm25 score normalized `abs(s)/(1+abs(s))`; expansion skipped when
  top1_norm ≥ 0.4 **and** (single hit OR top1 ≥ 2.0 × top2)
  (`SAGE_EXPAND_TOP1_NORM`, `SAGE_EXPAND_TOP1_RATIO`).
- Otherwise the LLM produces `{lex, vec, hyde}` variants: lex extends
  the bm25 channel, vec/hyde extend the vector channel.
- No key → silent no-expansion fallback (byte-identity on the free
  path); LLM failure → fallback + WARNING.

### Rerank position-blend (§"Rerank position-blend")

- Top-K (15) LLM rerank blended as
  `w_rrf·rrf + (1−w_rrf)·llm_score` with `w_rrf` by position:
  0.75 (1–3), 0.6 (4–10), 0.4 (11+) (`SAGE_RERANK_BLEND_CURVE`).
- `llm_score=None` → RRF score unchanged (never coerce None to 0).
- **Amendment 2026-05-17 (min-coverage gate):** when the LLM scored
  < 50% of the head (`SAGE_RERANK_MIN_COVERAGE`), keep pure RRF
  order — partial coverage was demoting the LLM-confirmed best below
  un-scored siblings on LongMemEval.

### Configuration cascade (§"Configuration cascade")

Four layers, highest wins: per-call `override=` kwarg → env vars
(mechanical `SAGE_MEMORY_*` + an 11-row legacy alias table preserving
M3a/M3b/M4 env names, one deduped DEBUG line per process per alias)
→ `.sage/config.yaml` in the project root (lazy-loaded, `sage_memory`
namespace) → built-in defaults (the worked YAML, mirrored in
`config.py:_BUILT_IN_DEFAULTS`). Type coercion follows the yaml type
at the key path; env-only keys stay `str`. API keys are env-only
read-only passthroughs, stripped from `get_all()`.

## Consequences

- Default install is BM25-only by design (see README "What runs by
  default", P1-5) — the gates above are what make that honest.
- Every channel failure must degrade to empty-with-log, never crash
  (P1-4 hardened the enforcement of this).

## Gaps

The choice of `k = 60` and the 0.7 graph weight follow RRF convention
/ tuning not recorded in code.
