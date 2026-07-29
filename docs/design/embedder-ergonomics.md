# Design: Embedder ergonomics

**Task:** P2-2 (SM-DOC-01, capability half) · **Depends:** P1-5 (merged) ·
**Brief per:** `06-spec-phase2-capability.md §P2-2`

## Problem

The default state is BM25-only (documented honestly in P1-5): the T3
TF-IDF embedder sits below the vector gate, and after a code scan
memories sit `stale: N` with no obvious next step. Free-path recall
is strong (97.2% R@5) — this task makes the *better* path easy; it
does not change defaults.

## Decisions

### 1. Staleness → actionable next step (prompt, NOT auto-enqueue)

`sage-memory status` already prints the stale count. It now ends with
a context-aware hint:

- stale > 0 and `[neural]` installed →
  `Next: sage-memory reindex --embeddings` (catch up stale rows with
  the ACTIVE embedder — no model change).
- stale > 0 and `[neural]` NOT installed →
  `Next: sage-memory embedder use fastembed` (one-command upgrade).
- stale == 0 → no hint.

**Auto-enqueue rejected**: embedding 458 memories silently on scan
changes cost/latency behavior without consent, and with T3 active it
would churn the corpus into TF-IDF vectors the user may not want
(the upgrade they actually want is usually fastembed/hosted).
Prompting preserves the zero-surprise floor.

### 2. One-command upgrade: `sage-memory embedder use <name>`

New `cli_embedder.py` with one subcommand: `use <name>` where
`<name> ∈ {fastembed, openai, voyage, cohere, local}`.

Flow (`use fastembed` shown; others analogous):

1. **Validate the tier is usable.** fastembed → `import fastembed`
   probe; on failure print the exact install command
   (`pip install 'sage-memory[neural]'` / uvx variant) and exit 2.
   Hosted tiers → require the matching `*_API_KEY` env (never prompt
   for it; print which var is missing, exit 2).
2. **Dim-migration path:** delegate to the existing, tested
   `cli_reindex._do_full_reembed(embedder_name=<name>)` — backup of
   the old vec tables, recreate at the new dim, `corpus_meta` update,
   enqueue re-embed. No new migration logic here.
3. Print what happened: backup timestamps, new dim, and the next
   step (`sage-memory reindex --embeddings` if rows remain stale,
   or restart the MCP server so the resolver picks up the new
   embedder).

`use local` is the downgrade path back to the zero-dep floor (dim
384 → fast path, still via `_do_full_reembed`).

### 3. Zero-dep floor unchanged (invariant 1)

No new imports on the default path: `cli_embedder.py` lazy-imports
`fastembed` only inside the validate step; `local`/T3 remains the
default embedder; a base-deps install passes the full test suite
(CI job 1 proves it).

### 4. ANN — measured, NOT built

Measurement (this machine, 384-dim float32, sqlite-vec MATCH,
20-query average, in-memory):

| vectors | ms/query |
|---|---|
| 1,000 | 0.2 |
| 10,000 | 1.9 |
| 50,000 | 10.8 |
| 100,000 | 21.5 |

Brute-force cosine is ~21ms even at 100K vectors — an order of
magnitude under the 200ms interactive budget at any realistic
per-project corpus. **No ANN index is justified.** Revisit if a
project corpus exceeds ~500K vectors or the 200ms budget tightens;
the measurement harness is 20 lines (recorded in the PR).

## Surfaces

- CLI: `sage-memory embedder use <name>` (new `cli_embedder.py`;
  dispatch in `__init__.py`). No MCP tool — this is operator
  ergonomics, not agent surface.
- `cli_status.py`: next-step hint lines per §1.

## Testing plan

- `embedder use fastembed` with fastembed missing → exit 2 + install
  hint (monkeypatch import).
- `embedder use openai` without key → exit 2 naming the env var.
- `use fastembed` happy path → delegates to `_do_full_reembed` with
  the right name (spy).
- Status hint: stale>0 both branches; stale==0 no hint.
- Base-deps suite green (floor proof).
