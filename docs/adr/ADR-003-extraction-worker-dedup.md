# ADR-003 — Extraction queue, worker, and dedup

**Status:** Accepted
**Cited by:** `worker.py`, `dedup.py`, `extractor.py`, `cli_queue.py`,
`cli_dedup.py`, `llm.py`, `migrations/007`

## Context

LLM work (entity extraction, re-embedding, dedup) must not block the
synchronous store/search path, and optional LLM use must degrade
cleanly to zero ("no LLM required" floor).

## Decision (reconstructed from citing code)

- **Queue** (`extraction_queue`, migration 007): store/update enqueues
  `extract` / `reembed` / `dedup` tasks; the sync path stays
  write-only-fast. `memory_id` nullable so dedup tasks (whole-corpus)
  can be enqueued (migration 008 rebuild).
- **Worker** (single daemon thread):
  - **Own SQLite connection** (§Failure Modes) — never shares the
    server's cached connection.
  - **Startup recovery** (§Worker startup recovery): `running` rows
    older than 300s reset to `pending` (crash-stale reclaim).
  - **Optimistic claim** (`UPDATE … WHERE status='pending'`); lost
    race → skip.
  - **Retention** (with ADR-001): daily prune of `done`/`failed` rows
    older than 30 days, cadence in `worker_state.last_prune_at`.
  - **Shutdown bound**: `stop()` joins up to 45s; daemon thread as
    backstop. (P1-3 later added migrate-on-open + crash marking —
    see CHANGELOG.)
- **Dedup** (§Dedup, `dedup.py`):
  1. `SELECT entities WHERE canonical_id IS NULL AND mention_count >= 2`
  2. Group by type; pairwise cosine on name embeddings
  3. Pairs over the cosine pre-filter threshold (default 0.9,
     tunable) → optional LLM confirm ("Are these the same entity?")
  4. On yes: `UPDATE entities SET canonical_id = <other_id>`
  - `llm_confirm=False` (stub mode) = cost-estimation report with no
    LLM calls.

## Consequences

- Reads resolve `canonical_id` at query time (`COALESCE`), so dedup
  never rewrites `mentions` rows.
- A missing LLM key fails dedup tasks with a structured reason,
  not a crash.

## Gaps

Why 300s for stale-running and 0.9 for the cosine threshold is not
recorded beyond "tunable via spec".
