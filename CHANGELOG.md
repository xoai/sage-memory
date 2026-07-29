# Changelog

All notable changes to sage-memory will be documented in this file.

## [Unreleased]

### Performance

- Resolver memoization (P1-2, descoped by measurement): profiling the
  post-P1-1 cold scan showed raw tree-sitter parsing at 0.25s of a
  1.5s scan — a process/thread pool (the P1-2 spec) would save ≤0.5s
  while adding real concurrency risk, and a threads probe showed zero
  parse speedup (0.12s serial vs threaded on 200 files). Per the
  spec's own "choose the executor by measurement" rule, no pool was
  built; the ≥3× cold-scan goal was already met 19× by P1-1. Instead,
  the Go/Java sibling-directory lookup is memoized per resolve run
  (59K calls on the corpus) and per-file directory strings are
  precomputed — cold scan 1.5s → 1.3s, identical DB state.

- Incremental rescan (P1-1; SM-PERF-01, SM-PERF-03): the resolve pass
  no longer re-reads or re-parses unchanged files. Relations for
  changed files are reused from the scan pass; cross-file dependents
  are re-resolved from the DB; rows orphaned by the `ON DELETE
  CASCADE` on changed files' symbols are snapshotted and restored.
  Resolver helpers (`_siblings_in_same_directory`, TS/Rust rel_path
  lookups) use precomputed maps instead of per-relation O(files)
  scans — measured as the dominant cost on the 471-file Go corpus:
  cold scan 29.1s → 1.5s; no-change rescan 27.9s → 0.2s (doc baseline
  73.2s/84.4s on slower hardware). Escape hatch: `--full-resolve`
  restores the old disk re-parse path (`--force` implies it).
  Migration `010_unresolved_relations_index.sql` adds a partial index
  on unresolved `code_relations.target_name`.

### Security

- Transport security (P0-3; SM-SEC-01/02/03, SM-DOC-03):
  - **Bearer auth** for `sse`/`http` transports: `--token` flag or
    `SAGE_MEMORY_TOKEN` env; every request must carry
    `Authorization: Bearer <token>` (`hmac.compare_digest`), else 401.
  - **Refuse-start rule**: non-loopback binds (including `0.0.0.0`
    and blank/wildcard hosts) without a token now exit with a clear
    error. Loopback and stdio stay zero-config.
  - **Host allowlist + Origin validation** (403): loopback spellings
    plus `--allowed-host` (repeatable) / `SAGE_ALLOWED_HOSTS`
    (os.pathsep-separated). Defeats DNS rebinding and browser
    cross-origin drives.
  - **`set_project` scoping**: only the detected project root subtree
    (or launch directory when no markers exist) plus
    `SAGE_ALLOWED_ROOTS` is accepted; `~/.ssh`, `~/.gnupg`, `~/.aws`,
    `/etc` are always denied. Containment uses resolved-path
    `Path.is_relative_to` (sibling-prefix paths rejected).
  - New `SECURITY.md` (threat model, reporting).

### Changed

- **Docker deployments**: the default `0.0.0.0` bind now requires
  `-e SAGE_MEMORY_TOKEN=...` or the container refuses to start.
  Previously-open unauthenticated Docker/team servers must set a
  token (or bind loopback). See docs/guides/self-hosted-server.md
  §"Authentication (P0-3)".

### Added

- `LICENSE` file (MIT). `pyproject.toml` now uses PEP 639 metadata
  (`license = "MIT"`, `license-files = ["LICENSE"]`); built wheels
  carry `License-Expression: MIT` and ship the license text under
  `dist-info/licenses/`. (P0-2, SM-LEGAL-01)
- CI quality gate (P0-1, SM-PROC-01): `.github/workflows/ci.yml` runs
  on every push to main and every PR — test matrix Python 3.11–3.13
  on base deps (proves the zero-extra floor), all-extras job, ruff
  lint, and a wheel/sdist build check. `dependabot.yml` groups weekly
  minor/patch bumps for pip and github-actions.
- `docs/config.yaml.example` (P0-1b, SM-BUG-01): the shipped config
  example, moved out of gitignored `.sage/`. Covers every recognised
  config key with real defaults and inline docs.

### Fixed

- Worker startup crash-safety (P1-3, SM-REL-01): the background worker
  could die silently on a fresh or partially-migrated DB
  (`no such table: extraction_queue`, previously visible only as a
  pytest thread-exception warning). The worker's own connection now
  runs the same idempotent migrations as the server; the thread body
  has a top-level crash wrapper that logs loudly and records the
  reason in `worker_state.last_error` (migration
  `011_worker_crash_state.sql`), cleared on the next healthy start;
  `sage-memory worker --status` surfaces `⚠ worker crashed: <reason>`.

- Docker image size budgets re-based to CI-measured reality
  (slim ~210MB / full ~455MB uncompressed; previously aspirational
  60MB/350MB targets that predated the v0.13.1 FastMCP dependency
  tree and were never CI-enforced — the first CI run caught the
  drift). Multi-stage slimming tracked as a follow-up.
- `tests/test_documentation.py` could never pass on a clean clone —
  it asserted `.sage/config.yaml.example` exists while `.gitignore`
  ignores `.sage/` (shipped broken in v0.13.1). The test now points
  at `docs/config.yaml.example`, and a new reverse-drift test proves
  every key in the example is a recognised config key. Verified on a
  fresh clone.
- fastembed-dependent tests now `importorskip` when the `[neural]`
  extra is absent (SM-TEST-01) instead of failing on the base-deps
  floor.

## [0.13.1] — 2026-07-29

### Fixed

- `test_stdio_store_search_round_trip` was non-hermetic: the stdio
  subprocess inherited the developer's real `HOME`, so
  `sage_memory_search` (default scope = project + global) was polluted
  by real global memories that filled `limit=5` and outranked the
  freshly stored one. Passed in CI (empty HOME), failed on dev
  machines. The subprocess now gets an isolated `HOME` subdir.
- Fresh installs (`uvx sage-memory`, MCP client auto-start) crashed
  with `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`
  after the `mcp` SDK 2.0.0 release removed the bundled FastMCP 1.0.
  The MCP server now runs on the standalone **FastMCP 3.x** framework
  (`fastmcp>=3,<4`; `mcp` pinned to `>=1.9,<2` for `mcp.types`). The
  migration deletes the M1.1a private-API workarounds
  (`PassthroughFuncMetadata`, direct `_tool_manager._tools` insertion):
  FastMCP v3 publishes hand-crafted tool schemas verbatim and passes
  incoming arguments through to `(**kwargs)` handlers natively.

### Changed

- `tools/list` entries now carry `"_meta": {"fastmcp": {"tags": []}}`
  — emitted unconditionally by FastMCP v3, spec-compliant (`_meta` is
  open by design), and ignored by MCP clients. All other wire shapes
  (schemas, `_project` enrichment, `{"error": ...}` envelopes, hub
  params) are byte-equal to 0.13.0. Baseline regenerated accordingly.

## [0.13.0] — 2026-05-24

Team MCP transports. Adds **Pattern B** (shared MCP server over SSE
or streamable-HTTP) and **Pattern C** (hub federation across multiple
project DBs) from sage-wiki, plus subscription auth for the optional
entity-dedup worker, plus two Docker images for self-hosted deploys.

The MCP server migrates from `mcp.server.stdio` to FastMCP so all
three transports (stdio, sse, streamable-http) dispatch through a
single supported SDK API. Backwards compat is load-bearing:
arg-less `sage-memory` invocation continues to launch stdio with
byte-equal `tools/list` output AND envelope-shape preserved by a
shared dispatch wrapper.

### Added

- `sage-memory serve --transport {stdio,sse,http} [--port N --host
  HOST --hub --log-level LEVEL]` — explicit transport selection.
  Arg-less `sage-memory` still launches stdio for backwards-compat.
- `sage-memory hub` subcommand tree:
  `init / add / remove / list / status / search / store / import /
  release`. Config at `~/.sage-hub.yaml` with versioned schema +
  per-version migration hook registry.
- `sage_memory_search(hub_projects=[...])` MCP param (active under
  `--hub`) fans out search across registered projects + global.
- `sage_memory_store(hub_target=...)` MCP param (active under
  `--hub`) routes the write to a writable hub-registered project.
- PID-and-heartbeat ownership protocol (`<project>/.sage-memory/
  .hub-owner.json`) with atomic-rename stale-reclaim + asyncio.Task
  heartbeat + per-DB `_disabled_writes` registry. Three documented
  escape hatches: `rm` the file, `sage-memory hub release <name>`,
  `SAGE_HUB_IGNORE_OWNERSHIP=1` env var (dev only).
- Subscription auth via OAuth: `sage-memory auth` subcommand tree
  (`login / import / list / remove / status`). Stored at
  `~/.sage-memory/auth.json` with mode 0600. Opt-in via
  `~/.sage-memory/config.yaml` (`auth.worker_llm.<provider>:
  subscription`). Affects only `sage-memory dedup` worker.
- `Dockerfile.slim` (≤ 60MB; bring-your-own embeddings) +
  `Dockerfile.full` (≤ 350MB; bge-small-en-v1.5 pre-bundled).
- `/health` endpoint on SSE / HTTP transports.
- New guide: `docs/guides/self-hosted-server.md` covering Docker,
  reverse-proxy patterns, Pattern A → B migration, subscription
  auth, ownership escape hatches.

### Changed

- MCP server migrated from low-level `mcp.server.Server` API to
  `mcp.server.fastmcp.FastMCP`. Tools registered via a thin
  `PassthroughFuncMetadata` adapter that preserves the hand-crafted
  inputSchema verbatim — published `tools/list` is byte-equal to
  pre-migration 0.12.x. Verified via
  `tests/test_tools_list_baseline.py`.
- `search.rrf_fuse` extracted as a top-level helper so hub
  fan-out and per-project search share one RRF implementation.
- `docs/guides/team-setup.md` — Patterns B + C move from "planned"
  to "shipped" with full setup instructions.

### Notes

- **Docker port-publish vs internal `--host`:** `docker run -p
  3333:3333` publishes the container's port on `0.0.0.0` of the
  host regardless of `--host 127.0.0.1`. For non-localhost
  deployments, deploy behind a reverse-proxy auth layer.
- **Hub federation v1 limitations:** fan-out search uses FTS5 only
  (no vector / graph / LLM stages on the cross-project path).
- **`hub import` v1 requires explicit `--to <name>`:** auto-naming
  is deferred to a future cycle.
- **Subscription auth scope:** only affects users of the optional
  `sage-memory dedup` worker.

## [0.12.0] — 2026-05-24

Memory-level semantic dedup. SHA-256 dedup catches identical
content; this release adds a cosine-similarity check on the
embedding so paraphrased memories (cosine ≥ 0.95) get flagged at
write time and can be linked via the existing `supersedes` graph
edge. No new LLM dependency — uses the embedding infrastructure
shipped in earlier cycles.

The signal is **advisory**: `sage_memory_store` responses surface
near-duplicates in the existing `suggested_links` field with
`confidence: "near_duplicate"`; the agent decides whether to link,
merge, or store as distinct. `sage_memory_search` results carry
`superseded_by: <newer_id>` when an incoming `supersedes` edge
exists — older memories are NOT filtered or down-ranked.

**No schema migration.** All required infrastructure existed
already (`memories_vec` from 001, `edges` from 002, `supersedes`
accepted by the free-form `link()` relation field). The change is
additive at the response-shape layer.

### Added

- **Semantic dedup signal** on `sage_memory_store` and
  `sage_memory_update`: `suggested_links` entries gain
  `confidence: "near_duplicate"` + `similarity` when the new
  memory's embedding has cosine ≥ 0.95 to an existing memory in
  the same scope. Runs synchronously on write; p99 ≤ 50ms on a
  10k-memory project (measured ~4.6ms in T7 perf test).
- **`superseded_by` annotation** in `sage_memory_search` result
  envelopes when a result has an incoming `supersedes` edge.
  Surfaces the most recent supersession (`ORDER BY created_at
  DESC, rowid DESC` tie-break — sqlite's `rowid` is monotonic
  per-table, so the later INSERT always wins a same-tick tie;
  `edges.id` would not work because it's a random UUID hex).
  Cross-DB supersedes is NOT followed by design — `edges` is
  per-DB.
- **`sage-memory dedup --mode {entity,memory}`** flag. `entity`
  (default) preserves existing M5 entity-dedup behavior. `memory`
  is reserved for the forthcoming `--backfill` flag and prints a
  forward-only stub message in 0.12.0; it short-circuits BEFORE
  the LLM-key gate and the `--provider stub` validation.
- **`sage-self-learning` skill** extended with a new "When a
  memory is a paraphrase of an older one" subsection that
  contrasts `supersedes` (semantic paraphrase, both stay visible)
  with the existing `corrects` + `status: invalidated` pattern
  (factual wrongness, original hidden).
- **`semantic_dedup` module** (`sage_memory.semantic_dedup`):
  `find_near_duplicate(conn, *, embedding, exclude_id=None,
  threshold=0.95, k=5) -> dict | None`. k=5 (not 1) gives headroom
  for the just-stored self-row + up to 4 invalidated neighbors
  before the post-LIMIT exclude_id/status filter yields empty.
  Defensive normalize at the boundary handles zero / NaN / non-unit
  inputs.
- **`@pytest.mark.perf`** marker registered in `pyproject.toml`
  with `addopts = "-m 'not perf'"` so perf tests skip by default
  and run via `pytest -m perf`.

### Changed

- **`_try_embed` return type**: now returns `list[float] | None`
  (was `None`). Internal-only; no external callers existed.
  Callers reuse the embedding for the dedup check without
  re-embedding.
- **MCP tool descriptions** updated:
  - `sage_memory_store` mentions the new `confidence:
    "near_duplicate"` signal and the `supersedes` linking pattern.
  - `sage_memory_search` documents the optional `superseded_by`
    field.
  - `sage_memory_link` explicitly enumerates `supersedes` in the
    relation examples (previously implicit via the free-form
    string field).

### Unchanged

- Storage / FTS5 / vector index / RRF retrieval / graph traversal
  — semantically untouched. Test baseline preserved (873 → 918,
  delta +45 from this cycle's new tests across 7 new files plus
  helpers and Gate-3 review additions).
- `sage-memory dedup` default behavior — `--mode entity` is the
  default, existing scripts work unchanged.
- `[neural]` extra still optional. With no embedder, the
  semantic-dedup branch silently no-ops; `suggested_links` falls
  back to FTS-only suggestions (existing 0.9.0 behavior).
- No new dependencies. No new migration. No new MCP tool.

### Notes

- Threshold 0.95 is fixed for v1 (conservative; no env-var
  tuning). Calibration against field data informs any future
  adjustment.
- Backfill is forward-only in 0.12.0. The `--mode memory` CLI
  scaffolding lands so a future `sage-memory dedup --mode memory
  --backfill` fits naturally.
- `find_near_duplicate` is project-scope (or whichever DB the
  caller passes). Cross-scope writes (project vs global) do NOT
  compare against each other's vectors. Search separately
  surfaces both DBs at read time, but `_annotate_superseded` runs
  per-DB by `source` tag.

## [0.11.1] — 2026-05-24

Positioning clarification — three differentiators named
(coding-assistant memory, experience layer,
skills-as-intelligence) without narrowing the audience. sage
remains memory for ANY AI agent that needs persistent context;
the three wedges describe capabilities, not exclusive use cases.

**No behavior change.** All MCP tools, CLI subcommands, schema,
retrieval pipeline, embedder cascade, entity extraction — all
identical to 0.11.0. Wire format, exit codes, and skill install
paths preserved. This is a docs-and-descriptions patch.

### Changed

- **`pyproject.toml description`** rewritten to "Local MCP memory
  for AI agents — graph-native, learns from mistakes, no LLM
  required" (was: "Ultrafast local MCP memory for LLMs —
  project-aware, zero-config"). Audience phrasing kept broad
  ("AI agents") with three differentiators named. Visible on
  PyPI, GitHub repo card, and `pip show sage-memory`.
- **`README.md`** restructured:
  - New "Where sage fits" section frames the choice as an
    infrastructure trade-off (local-first / code-aware /
    learning-loops / graph-reasoning vs hosted-SaaS /
    conversation-extraction / cross-machine sync) rather than an
    audience match.
  - "Optional: Codebase Scan (0.11+)" section promoted from its
    previous position (after Retrieval Pipeline) to immediately
    after Setup — it's the latest big feature and the strongest
    proof of the "coding-assistant memory" differentiator.
  - Highlights reordered: three differentiator bullets
    (coding-assistant memory, experience layer,
    skills-as-intelligence) lead the bullet list.
- **5 MCP tool descriptions** (`sage_memory_store`,
  `sage_memory_search`, `sage_memory_link`, `sage_memory_graph`,
  `sage_memory_scan_codebase`) each gain a single "Best fit:" /
  "After running scan-codebase" sentence appended; existing text
  preserved. Examples broadened to cover use cases beyond coding.
- **3 skill `description:` front-matters** (`sage-memory`,
  `sage-ontology`, `sage-self-learning`) refreshed to lead with
  the broader "AI agents" framing in the first 1-2 sentences.
  Activation triggers preserved verbatim; install paths
  unchanged.

### Unchanged

- Wire format, exit codes, return envelopes — identical to 0.11.0.
- Skill `name:` fields — preserved per 0.10.0 collision-free
  rename contract.
- Schema (migration 009 still the head) — identical.
- Audience scope — sage continues to serve any AI agent that
  needs persistent memory; the three differentiators do NOT
  restrict who can use it.

### Upgrade notes (0.11.0 → 0.11.1)

```bash
pip install -U sage-memory    # picks up the new docs + descriptions
```

No re-install of skills required (body content unchanged). MCP
agents will see updated tool descriptions on next tools/list call.

## [0.11.0] — 2026-05-23

Tree-sitter-backed codebase scanning across **10 languages** —
Python, TypeScript, TSX, JavaScript, JSX, Go, Rust, Java, Ruby,
PHP, C, C++. Opt-in via the new `[codebase]` pip extra. Adds the
`sage-memory scan-codebase` CLI subcommand, the
`sage_memory_scan_codebase` MCP tool, and a new "Codebase Scan
(0.11+)" section in the `sage-ontology` skill. End result: an
agent can ask "where is X called?" or "what imports Y?" and get
millisecond answers from the pre-indexed graph instead of
re-reading source files.

### Added

- **`[codebase]` pip extra:** `pip install 'sage-memory[codebase]'`
  pulls in `tree-sitter-language-pack>=0.7.0,<1.0` (Goldziher /
  kreuzberg-dev; actively maintained, replaces the unmaintained
  `tree-sitter-languages`).
- **`sage-memory scan-codebase` CLI** with full flag surface —
  positional `path`, `--languages`, `--include-ignored`,
  `--limit` (default 5000), `--dry-run`, `--force`, `--help`. Exit
  codes follow spec: 0 (success / no-op), 1 (bad path / home-dir
  refuse / scan already in progress), 2 (extra not installed),
  3 (`--limit` exceeded), 4 (catastrophic parse failures).
- **`sage_memory_scan_codebase` MCP tool** always listed in TOOLS
  (predictable agent surface). Success envelope:
  `{success: true, files: {scanned, changed, unchanged,
  with_error_nodes}, symbols: {...}, relations: {imports,
  calls_resolved, calls_unresolved}, elapsed_ms, ...}`.
  Failure envelope: `{success: false, message: str}` aligned to
  the existing sage-memory MCP error convention.
- **Migration 009** introduces 4 new SQLite tables:
  `code_symbols`, `code_relations`, `codebase_scans`,
  `scan_locks`. Self-referential CASCADE on
  `code_symbols.parent_id` for the nested-fn / method-on-class
  hierarchy. UNIQUE discriminators on `line_start` (symbols) and
  `column_start` (relations) handle sibling-shadow nested
  functions and same-line duplicate calls.
- **Each scanned file becomes a memory entry** with title
  `[file:<lang>] <rel_path>`, content `Source file (<Language>)`,
  tags `["codebase", "file", "<lang>"]`. Path-salted SHA-256
  prevents UNIQUE collisions across the dozens of empty
  `__init__.py` files in a typical Python project.
- **Symbol kinds extracted:** `FUNCTION`, `CLASS`, `METHOD`,
  `INTERFACE`, `STRUCT`, `ENUM`. Methods get `parent_id` linked
  to their containing class/struct symbol.
- **Relations extracted:** `imports` (per-language target shape —
  Python `module.name`, TS `./mod.name`, Java/PHP qualified
  identifiers preserved), `calls` (with
  `confidence: resolved | unresolved` so agents can filter).
- **Per-language resolvers (9 total):** Python has the deepest
  resolution (cross-file imports + `self.X` method calls);
  TS/JS resolve `./module` relative imports; Go/Java use the
  same-directory-equals-same-package convention; Rust resolves
  `foo::bar()` via the sibling `foo.rs` / `foo/mod.rs`
  convention; Ruby/PHP/C/C++ resolve same-file only.
- **`sage_memory_search(filter_tags: ["codebase"])`** is the
  canonical discovery path after a scan. Hint added to the MCP
  tool's `filter_tags` description.
- **`sage-ontology` skill extended** with a new "## Codebase
  Scan (0.11+)" section explaining the install-detect gate +
  search workflow + agent fallback when the extra isn't
  installed.

### Safety guards

- **Project-scoped advisory lock** in `scan_locks` prevents
  concurrent scans on the same project — second invocation
  fast-fails with "scan already in progress" rather than
  blocking. Different projects can scan in parallel.
- **600s stale-row recovery** ensures a crashed prior scan
  doesn't permanently lock the project (the `finally`-block
  release is the primary mechanism; recovery is the backstop).
- **Home-directory refusal:** `Path(root).resolve() ==
  Path.home().resolve()` exits 1 — scanning `$HOME` is a
  uniformly bad idea.
- **Pre-walk `--limit` enforcement:** the file count is checked
  BEFORE any DB writes happen, so an over-limit scan leaves zero
  side effects.
- **Atomic per-file transaction** (`with conn:`): a parse failure
  on file N leaves files 1..N-1 cleanly indexed; on success the
  block commits memory upsert + symbol DELETE + symbol INSERT +
  `codebase_scans` upsert as one unit.

### Unchanged

- Storage / retrieval / search / graph machinery — untouched.
- 8 pre-existing MCP tools (`sage_memory_store` etc.) — unchanged.
- 3 pre-existing skills (`sage-memory`, `sage-ontology`,
  `sage-self-learning`) — `sage-ontology` got the new section but
  no other content changed.
- No breaking changes for users who don't install `[codebase]`.

### Upgrade notes (0.10.0 → 0.11.0)

```bash
pip install -U 'sage-memory[codebase]'   # add the new extra
sage-memory scan-codebase                # index the current project
sage-memory install-skills <agent> -y    # refresh the ontology skill text
```

The CLI subcommand and MCP tool are no-ops without the
`[codebase]` extra installed — they print/return the install hint
and exit cleanly.

## [0.10.0] — 2026-05-20

Skill identifier rename for collision-free install. Source folders
and the `name:` frontmatter of all three bundled skills now carry
the `sage-` prefix (`sage-memory`, `sage-ontology`,
`sage-self-learning`). Adapters install verbatim — install paths
are byte-identical to 0.9.0, so existing installations don't move.
Marker blocks in `AGENTS.md` / `GEMINI.md` style targets get
new names, with transparent legacy-block migration on re-install.

### Changed

- **Source folders renamed** under `src/sage_memory/skills/`:
  `memory/` → `sage-memory/`, `ontology/` → `sage-ontology/`,
  `self-learning/` → `sage-self-learning/`. `name:` frontmatter
  field inside each `SKILL.md` updated to match.
- **`sage-memory install-skills --skill` accepted values updated:**
  `sage-memory`, `sage-ontology`, `sage-self-learning`. Legacy bare
  names (`memory`, `ontology`, `self-learning`) are rejected with
  a migration hint pointing at the new prefixed name and CHANGELOG.
- **Adapter prefix dropped.** `agent_claude_code` and `agent_cursor`
  no longer prepend `sage-` at install time — the source folder name
  IS the install name. End result: installed paths are byte-identical
  to 0.9.0 outputs (`~/.claude/skills/sage-memory/`,
  `~/.cursor/rules/sage-memory.mdc`, etc.).

### Migration (transparent)

- **Marker-block migration is automatic on re-install.** Running
  `sage-memory install-skills <agent> -y` against an `AGENTS.md` /
  `GEMINI.md` file with legacy `<!-- sage-memory:skill:memory:begin
  -->` blocks detects each legacy block and replaces it with a
  new-named `<!-- sage-memory:skill:sage-memory:begin -->` block.
  No manual edit needed; no orphan blocks.
- **No install-path migration needed.** Existing 0.9.0 installs at
  `~/.claude/skills/sage-memory/`, `.cursor/rules/sage-memory.mdc`,
  `.codex/AGENTS.md`, etc. stay in place. On `-y`, SKILL.md content
  refreshes with the new `name:` field (single diff prompt without
  `-y`); no second copy lands at a different location.

### Unchanged

- Agent identifiers (`claude-code`, `codex`, `gemini`, `cursor`,
  `opencode`, `all`) — same.
- Scope flags (`--project`, `--global`) — same.
- Behavior flags (`--dry-run`, `-y`, `--skill`) — same shape; only
  `--skill` accepted-values list changed.
- MCP server tool names (`sage_memory_store` etc.) — unchanged.
- Schema, retrieval pipeline, embedder cascade, entity extraction —
  all unchanged. No retrieval code touched this cycle.

### Breaking

- **Hard-coded `--skill memory` / `--skill ontology` /
  `--skill self-learning` invocations** in user scripts or shell
  aliases need updates. One-line user fix per invocation. Error
  output points users at the new name.

### Aesthetic note

- Marker blocks follow the pattern `<!-- sage-memory:skill:<id>:begin -->`
  where `<id>` is the renamed skill identifier — e.g. `sage-memory`,
  `sage-ontology`, `sage-self-learning`. For the sage-memory skill
  specifically, this stutters as `sage-memory:skill:sage-memory:begin`
  (the constant prefix `sage-memory:skill:` is the product namespace,
  the trailing `sage-memory` is the skill id). Unambiguous, intentional
  — not a typo.

### Upgrade notes (0.9.0 → 0.10.0)

```bash
pip install -U sage-memory                            # 0.10.0
sage-memory install-skills <agent> --project -y       # transparent migration
```

The re-install detects + replaces any legacy-named blocks. If you
have a custom skill setup that referenced the bare folder names,
update those references manually.

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
