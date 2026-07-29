# ADR-005 — Embedder cascade + corpus dim lock

**Status:** Accepted
**Cited by:** `embedder.py`, `migrations/006_embedding_meta.sql`

## Context

Vector quality depends on the embedding model, but the product floor
is "no LLM required, zero dependencies, no network." Hosted models
beat local ones; local neural beats hashing; hashing always works.
Switching models mid-corpus corrupts the vector index if dims differ.

## Decision

### Tiers (cascade order)

| Tier | Embedder | Dim | Quality | Availability |
|---|---|---|---|---|
| T0 | explicit override | — | — | caller-supplied |
| T1 | OpenAI `text-embedding-3-small` | 1536 | 0.85 | `OPENAI_API_KEY` |
| T1 | Voyage `voyage-3-lite` | 512 | 0.85 | `VOYAGE_API_KEY` |
| T1 | Cohere `embed-english-v3.0` | 1024 | 0.85 | `COHERE_API_KEY` |
| T2 | FastEmbedder `BAAI/bge-small-en-v1.5` | 384 | 0.85 | `[neural]` extra |
| T3 | LocalEmbedder (char 3–5-gram TF-IDF hashing) | 384 | 0.45 | always |

Char n-grams capture morphological similarity ("authenticate" ↔
"auth" ↔ "OAuth" share trigrams), effective for LLM-authored content
where vocabulary is consistent.

### Corpus dim lock (with ADR-001, migration 006)

`corpus_meta.vec_dim` locks the corpus to the first writer's dim
(default 384). Per-vector provenance `(model_name, model_version,
dim, created_at)`; staleness = meta mismatch or missing meta row
(§Staleness handling).

### Resolver rule (§Resolver rule, six worked scenarios)

1. Candidates = available embedders whose native dim == corpus dim.
2. Pick the highest tier among candidates (T1 > T2 > T3; within T1:
   OpenAI → Voyage → Cohere). Log when a higher-tier embedder is
   available but dim-mismatched (hint:
   `sage-memory reindex --re-embed --embedder <name>`).
3. No candidates → raise `DimMismatchRefuseError`. **Silent
   down-projection is explicitly rejected.**

### Hosted-call tunables (§Pinned design decisions)

Mean-pool cap 32 segments for over-long inputs (then L2-normalize +
`[truncated]` sentinel), 3 retry attempts, 0.5s doubling backoff,
30s HTTP timeout.

## Consequences

- Default installs run T3 (quality 0.45 < the 0.6 vector gate), so
  the vector channel is off until `[neural]` or a hosted key — the
  BM25-only default state documented in the README (P1-5).
- Re-embedding is a deliberate, dim-locked operation, never implicit.

## Gaps

The 0.85/0.45 quality scores are calibration estimates; methodology
not recorded in code.
