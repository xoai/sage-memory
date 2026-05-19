# Changelog

All notable changes to sage-memory will be documented in this file.

## [0.9.0] — 2026-05-19

Agent-driven extraction. The calling agent (Claude Code, Cursor,
Codex, Gemini, OpenCode) now provides entity/relation structure
inline as part of `sage_memory_store`, instead of sage-memory's
background worker re-deriving it via its own LLM call. Users without
an LLM API key configured for sage-memory get the full knowledge
graph because their agent IS the LLM.

### Added

- **`entities` and `relations` params on `sage_memory_store` and
  `sage_memory_update`.** Optional arrays of
  `{"name", "type", "surface_form"?}` and
  `{"from", "to", "rel"}`. Entity types: `PERSON, CONCEPT,
  TECHNOLOGY, PROJECT, EVENT, OTHER`. Relation types: `mentions,
  relates_to, contains, depends_on, contradicts, derived_from,
  implements, references, supersedes, alternative_to`. Defensive
  caps: 50 entities, 100 relations per call. Validation errors
  reject the whole call (no partial writes).
- **`suggested_links` response field** on store/update. Up to 3
  candidate link targets surfaced via direct FTS5 query against
  the project DB (`status='active'`). Agents can follow up with
  `sage_memory_link` to formalize the connection. Adds ≤ 5ms p95
  to store on a 1K-memory corpus.
- **`extraction_write.write_extraction()`** — shared helper used by
  both the agent-driven path and the background worker. Guarantees
  byte-equivalent row inserts regardless of which path ran.
- **`extractor.validate_agent_payload()`** — shape + vocab + size
  validation for agent-provided payloads, with explicit rename of
  the JSON wire fields (`from`/`to`/`rel`) to the worker-shape
  consumed by `write_extraction`.

### Changed

- **`sage_memory_search` defaults for `expand` and `rerank` flip from
  resolves-to-`llm.is_configured()` to explicit `false`.** Brings live
  behavior into parity with the 0.8.0 published bench numbers
  (free-path R@5 = 0.972; hosted R@5 = 0.986; both captured with the
  stages off). To re-enable: pass `expand=true` / `rerank=true`
  explicitly in your search call. Old clients that never passed these
  params will see the new default automatically.
- **Bundled skills updated** with an "Extract Before Store" section
  in `memory`, an "Agent-driven extraction" callout in `ontology`,
  and an entity-aware Prevention pattern in `self-learning`. Each
  shows the new `entities`/`relations` invocation shape.

### Deprecated

- **Background extraction worker path.** Still functional in 0.9.0 as
  a fallback (runs only when an LLM API key is configured AND the
  agent did not pass `entities`/`relations`). Will be **removed in
  1.0.0**. Migration: pass `entities` and `relations` from the agent
  side. The bundled skills demonstrate the pattern.
- Workers emit a one-time INFO log at startup describing the
  deprecation; visible via `cli_worker --status` will NOT trigger it
  (status inspection doesn't enter the run loop).

### Upgrade notes (0.8.0 → 0.9.0)

- **No breaking changes on the wire.** `sage_memory_store` and
  `sage_memory_update` remain additive — the new params are optional;
  callers that don't pass `entities` still get the worker path if a
  key is set.
- **Re-install bundled skills** to pick up the new `Extract Before
  Store` instructions for your agent:
  ```bash
  sage-memory install-skills <agent> --project   # or --global
  ```
  `pip install -U` does NOT refresh installed skills — they live in
  user-controlled agent config directories.
- **Search behavior changes for any caller that omitted `expand=`
  / `rerank=` kwargs.** Previously `None` resolved to "on with key,
  off without"; in 0.9.0 `None` is always off. Pass `=true`
  explicitly to opt in.
- **`suggested_links` field appears on every store/update response.**
  MCP clients tolerate unknown JSON fields per spec; old clients
  ignore it harmlessly.

## [0.8.0] — 2026-05-19

`sage-memory install-skills` ships — one-command installation of the
three bundled skills into AI coding agents. Replaces the manual
"copy from the repo" UX with a small CLI that supports five targets
out of the box.

### Added

- **`sage-memory install-skills <agent>... [--project | --global]`** —
  installs sage-memory's three skills (memory, ontology, self-learning)
  into the conventional config location of the target agent. Supported
  agents:
  - `claude-code` — `~/.claude/skills/` or `.claude/skills/`
  - `cursor`      — `.cursor/rules/sage-*.mdc`
  - `codex`       — `~/.codex/AGENTS.md` or `./AGENTS.md`
  - `gemini`      — `~/.gemini/GEMINI.md` or `./GEMINI.md`
  - `opencode`    — `~/.config/opencode/AGENTS.md` or `./AGENTS.md`
  - `all`         — install for every supported agent in one command
- **Flags:** `--project | --global` (one required; no default),
  `--skill <name>` (repeatable filter), `--dry-run`, `-y/--yes`
  (auto-overwrite, required for non-TTY use).
- **Marker-delimited blocks** for AGENTS.md / GEMINI.md style targets,
  so re-installs replace exactly the prior block without disturbing
  user content. Version metadata lives *inside* the block, excluded
  from byte-equality so version bumps with identical skill bodies are
  idempotent.
- **Conflict resolution:** unified-diff prompt per file (or per block)
  with `[o]verwrite / [k]eep / [s]kip`. `--yes` skips prompts; non-TTY
  stdin without `--yes` preserves local content via the prompt's
  EOFError → KEEP fallback.
- **Bundled resources footer** for AGENTS.md-style targets — rewrites
  relative `references/*` references in the skill body to absolute
  paths under the bundled wheel location, plus a footer listing every
  reference file's absolute path so tools that don't follow markdown
  links still have pointers.

### Changed

- **`skills/` moved from repo root to `src/sage_memory/skills/`** so
  the skill files ship inside the wheel (`pip install sage-memory`
  now bundles them). `importlib.resources.files("sage_memory") /
  "skills"` resolves to the bundled location at runtime.
- **Path-change callout for 0.7.x users:** if your tooling or docs
  reference `github.com/.../blob/main/skills/...` URLs, update them to
  `github.com/.../blob/main/src/sage_memory/skills/...`. The old paths
  return 404 from 0.8.0 onward.

### Upgrade notes (0.7.x → 0.8.0)

- Existing installations are unaffected at runtime — the change is
  purely about where the skill files live in the repo and wheel. No
  database migrations, no MCP API changes.
- To use the new CLI: `sage-memory install-skills <agent> --project`
  (or `--global`). Run with `--dry-run` first if you have existing
  skill files at the target paths.

## [0.7.0] — 2026-05-19

Bug fix + benchmark release. The embedder resolver shipped in 0.6.0
was defined but never invoked at server startup — `get_embedder()`
returned `LocalEmbedder` regardless of `OPENAI_API_KEY` /
`VOYAGE_API_KEY` / `COHERE_API_KEY`. 0.7.0 wires the resolver into
`server.run()` so hosted embedders are actually picked up. Also
publishes the LongMemEval-S benchmark numbers.

### Fixed

- **Embedder bootstrap** (`server.py`): server now reads
  `corpus_meta.vec_dim` at startup and calls `resolve(corpus_dim)` +
  `set_embedder()` before the worker loop starts. Logs the active
  embedder + dim + quality. `DimMismatchRefuseError` surfaces with a
  reindex hint instead of silent fallback to local.

### Added

- **`tests/test_embedder_bootstrap.py`** — 6 tests covering the
  resolver cascade (384d no-key, 384d + OpenAI key, 1536d + OpenAI
  key, 1536d no-key refuse, `set_embedder()` singleton wiring,
  `server.run()` bootstrap path).
- **Hosted-vector benchmark harness**
  (`evaluation/longmemeval/bench_hosted.py`): recreates
  `memories_vec` + `chunks_vec` at the hosted embedder's native dim,
  bypassing the 384d default. Auto-picks the embedder from env
  (OpenAI 1536d / Voyage 512d / Cohere 1024d).
- **`evaluation/longmemeval/REPORT.md`** — 500q LongMemEval-S
  benchmark report. Free-path R@5 = 0.972 ($0); hosted-vector
  (OpenAI 3-small) R@5 = 0.986 (+1.4pp, ~$0.50 per 500q).
  Per-question-type breakdown + comparison row vs gbrain.
- **`evaluation/longmemeval/REPRODUCER.md`** — step-by-step walkthrough
  for reproducing the benchmark numbers from a clean clone.

### Removed

- Internal ablation scripts (`run_4way_ablation.sh`,
  `run_curve_ablation.sh`, `run_tier_comparison.py`) — not part of the
  documented reproducer flow.
- Legacy v0.5.0 evaluation harness (`evaluation/PROTOCOL.md`,
  `evaluation/REPORT.md`, four `run_eval*.py` scripts, `seed/`) —
  superseded by the LongMemEval suite.

### Upgrade notes (0.6.0 → 0.7.0)

- If you were running 0.6.0 with `OPENAI_API_KEY` (or `VOYAGE_API_KEY`
  / `COHERE_API_KEY`) set expecting hosted embeddings, you were
  silently on `LocalEmbedder`. 0.7.0 will now pick up the hosted key
  on startup.
- If your corpus was written with `LocalEmbedder` (384d) and the
  resolver picks a non-matching tier (e.g. OpenAI 1536d), sage will
  refuse to start with a clear reindex hint. Run
  `sage-memory reindex --re-embed --embedder <name>` to migrate the
  vec tables before restarting.

## [0.6.0] — 2026-05-17

Retrieval upgrade — ontology-aware indexing + multi-stage search
pipeline. Backwards-compatible: existing env-var tunables continue
to work over the new yaml-based config cascade.

### Added

- **Chunked retrieval** (`chunker.py`) — splits long memories at
  paragraph/sentence boundaries; chunk hits fold back to their
  parent memory at search time via dedicated `chunks_fts` /
  `chunks_vec` virtual tables.
- **Knowledge graph channel** (`graph_channel.py`) — two-layer BFS
  (entity-mediated + memory-direct via edges) with three configurable
  rank curves (linear / harmonic / type-weighted). Joins the BM25 +
  vector channels as a third leg of RRF fusion.
- **Background extraction worker** (`worker.py` + `extractor.py`) —
  polls a persistent `extraction_queue` and runs inline LLM
  entity/relation extraction with controlled-vocab type validation.
  Provider cascade (Anthropic primary, OpenAI fallback) with retry
  and code-fence stripping. Free-path floor: no LLM key → no
  extraction; search degrades gracefully.
- **LLM query expansion + rerank** (`expand.py` + `rerank.py`) —
  optional query expansion produces `{lex, vec, hyde}` variants with
  a strong-signal short-circuit; optional rerank applies a position-
  blend curve over the top-K. New MCP params: `expand`, `rerank`,
  `channels`, `strategy` (three-state: None / True / False).
- **Embedder cascade** — local / fastembed / OpenAI / Voyage / Cohere
  tiering resolved against `corpus_meta.vec_dim`; explicit error when
  the configured tier doesn't match the corpus dim.
- **Config cascade** (`config.py`) — per-call > env > yaml > built-in.
  All existing env-var names continue to work (deprecation logged
  once per name at DEBUG, never WARNING).
- **`sage-memory reindex`** CLI — `--re-embed --embedder <name>`
  (full backup + swap), `--embeddings` (partial; stale-meta only),
  `--memory-id`, `--limit`, `backup-list`, `backup-drop`.
- **`sage-memory dedup`** CLI — default worker-async enqueue with
  at-most-one concurrency contract, `--sync` in-process with sqlite
  advisory lock, `--provider stub` for cost estimation.
- **`sage-memory queue prune`** CLI — manual prune that bypasses the
  24h auto-prune gate.
- **`timings`** field on every search result with per-stage
  perf_counter deltas.

### Schema

Migrations add chunks, entities / mentions / relations, embedding
metadata, the extraction queue, and the worker-state singleton.
`extraction_queue.memory_id` is now nullable to support dedup tasks.

### Upgrade notes (0.5.0 → 0.6.0)

- No breaking config changes. Existing env vars (`SAGE_RERANK_TOP_K`,
  `SAGE_EXPAND_TOP1_NORM`, etc.) continue to work and take precedence
  over `.sage/config.yaml`. The new yaml is optional.
- New CLI commands (`reindex`, `dedup`, `queue`) extend the existing
  dispatch. No change to existing `sage-memory` /
  `sage-memory status` / `sage-memory worker --status` invocations.
- Migrations land automatically on first server start. The
  `extraction_queue` rebuild preserves all existing rows.

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
