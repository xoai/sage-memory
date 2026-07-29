# ADR-001 — Memory model: chunks, entities, embedding metadata

**Status:** Accepted (M1 era, migrations 001–006)
**Cited by:** `migrations/004_chunks.sql`, `005_entities.sql`,
`006_embedding_meta.sql`, `008_worker_state.sql`, `worker.py`

## Context

sage-memory stores three memory kinds in one SQLite file per project
(knowledge, structure, experience). Long memories defeat single-vector
retrieval, and agents need a structural graph layer, not just prose.

## Decision (reconstructed from citing code)

- **Chunks** (migration 003/004): long memories are split into chunk
  rows, each with its own FTS5 + vec0 index; `chunks.memory_id`
  references `memories(id) ON DELETE CASCADE`. Chunk *writes* happen
  in M2 (`chunker.py`, see ADR-002); this ADR creates the tables
  and triggers.
- **Entities / mentions / relations** (migration 005): a heterogeneous
  graph layer — extracted entities, their mentions in memories, and
  typed relations between entities. Feeds the graph retrieval channel
  (ADR-004) and the dedup worker (ADR-003).
- **Embedding metadata + corpus dim lock** (migration 006, with
  ADR-005): per-vector provenance `(model_name, model_version, dim,
  created_at)` split into `memory_embedding_meta` +
  `chunk_embedding_meta` with real FKs (no polymorphic-id
  antipattern); `corpus_meta.vec_dim` locks the corpus dim to the
  first writer's choice (default 384).
- **Queue retention** (with ADR-003): `extraction_queue` rows in
  `done`/`failed` state are pruned after 30 days; cadence tracked in
  the `worker_state` singleton.

## Consequences

- The empty-entity-table fast path (ADR-004 invariant) exists because
  entities are opt-in/populated later, not at scan time.
- Staleness is computed from meta mismatch, enabling re-embed
  targeting without re-reading content.

## Gaps

The M1-era design discussions behind the three-kind model are not
recorded in code beyond what is above; to be confirmed by maintainer.
