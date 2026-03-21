# Changelog

All notable changes to sage-memory will be documented in this file.

## [0.5.0] — 2025-03-18

### Added

- **`sage_memory_set_project`** tool — set the active project for this session. Call first before other tools. Ensures stores and searches hit the correct database when the MCP server stays running across project switches. Priority chain: explicit `set_project` → `SAGE_PROJECT_ROOT` env var → cwd walk-up → global DB. Home directory safety check prevents `.sage-memory/` in `~`.
- **Graph support** via `edges` table (migration 002) with CASCADE deletes, JSON properties, and composite unique constraint `(source_id, target_id, relation)`.
- **`sage_memory_link`** tool — create, update, or delete typed directed edges between memories. Supports: `depends_on`, `has_task`, `assigned_to`, `blocks`, `part_of`, `contains`, `relates_to`, or any custom relation type. Self-loops rejected. Upsert on duplicate edges. Properties stored as JSON.
- **`sage_memory_graph`** tool — cycle-safe BFS traversal from a starting memory. Supports: outbound, inbound, or both directions. Depth limit 1-5. Optional relation type filter. Returns discovered nodes (full memory data) and edges (with properties).
- **Comprehensive test suite** — 59 tests across 9 suites: CRUD, dual-DB merge, filter_tags isolation, graph CRUD, graph traversal (with cycle detection), ontology patterns, self-learning patterns, set_project isolation, and performance benchmarks.
- **Evaluation framework** — 4 evaluations: self-learning retrieval (50 tasks), knowledge context coverage (30 questions), OR vs AND retrieval quality (29 queries), graph-enhanced recall (11 learnings, 4 entities).

### Fixed

- **FTS5 query sanitization** — punctuation (`?`, `!`, `.`, etc.) leaked into FTS5 queries causing silent search failures. Now strips all non-word characters (`[^\w\s]`). Discovered during evaluation testing.
- **Project root caching** — project root was cached once at first access and never re-evaluated. MCP servers staying running across project switches would silently use the wrong database. Now re-evaluates on every tool call (< 1ms cost).
- **Home directory safety** — `find_project_root` no longer returns `~` as a project root even if `~/.git` exists.

### Why

The ontology skill encodes entity relationships (Task blocks Task, Project has_task Task) as tagged memory entries. Multi-hop traversal requires N sequential MCP calls — one per hop. With `sage_memory_graph`, the same 2-hop query is a single call. Entity deletion required manual cleanup of relation entries; CASCADE now handles it automatically.

Design choice: Approach B (property edges with JSON metadata) over 5 alternatives evaluated in the ADR. Key tradeoffs: CASCADE integrity over triple-store flexibility, JSON properties over fixed columns, memory-to-memory only (external entities represented as memories by convention).

### Performance

| Operation | P50 | P95 |
|---|---|---|
| Create edge | 0.19ms | 0.35ms |
| Graph traversal (depth 1-3) | 0.17ms | 0.30ms |
| Store (unchanged) | 0.32ms | 0.75ms |
| Search (unchanged) | 0.88ms | 4.60ms |

All 59 tests pass. Total codebase: ~1,550 lines Python + 70 lines SQL.

## [0.4.0] — 2025-03-18

### Changed

- **Tool names renamed** from `memory_*` to `sage_memory_*`:
  - `memory_store` → `sage_memory_store`
  - `memory_search` → `sage_memory_search`
  - `memory_update` → `sage_memory_update`
  - `memory_delete` → `sage_memory_delete`
  - `memory_list` → `sage_memory_list`

### Why

MCP tool names exist in a flat global namespace within a client session. Claude Code has its own built-in "memory" concept, and tool names like `memory_store` collide with that — causing the agent to dispatch to its internal memory system instead of the MCP tools. The `sage_memory_` prefix makes tool dispatch unambiguous regardless of what other memory systems the client has.

This is a pre-publication breaking change. Skills referencing the old tool names need to update their instructions to use the new names.

## [0.3.0] — 2025-03-18

### Added

- `filter_tags` parameter on `memory_search` — hard WHERE filter with AND logic, applied *before* BM25 ranking. Only memories matching ALL specified filter tags are returned. Existing `tags` parameter unchanged (soft ranking boost).
- Over-fetch multiplier (3x) when `filter_tags` is active, to compensate for candidates removed by tag filtering and maintain result quality at requested limit.

### Why

The self-learning skill stores learnings tagged `["self-learning", ...]` alongside regular codebase knowledge. Without hard filtering, `memory_search(tags=["self-learning"])` boosted learnings in ranking but didn't exclude non-learning entries — noise leaked into results. `filter_tags` gives skills clean namespace isolation: `filter_tags: ["self-learning"]` returns only learnings, while `tags: ["auth"]` can still boost auth-related results within that filtered set.

Design boundary: `filter_tags` is the one hard-filter mechanism on search. Future filtering needs (metadata, date ranges, custom fields) should prove themselves via tag conventions before earning dedicated parameters.

## [0.2.0] — 2025-03-17

### Added

- `tags` parameter on `memory_list` tool with AND logic — all specified tags must match. Enables browsing memories by tag without a search query (e.g., `memory_list(tags=["self-improvement", "gotcha"])`).
- Tool description for `memory_list` updated to document tag filtering behavior.

### Why

The sage-self-improvement skill needs to browse learnings by type (`self-improvement`, `gotcha`, `verified`) without constructing a search query. Previously this required abusing `memory_search` with a dummy query. Now `memory_list` handles it cleanly.

Tags use AND logic, consistent with how `memory_search` treats its `tags` parameter. This means `tags: ["self-improvement", "auth"]` returns only memories tagged with both — not either.

## [0.1.0] — 2025-03-17

### Initial release

sage-memory is an MCP server that gives LLMs persistent, project-aware memory.

#### Architecture

- **Project-local databases**: each project gets `.sage-memory/memory.db` at the project root, auto-detected by walking up from the working directory looking for `.git`, `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, and other markers.
- **Global database**: cross-project knowledge stored at `~/.sage-memory/memory.db`.
- **Dual-DB search**: every query hits both project and global databases, results merged with project-priority ranking (+10% boost), deduplicated by content hash.
- **FTS5 with OR semantics**: BM25 ranks documents by term match density. Stopword removal, prefix matching, and term-frequency filtering (drops terms appearing in >20% of corpus) keep results precise at scale.
- **Quality-gated vector search**: sqlite-vec cosine similarity is only used when a neural embedder (quality ≥ 0.6) is installed. The local TF-IDF embedder (quality 0.45) auto-downgrades to keyword-only search, avoiding noisy vector results that degrade ranking.
- **Deferred embedding**: store writes content + FTS5 index synchronously (< 1ms), embedding happens separately. If embedding fails, the memory remains keyword-searchable.
- **Batched access tracking**: search result access counts are buffered and flushed in bulk, keeping write locks out of the read path.

#### Tools

- `memory_store` — persist knowledge with automatic dedup (SHA-256 content hash), auto-generated titles, and tag support. Scope: `project` (default) or `global`.
- `memory_search` — hybrid search across project + global DBs. Strategies: `hybrid` (default), `keyword`, `semantic`. Supports tag boosting, limit, and scope filtering.
- `memory_update` — partial update by ID with automatic re-indexing on content changes.
- `memory_delete` — delete by ID.
- `memory_list` — paginated browsing with scope filter.

#### Embedder

- `Embedder` protocol for pluggable backends.
- Built-in `LocalEmbedder`: zero-dependency, character n-gram TF-IDF hashing (384-dim). Captures morphological similarity without neural models.
- Optional `FastEmbedder`: neural embeddings via fastembed (`pip install sage-memory[neural]`). Enables hybrid FTS5 + vector search with Reciprocal Rank Fusion.

#### Performance (benchmarked on FastAPI + Pydantic + httpx + Rich, 340K lines)

- Store: 1.0ms mean, ~1,000 writes/sec, flat throughput from 1K to 50K memories.
- Search: 2.5ms mean at 1K, 46ms at 22K. Per-project databases keep each DB in FTS5's sweet spot.
- Recall: 91% on LLM-authored capture-knowledge content (30 queries), 80–83% on raw code chunks (50 queries).

#### Codebase

- 7 source files, ~1,100 lines of Python, 53 lines of SQL.
- 2 required dependencies: `mcp`, `sqlite-vec`.
- 1 optional dependency: `fastembed` (for neural embeddings).
