# Changelog

All notable changes to sage-memory will be documented in this file.

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
