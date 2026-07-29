# ADR-008 — Hub federation

**Status:** Accepted (rev 2 — `import.py` renamed `importer.py`;
Python keyword)
**Cited by:** `hub/` package, `cli_hub.py`, `server_fastmcp.py`,
`__init__.py`

## Context

Teams work across many project repos. Each project has its own
`.sage-memory` DB; agents need cross-project search and,
deliberately, cross-project writes without switching the server's
active project.

## Decision

### Registry (§"Decision")

`~/.sage-hub.yaml`: `version: 1` + `projects: [{name, path,
searchable, writable}]`. Missing version → assume 1 and rewrite on
next save. Version > `LATEST_KNOWN_VERSION` → **refuse to load**
(safer than warn-but-load). Schema evolution via
`MIGRATIONS[(from_v, to_v)]` callables in `hub/config.py`. Saves are
atomic (sibling temp file + `os.replace`; last writer wins —
acceptable: concurrent edits are rare, worst case is one re-run of
`hub add`).

§"Default flag semantics on hub add": `searchable=True` (mirrors
sage-wiki `hub.go:122-126`), `writable=False` (routed-write targets
are explicit opt-in).

### Federation semantics (§"Federation semantics")

- **Fan-out search** (`hub search`, `hub.search.fan_out_search`):
  opens each `searchable: true` project DB **read-only** + the
  global DB, runs the FTS5 primitive per DB (cap 50), merges via the
  shared `search.rrf_fuse`, results carry `source` (project name or
  `"global"`). v1 deliberately descoped to FTS-only cross-project
  (M2 review C2) — per-project `search.search()` still runs the full
  pipeline.
- **Routed write** (`hub store --to`, with ADR-009 §5): resolve
  project → validate `writable: true` → acquire/verify ownership →
  switch active project → standard `store.store()` → restore prior
  state → release ownership if freshly acquired.
- **Import** (`hub import`, with ADR-009 §6): source DB read-only
  (`file:…?mode=ro`), destination ownership acquired BEFORE the
  transaction, released after commit-or-rollback (try/finally);
  single-transaction import; dedup on `content_hash`; copies
  memories + memories_vec + edges; edges with unresolvable endpoints
  are dropped (referential integrity).

### MCP integration (§"MCP integration")

`sage-memory serve --hub` activates hub-aware tool behavior:
`sage_memory_search` gains `hub_projects`, `sage_memory_store` gains
`hub_target`. Without `--hub` the kwargs are silently dropped
(spec-compliant no-op).

### CLI

`sage-memory hub {init, add, remove, list, status, search, store,
import, release}`; hand-rolled flag parsing; `--config-path`
override. Import's default target name (source basename) deferred —
v1 requires explicit `--to`.

## Consequences

- Federation is a thin read-mostly layer over per-project DBs; the
  ownership protocol (ADR-009) carries all write safety.
- Hub config is user state, never project state; sync remains a user
  concern (local-first invariant).

## Gaps

Why per-DB FTS cap 50 (not limit-scaled) is not recorded.
