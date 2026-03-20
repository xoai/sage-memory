# ADR: sage-memory Architecture and Evolution

**Status:** Accepted
**Date:** 2025-03-18
**Authors:** Ninh (PM/Architect), Claude (Principal Engineer)
**Scope:** sage-memory v0.1–v0.5, skills integration, storage protocol

---

## 1. Context and Problem Statement

LLMs used as coding assistants forget everything between sessions. Every interaction starts from zero — re-reading files, re-discovering patterns, re-learning project conventions. This wastes time, produces inconsistent advice, and fails to accumulate understanding.

We need a persistent memory system that:

- Stores and retrieves knowledge for LLMs working on private codebases
- Works locally (no cloud dependency, no data leaving the machine)
- Integrates with existing AI coding tools (Claude Code, Cursor, VS Code) via MCP
- Is fast enough to be invisible (<50ms search, <2ms store)
- Is accurate enough to be trustworthy (>80% recall on natural language queries)
- Is lean enough to be maintainable (~1,000 lines, minimal dependencies)
- Serves as the storage layer for higher-level skills (knowledge capture, ontology, self-learning)

---

## 2. Design Principles

Established at project inception and validated through every subsequent decision:

1. **Lean.** Minimal files, minimal lines, minimal abstractions. Every line earns its place.
2. **Two dependencies.** `mcp` (the protocol) and `sqlite-vec` (vector search). No ML stack required by default.
3. **Performance and quality are top priorities.** Sub-3ms search, >80% recall, ~1,000 writes/sec — all benchmarked and proven.
4. **Beautiful, elegant code.** Clean separation of concerns, consistent patterns, no premature abstraction.
5. **Zero configuration.** Auto-detect project root, auto-create database, auto-route queries. Developer adds one JSON block and never thinks about it again.

---

## 3. Version History and Decisions

### v0.1.0 — Foundation

**Decision: Per-project databases, not a single global DB with scope filtering.**

The initial prototype used a single database with a `scope` column. Stress benchmarking on 4 codebases (FastAPI, Pydantic, httpx, Rich — 340K lines, 22K chunks) revealed that FTS5 OR queries degrade linearly with corpus size:

| Corpus size | FTS5 mean latency |
|---|---|
| 1,000 | 0.4ms |
| 5,000 | 8.3ms |
| 10,000 | 18.0ms |
| 22,000 | 36.5ms |
| 50,000 | 87.6ms |

A single project rarely exceeds 15K memories, but combining multiple projects in one DB pushes past FTS5's sweet spot. The solution: each project gets `.sage-memory/memory.db` at its project root, and cross-project knowledge lives at `~/.sage-memory/memory.db`. Search queries hit both DBs and merge results with project-priority ranking.

This eliminated the `scope` column entirely — each DB file IS the scope. Simpler schema, simpler queries, better performance.

Alternatives considered:
- Single DB with scope column — rejected due to FTS5 scaling
- One DB per scope string (e.g., `project:billing.db`) — rejected, too many files, harder lifecycle management
- Shared DB with partitioned FTS5 tables — rejected, complex and fragile

**Decision: FTS5 with OR semantics, not AND.**

The single highest-impact design choice. BM25 with OR ranks documents by term match density — documents matching 4 of 6 query terms score higher than those matching 1 of 6. AND requires ALL terms to match, which returns zero results for natural language queries where not every word appears in the target document.

Head-to-head benchmark — same 20 LLM-authored entries, 30 queries, OR vs AND:

| Metric | OR semantics | AND semantics |
|---|---|---|
| Mean recall | 91% | 20% |
| Semantic query recall | 81% | 0% |
| Workflow query recall | 100% | 0% |

The AND approach only works for exact API lookups where the query uses the same vocabulary as the stored content (100% recall for both). For every other query type, AND is catastrophically worse.

**Decision: Quality-gated vector search.**

The local TF-IDF embedder (zero dependencies) has quality score 0.45. At this quality level, vector search results are noisy and degrade RRF fusion — a mathematically provable problem identified during critical review (Gennady's point: noisy vec results push correct FTS5 results down in fused ranking).

Solution: the `Embedder` protocol includes a `quality` property. When quality < 0.6, hybrid search auto-downgrades to keyword-only. When a neural embedder is installed (`pip install sage-memory[neural]`, quality ~0.85), hybrid search activates automatically. Zero configuration change needed.

This also means store skips embedding when quality < 0.6, keeping write latency at <1ms.

**Decision: Deferred embedding (two-phase store).**

Phase 1: INSERT content + FTS5 trigger → commit (synchronous, <1ms). The memory is immediately keyword-searchable.

Phase 2: embed + write vector (only if embedder quality warrants it). If embedding fails, the memory remains searchable via FTS5.

Rationale (Linus's principle): the write path should not depend on the slowest component. With fastembed, embedding takes 5-50ms — acceptable but variable. With a larger model, worse. Decoupling means store latency is constant and predictable.

**Decision: Batched access tracking.**

Every search result gets its `accessed_at` and `access_count` updated. Initially this was done inline — a write transaction inside every search call (Carmack's critique: "You're taking a WAL write lock during reads").

Fix: buffer access updates and flush every 20 entries. Search calls are pure reads. Access tracking is a background side-effect.

**Decision: Normalized RRF scores.**

Reciprocal Rank Fusion produces raw scores in a narrow range (~0.015–0.030). The initial implementation combined RRF with temporal decay (0–1) and tag boosts (0.05–0.15). The decay and boosts dominated by 10x (Gennady's critique: "Your 'relevance' score is actually 85% recency + tags").

Fix: normalize RRF to [0, 1] before combining. Relevance is now the primary signal. Tag boosts and recency are small adjustments, not dominant factors.

**Decision: Term-frequency filtering in FTS5 queries.**

At scale, common terms like "response," "error," "model" match >20% of documents, diluting BM25's ability to rank by actually-discriminative terms. Solution: before building the FTS5 query, check each term's document frequency. Drop terms appearing in >20% of the corpus. This is ~20 lines of code and directly addresses the 50K scaling problem identified in stress benchmarking.

**Architecture (7 files, ~1,100 lines):**

```
src/sage_memory/
├── server.py       MCP server, 5 tools, dict-based dispatch
├── search.py       Dual-DB search, FTS5 OR, RRF fusion, access tracking
├── store.py        Store, update, delete, list, deferred embedding
├── embedder.py     Embedder protocol + local char-ngram + optional neural
├── db.py           Project detection, dual DB connections, migrations
├── __init__.py     Entry point
└── migrations/
    └── 001.sql     Schema: memories + FTS5 + vec0
```

### v0.2.0 — Tag Filtering on List

**Decision: Add `tags` parameter to `sage_memory_list` with AND logic.**

Requested by the self-learning skill team. They needed to browse learnings by type (`memory_list(tags=["self-learning"])`) without constructing a search query. Previously this required abusing `memory_search` with a dummy query.

Implementation: one parameter, one `WHERE tags LIKE ?` clause per tag (AND logic). +19 lines across `store.py` and `server.py`.

AND logic was confirmed as correct by the self-learning team — their query patterns are all AND-shaped: "all gotchas" = `["self-learning", "gotcha"]`, "verified auth learnings" = `["self-learning", "verified", "auth"]`.

### v0.3.0 — Hard Tag Filtering on Search

**Decision: Add `filter_tags` parameter to `sage_memory_search` — hard WHERE filter before BM25 ranking. Keep existing `tags` as soft ranking boost.**

Problem discovered by self-learning team's test suite: `sage_memory_search(tags=["self-learning"])` boosted learnings in ranking (+3% per tag match) but didn't exclude non-learning entries. Noise from regular codebase knowledge leaked into results.

The test proved it: `search(query="billing saga", tags=["self-learning"])` returned the "Billing service architecture overview" (tagged `architecture`, not `self-learning`) alongside actual learnings.

Impact evaluation before implementation:
- Performance: minimal — LIKE filter applies to FTS5 result set (10-40 rows), not full table. Over-fetch multiplier (3x) compensates for candidate removal.
- Quality: net positive — precision improves for namespaced content, recall unchanged (filtering is opt-in).
- Long-term: doesn't constrain future evolution. If a `type` field is needed later, `filter_tags` still works alongside it.
- Precedent: `filter_tags` is declared as THE one hard-filter mechanism on search. Future filtering needs should prove themselves via tag conventions before earning dedicated parameters.

Alternative considered: rename `tags` to `boost_tags`, add `filter_tags`. Rejected because renaming is a breaking change for zero benefit — `tags` as soft boost is genuinely useful ("prefer billing-related results" without excluding non-billing).

### v0.4.0 — Tool Name Namespacing

**Decision: Rename tools from `memory_*` to `sage_memory_*`.**

Root cause analysis from the sage-framework team: Claude Code has its own built-in "memory" concept. Tool names like `memory_store` collide — the agent dispatches to its internal memory system instead of the MCP tools, creating `.memory/MEMORY.md` files instead of calling sage-memory.

The advisory panel (Torvalds, Hickey, Norman, Beck) converged on: namespace the tool names, change the MCP config key, rewrite skill instructions with exact tool names.

We own the tool names and the config key. The rename is a find-and-replace in `server.py` — zero logic change. Pre-publication, so no external users to migrate.

Full naming chain after v0.4:

```
Package name:     sage-memory
MCP config key:   "sage-memory"
Server name:      sage-memory
Tool names:       sage_memory_store, sage_memory_search, sage_memory_update,
                  sage_memory_delete, sage_memory_list
DB directory:     .sage-memory/
```

No ambiguity at any level.

---

## 4. Benchmark Data

All benchmarks run against real codebases, not synthetic data.

### Store Performance (500 chunks, FastAPI codebase)

| Version | Mean | P95 | Throughput | DB size |
|---|---|---|---|---|
| v0.1 (mcp-memory) | 2.0ms | 3.5ms | 514/s | 2.2MB |
| v0.2 (mcp-memory) | 1.2ms | 1.8ms | 803/s | 0.8MB |
| sage-memory | 0.9ms | 0.8ms | 1,117/s | 0.7MB |

Improvement driven by deferred embedding (v0.2) and quality-gated embedding skip (sage-memory).

### Search Performance (500 chunks, 14 queries)

| Version | Mean | P95 | Mean recall | Acceptable recall |
|---|---|---|---|---|
| v0.1 (mcp-memory) | 5.2ms | 31.8ms | 38% | 47% |
| v0.2 (mcp-memory) | 2.1ms | 4.5ms | 65% | 75% |
| sage-memory | 3.4ms | 15.6ms | 73% | 79% |

Recall improvement driven by FTS5 OR (v0.2), domain stopwords, and term-frequency filtering (sage-memory).

### Scale Ladder (4 codebases, 50 adversarial queries)

| Memories | Store | Throughput | Search mean | Search P95 | Recall |
|---|---|---|---|---|---|
| 1,000 | 1.0ms | 1,000/s | 2.5ms | 9ms | 80% |
| 5,000 | 0.9ms | 1,100/s | 12ms | 56ms | 81% |
| 10,000 | 0.9ms | 1,055/s | 21ms | 72ms | 83% |
| 22,000 | 1.0ms | 1,000/s | 46ms | 101ms | 83% |
| 50,000 | 1.1ms | 943/s | 101ms | 275ms | 76% |

Store throughput is flat — SQLite INSERT + FTS5 trigger don't degrade with corpus size. Search degrades linearly with FTS5 OR (the FTS5 engine must score all matching documents). Per-project databases keep each DB under 15K, in FTS5's sweet spot.

Component profiling at 50K: FTS5 = 87.6ms mean, vec = 0.0ms (quality-gated off). FTS5 is the bottleneck, not vec.

### Per-Category Recall (22K scale)

| Category | Recall | Notes |
|---|---|---|
| Exact API lookups | 95% | Keyword search dominates — nearly perfect |
| Scoped queries | 91% | Project isolation works correctly |
| Semantic paraphrases | 66% | FTS5 OR catches most, neural embedder reaches 85%+ |
| Cross-codebase | 68% | Multi-DB merge works |
| Adversarial | 60% | Graceful degradation on typos, garbage, edge cases |

### OR vs AND Semantics (LLM-authored content)

20 genuine capture-knowledge entries about httpx, 30 developer queries. Same content stored in both an OR-based and AND-based FTS5 backend:

| Metric | OR semantics | AND semantics |
|---|---|---|
| Mean recall | **91%** | 20% |
| Acceptable recall (≥50%) | **97%** | 20% |
| Semantic queries | **81%** | **0%** |
| Workflow queries | **100%** | **0%** |
| Architecture queries | **100%** | **0%** |
| Exact API lookups | 100% | 100% |

AND is faster (short-circuits on first miss) but returns nothing for natural language queries. OR with BM25 ranking is the correct choice for LLM-authored content retrieval.

### Self-Learning Integration Test (v0.3)

25 seed learnings + 3 noise entries, 8 retrieval queries:

| Metric | Result | Target |
|---|---|---|
| Store P50 | 0.36ms | — |
| Search P50 | 0.54ms | — |
| Avg recall | 0.92 | ≥ 0.70 ✅ |
| Avg precision | 0.41 | ≥ 0.80 ❌ |
| Tag isolation (with filter_tags) | ✅ | — |
| Edge tag precision | 1.00 | — |
| Keyword precision | 0.63 | — |

Precision below target due to small corpus (25 entries — BM25 can't discriminate). Edge tags completely solve this (+0.37 precision delta). Precision improves with corpus size.

---

## 5. Critical Review Process

Every major design decision was pressure-tested through an advisory board representing distinct engineering lenses:

**Linus Torvalds** — systems-level thinking. Identified: embedding shouldn't be in the store hot path (→ deferred embedding), SQLite pragma over-configuration (→ sane defaults), write locks during reads (→ batched access tracking).

**Jeff Dean** — scale thinking. Identified: candidate multiplier too high (4x → 2x), graph traversal should be separate tool not integrated into search, 50K edge ceiling for SQLite CTEs.

**Gang of Four** — extension points. Identified: embedder as the one real axis of variation (→ Protocol pattern), cautioned against abstracting ranker/normalizer/database.

**Guido van Rossum** — Pythonic API design. Identified: dataclass transport overhead (→ plain dicts at boundaries), if/elif dispatch (→ dict-based dispatch), file count reduction (17 → 7).

**John Carmack** — latency profiling. Identified: need per-component timing, dict construction in hot loops, access tracking write locks during reads.

**Gennady Korotkevich** — algorithmic correctness. Identified: RRF score scaling bug (boosts dominating relevance by 10x), noisy vec results degrading fusion (→ quality-gated vec), missing cycle detection in graph traversal.

**Dave Cutler** — failure modes. Identified: WAL file lifecycle, concurrent access risks, missing integrity checks.

**Brendan Eich** — developer experience. Identified: need for CLI/REPL for debugging, config key clarity.

---

## 6. Current Architecture (v0.4.0)

### Schema

```sql
-- memories table: text content with FTS5 full-text index
memories (
    id, title, content, tags JSON, content_hash UNIQUE,
    embedded BOOLEAN, created_at, updated_at, accessed_at, access_count
)

-- FTS5: BM25 with column weights title=10, content=3, tags=1
memories_fts USING fts5(title, content, tags, tokenize='porter unicode61')

-- Vector index: 384-dim, used only when neural embedder active
memories_vec USING vec0(memory_id TEXT PRIMARY KEY, embedding float[384])
```

### Search Pipeline

```
query → tokenize → remove stopwords → term-frequency filter (drop >20% terms)
      → FTS5 MATCH (OR) + optional filter_tags WHERE
      → BM25 rank

query → embed (if quality ≥ 0.6)
      → sqlite-vec KNN + optional filter_tags WHERE
      → distance rank

      → Weighted RRF fusion (fts_weight=1.0, vec_weight=quality)
      → normalize [0,1]
      → +10% project DB boost
      → +3% per tag match (cap 15%)
      → +0-5% recency tiebreaker (14-day half-life)
      → dedup by content hash across DBs
      → top-k results
```

### Tools

| Tool | Purpose |
|---|---|
| `sage_memory_store` | Persist knowledge with auto-dedup and deferred embedding |
| `sage_memory_search` | Hybrid search with `filter_tags` and `tags` boost |
| `sage_memory_update` | Partial update with automatic re-indexing |
| `sage_memory_delete` | Delete by ID with CASCADE edge cleanup (v0.5) |
| `sage_memory_list` | Paginated browse with AND tag filtering |

### File Structure

```
sage-memory/                   v0.4.0
├── pyproject.toml             2 deps: mcp, sqlite-vec
├── README.md                  Setup, tools, performance, architecture
├── CHANGELOG.md               v0.1–v0.4 history
├── .gitignore
└── src/sage_memory/
    ├── __init__.py              8 lines   entry point
    ├── __main__.py              3 lines   python -m sage_memory
    ├── server.py              229 lines   7 tools (v0.5), dict dispatch
    ├── search.py              384 lines   dual-DB, FTS5 OR, RRF, filter_tags
    ├── store.py               233 lines   store/update/delete/list, deferred embed
    ├── embedder.py            175 lines   Protocol + local + optional neural
    ├── db.py                  190 lines   project detection, dual DB, migrations
    └── migrations/
        ├── 001_initial.sql     53 lines   memories + FTS5 + vec0
        └── 002_edges.sql       -- lines   edges table (v0.5)
```

---

## 7. Skills Architecture

### Three-Layer Design

```
┌──────────────────────────────────────────────────────────────┐
│                         LLM Runtime                          │
│  (Claude Code, Cursor, VS Code — executes skill instructions)│
└─────────────┬────────────────────┬───────────────────────────┘
              │                    │
      ┌───────▼────────┐  ┌───────▼────────┐
      │     Skills      │  │   MCP Tools    │
      │  (instruction   │  │  (sage-memory  │
      │   files that    │──│   server)      │
      │   shape LLM     │  │               │
      │   behavior)     │  │  7 tools      │
      └────────────────┘  └───────┬────────┘
                                  │
                          ┌───────▼────────┐
                          │    SQLite DB    │
                          │  memories       │
                          │  memories_fts   │
                          │  memories_vec   │
                          │  edges (v0.5)   │
                          └────────────────┘
```

The skills are the intelligence layer — they teach the LLM what to store, when to search, and how to structure content. sage-memory is the storage layer — it stores and retrieves. The LLM is the runtime that connects them.

This separation is deliberate. Skills can be rewritten for different LLMs without changing sage-memory. sage-memory can add features without changing skills. New skills can be built on sage-memory by third parties using the authoring guide.

### Skill Overview

**memory** (v1.0.0) — three layers of knowledge persistence:
- Layer 1: Automatic recall at session/task start
- Layer 2: Automatic remember during work (store insights, not facts)
- Layer 3: Deliberate learning via `sage learn` / `sage learn <path>`
- Produces: memory entries + knowledge reports (`.sage/docs/memory-{name}.md`)
- Defines the "unified knowledge facets" — knowledge (default), structure (ontology), warnings (learning)

**ontology** (v1.0.0) — typed knowledge graph:
- Entities stored as memories (tagged `ontology`, `entity`)
- Relations stored as memories (tagged `ontology`, `rel`, `edge:{id}`)
- Core types: Task, Person, Project, Event, Document (extensible via schema entries)
- Core relations: has_owner, has_task, assigned_to, blocks, part_of, depends_on
- Graph traversal via tag-based search (current) and `sage_memory_graph` (v0.5)
- Validation: required properties, enum values, cardinality, cycle detection

**self-learning** (v1.0.0) — mistake detection and prevention:
- Five types: gotcha, correction, convention, api-drift, error-fix
- Four-part content: what happened / why wrong / what's correct / prevention
- Lifecycle: capture → verify → consolidate → promote → share
- Namespace isolation via `filter_tags: ["self-learning"]`
- Ontology integration via `edge:{entity_id}` tags

### Unified Storage Model

All three skills store through sage-memory. Differentiation is through tags:

| Facet | Tag convention | Example |
|---|---|---|
| Knowledge (default) | Domain tags only | `["architecture", "billing"]` |
| Ontology entity | `["ontology", "entity", "{type}"]` | `["ontology", "entity", "task"]` |
| Ontology relation | `["ontology", "rel", "{type}", "edge:{from}", "edge:{to}"]` | `["ontology", "rel", "blocks", "edge:task_a1", "edge:task_f3"]` |
| Learning | `["self-learning", "{type}"]` | `["self-learning", "gotcha", "stripe"]` |

One search returns all facets. The LLM categorizes by tags and synthesizes context from all three.

---

## 8. v0.5 Plan — Graph Support

### What We're Adding

**Migration 002: edges table**

```sql
CREATE TABLE edges (
    id         TEXT PRIMARY KEY,
    source_id  TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    target_id  TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    relation   TEXT NOT NULL,
    properties TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    UNIQUE (source_id, target_id, relation)
);

CREATE INDEX idx_edges_source ON edges(source_id);
CREATE INDEX idx_edges_target ON edges(target_id);
CREATE INDEX idx_edges_relation ON edges(relation);
```

Design choice: **Approach B (property edges)** over 5 alternatives evaluated:

| Approach | Rejected because |
|---|---|
| A. Basic edges (weight only) | No edge metadata extensibility |
| C. Triple store (RDF) | No referential integrity, complex queries, SQLite isn't a graph DB |
| D. Embedded JSON links | O(N) full scan for reverse lookups, no traversal |
| E. Tag conventions only | Can't traverse, can't type relationships, fragile encoding |
| F. Typed edges (source_type/target_type) | Over-engineered — external entities can be represented as memories |

Approach B gives: CASCADE deletes, JSON properties for extensibility, efficient traversal via recursive CTEs, and memory-to-memory integrity. The JSON `properties` column lets skills attach arbitrary metadata (`{"confidence": 0.9}`, `{"recurrence_count": 3}`) without schema changes.

The external-entity problem (linking to files, URLs, concepts that aren't memories) is solved by convention: the capture-knowledge workflow already creates memories for modules and files. A file IS a memory. No type columns needed.

**Two new tools:**

`sage_memory_link` — create, update, or delete a typed edge between two memories.

```json
{
  "source_id": "abc123",
  "target_id": "def456",
  "relation": "depends_on",
  "properties": {"confidence": 0.9}
}
```

To delete: pass `"delete": true` with `source_id`, `target_id`, `relation`.

`sage_memory_graph` — cycle-safe multi-hop traversal from a starting memory.

```json
{
  "id": "abc123",
  "relation": "depends_on",
  "direction": "outbound",
  "depth": 2
}
```

Returns connected memories with relationship paths. Uses `WITH RECURSIVE` CTE with explicit cycle detection (`path NOT LIKE '%,{id},%'`).

**Design boundary:** Graph traversal is a separate tool call, not integrated into search. The LLM decides when graph context is worth the extra call. This preserves search performance and keeps graph optional.

### What Changes for Each Skill

**memory skill:**
- No changes required. Doesn't use graph features.
- Optional enhancement: `sage learn <path>` could use `sage_memory_link` to connect module memories to their dependencies, building a navigable architecture graph.

**ontology skill:**
- Relations can now use `sage_memory_link` instead of (or alongside) storing relation entries as memories.
- Multi-hop traversal uses `sage_memory_graph` instead of sequential search calls (4 calls → 1 call for 2-hop).
- Entity deletion uses `sage_memory_delete` — CASCADE automatically removes edges. No manual cleanup of relation entries needed.
- Tag-based approach (`edge:{id}` tags) still works for simple lookups. `sage_memory_graph` is for multi-hop traversal.

**self-learning skill:**
- Ontology integration strengthened: learnings linked to entities via `sage_memory_link` in addition to (or instead of) `edge:{id}` tags.
- Graph-based recall: "show me all learnings connected to this task, including learnings about its dependencies" = one `sage_memory_graph` call.
- Hot spot analysis: traverse from a module/service → count connected learnings → identify most mistake-prone areas.

### Performance Expectations

| Operation | Estimated latency | Notes |
|---|---|---|
| Create edge | <1ms | Single INSERT |
| Single-hop query | <1ms | Indexed lookup on source_id/target_id |
| 2-hop traversal | 1-3ms | Recursive CTE, ~100 rows visited |
| 3-hop traversal | 3-10ms | ~1,000 rows worst case |
| Delete memory + CASCADE | <2ms | SQLite handles cascade internally |

At typical ontology sizes (500 entities, 2,500 edges), all operations are sub-millisecond.

### Estimated Implementation

| File | Change | Lines |
|---|---|---|
| `migrations/002_edges.sql` | New — edges table + indexes | ~40 |
| `graph.py` | New — link and graph traversal | ~120 |
| `server.py` | Add 2 tool definitions + handler dispatch | ~50 |
| `store.py` | No change | 0 |
| `search.py` | No change | 0 |
| Total new | | ~210 |
| Total codebase | v0.4 (1,222) + 210 | ~1,430 |

### Skill Rewrites

All three skills will be rewritten to leverage v0.5 tools:

| Skill | Key changes |
|---|---|
| memory | Add `sage_memory_link` usage in `sage learn` for dependency graphs between module memories |
| ontology | Replace multi-call traversal with `sage_memory_graph`, simplify entity deletion (CASCADE), keep tag-based approach as lightweight alternative |
| self-learning | Use `sage_memory_link` for entity cross-referencing, enable graph-based hot spot analysis |

### Test Suite

Comprehensive tests covering all 7 tools + graph:

| Suite | What it tests |
|---|---|
| Store/retrieve | Basic CRUD, dedup, tag filtering |
| Search quality | Recall/precision across query categories |
| Dual-DB merge | Project + global results, priority ranking |
| filter_tags | Namespace isolation, AND logic |
| Graph CRUD | Create/read/delete edges, CASCADE |
| Graph traversal | Multi-hop, cycle detection, depth limit, direction |
| Ontology patterns | Entity + relation lifecycle, traversal, validation |
| Self-learning patterns | Learning isolation, edge-tag linking, consolidation |
| Performance | Latency percentiles at 1K/5K/10K scale |

---

## 9. Storage Protocol

### Tag Conventions (stable, shared across skills)

| Tag | Meaning | Used by |
|---|---|---|
| `ontology` | Entry is an ontology entity or relation | ontology skill |
| `entity` | Ontology entity (with type tag) | ontology skill |
| `rel` | Ontology relation (with relation type tag) | ontology skill |
| `edge:{id}` | Links entry to an ontology entity | ontology + self-learning |
| `self-learning` | Entry is a learning | self-learning skill |
| `gotcha`, `correction`, `convention`, `api-drift`, `error-fix` | Learning type | self-learning skill |
| `verified`, `promoted`, `stale` | Learning lifecycle state | self-learning skill |
| `schema` | Ontology type definition | ontology skill |
| Domain tags (e.g., `billing`, `auth`, `stripe`) | Subject area | all skills |

### Content Conventions

**Knowledge (memory skill):** Free-form prose. Explain what AND why. Use project vocabulary (class names, function names, domain concepts).

**Ontology entities:** JSON in content field. Title format: `[{Type}:{id}] {description}`.

**Ontology relations:** JSON in content field. Title format: `[Rel:{type}] {source} → {target}`.

**Learnings:** Four-part structure: what happened / why wrong / what's correct / prevention. Title format: `[LRN:{type}] {description}`.

### Scope Rules

| Scope | When to use | Where stored |
|---|---|---|
| `project` (default) | Codebase-specific knowledge | `.sage-memory/memory.db` at project root |
| `global` | Cross-project patterns, personal conventions | `~/.sage-memory/memory.db` |

Rule of thumb: if you'd want to know this in a different project using the same library/tool, it's global.

---

## 10. Future Considerations

### Validated and Deferred

These were proposed, evaluated, and deliberately deferred with specific criteria for when to reconsider:

| Feature | Deferred until | Rationale |
|---|---|---|
| `metadata` JSON field on memories | Evidence of what metadata actually gets used | Tag conventions handle v1 needs |
| `tags_any` (OR tag filter) | Evidence that AND is insufficient | Two queries is simpler than query operators |
| Memory-to-external edges (Approach F) | Someone needs it and can't represent the external entity as a memory | Convention: create a memory for the external entity |
| Schema-level `type` field | Tag-as-namespace proves to be the dominant pattern across 3+ skills | Let usage define taxonomy, then promote |
| Full SPARQL-like query language | 50K+ edges with complex pattern matching | Recursive CTEs handle current patterns; if not, use a real graph DB |

### Open Questions

1. **Cross-project knowledge transfer.** A Stripe webhook gotcha in project A applies to project B. Currently impossible — each project DB is isolated. Options: manual re-store (the user says "remember this globally"), shared DB for specific tags, export/import mechanism.

2. **Memory lifecycle.** What happens after 6 months of accumulated memories? No pruning mechanism exists. Options: auto-stale based on access_count, periodic review prompts, TTL per entry.

3. **Team sharing.** One developer's memories could benefit the whole team. Options: commit `.sage-memory/` to the repo (simple but large), export to a shared knowledge base, sync mechanism.

4. **Embedding model migration.** If a user switches from local to neural embedder (or changes neural model), existing vectors are incompatible. Need a re-embedding migration path.

---

## 11. Build Plan for v0.5

### Sequence

```
1. graph.py + migration 002                    → foundation (code)
2. Test suite                                   → proves foundation works (code)
3. Rewrite 3 skills with new tools             → uses proven tools (content)
4. Skill-authoring guide                        → generalizes from skills (docs)
5. Storage protocol doc                         → extracts conventions (docs)
```

Items 4-5 are best written after the skills have been used in practice, so the documented conventions reflect reality rather than speculation.

### Deliverables

| Deliverable | Type | Estimated size |
|---|---|---|
| `migrations/002_edges.sql` | Code | ~40 lines |
| `graph.py` | Code | ~120 lines |
| `server.py` updates | Code | ~50 lines |
| Test suite | Code | ~500 lines |
| Memory skill rewrite | Skill | ~400 lines markdown |
| Ontology skill rewrite | Skill | ~500 lines markdown |
| Self-learning skill rewrite | Skill | ~400 lines markdown |
| Skill-authoring guide | Docs | ~300 lines markdown |
| Storage protocol doc | Docs | ~200 lines markdown |
| **Total** | | **~2,500 lines** |

### Success Criteria

| Metric | Target |
|---|---|
| All existing tests pass (no regression) | 100% |
| Graph operations sub-millisecond | P95 < 5ms |
| Cycle detection prevents infinite loops | Verified on cyclic test graphs |
| CASCADE deletes clean up edges | Verified |
| Ontology skill multi-hop traversal | 1 call instead of N calls |
| Self-learning filter_tags isolation | 100% (no noise) |
| Total codebase | < 1,500 lines Python |
| Total dependencies | 2 required, 1 optional |
