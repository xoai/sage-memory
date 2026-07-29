# Design: Cross-tool code-graph import

**Task:** P2-4 (SM-CAP-01 adjacent) · **Depends:** P2-1 ·
**Brief per:** `06-spec-phase2-capability.md §P2-4`

## Problem

Other tools in this space produce deterministic code graphs
(`graph.json` of nodes/edges with confidence labels) across more
languages than sage-memory's 10. sage-memory's differentiator is
persistent, curated, self-learning memory — not extraction breadth.
Importing an external graph gives users both. **Scope guard: import
only; we do not re-implement other tools' extraction.**

## Input artifact

Pure data interchange — **no dependency on the external package**
(the two ecosystems pin incompatible `tree-sitter` versions and
cannot share one Python environment). Input is a JSON file:

```json
{
  "tool": "external-tool-name",
  "version": "1.2.3",
  "nodes": [
    {"id": "n1", "name": "Foo", "qualified_name": "pkg.Foo",
     "kind": "FUNCTION", "language": "kotlin",
     "file": "src/pkg/Foo.kt", "line_start": 10, "line_end": 40}
  ],
  "edges": [
    {"source": "n1", "target": "n2", "kind": "calls",
     "confidence": "resolved", "line": 22}
  ]
}
```

Validation: top-level `nodes`/`edges` arrays required; each node
needs `id`, `name`, `file`; each edge needs `source`, `target`,
`kind`. Malformed entries are counted and skipped (never abort the
import), matching the scan loop's documented resilience contract.

## Confidence mapping (no silent upgrades)

The artifact's confidence vocabulary maps onto sage-memory's
`resolved` / `unresolved`:

| External value | sage-memory |
|---|---|
| `resolved`, `exact`, `verified`, `true` | `resolved` |
| everything else (`inferred`, `heuristic`, `unknown`, missing) | `unresolved` |

An external edge is **never silently upgraded**: anything that is
not an explicit fact label lands in `unresolved`, with the original
value preserved in a per-edge provenance note (see `source` column
below — the original label is recoverable from the artifact).

Resolution of edge endpoints: external node ids are mapped to the
`code_symbols` rows created by this import; an edge whose endpoint
can't map is written with `target_symbol_id = NULL` (never dropped
silently — same philosophy as the native resolver's "unresolved
stays visible").

## Provenance: `source` column (migration `013_relation_source.sql`)

Append-only:

```sql
ALTER TABLE code_relations ADD COLUMN source TEXT NOT NULL DEFAULT 'native';
```

- Native scans write nothing new — the DEFAULT keeps them `native`
  (no changes to the scan/resolve path).
- Imported rows get `source = 'import:<tool>'` (e.g.
  `import:codemap`). P2-1 queries can then distinguish/prove
  provenance; `hubs`/`affected` output gains no shape change (the
  column is queryable, not required reading).
- Index: none (cardinality is tiny; the existing relation indexes
  carry the queries).

## Idempotency: per-source replace

Re-importing from the same tool replaces **that source's** rows
only:

```sql
DELETE FROM code_relations WHERE source = 'import:<tool>';
```

before inserting the new set. Native rows and other tools' rows are
never touched. Symbols are keyed the same way as native rows
(file_memory_id + qualified_name) — a re-import of the same file
set reuses the upserted file memories; `INSERT OR IGNORE` on the
relation UNIQUE constraint keeps the operation retry-safe.

## File memory linkage

Imported nodes reference files that may not exist in
`codebase_scans` (different language, no native scan). The importer
upserts a file memory + `codebase_scans` row per distinct file
(content_hash = `import:<tool>:<file>` sentinel — distinguishes
from real content hashes and satisfies NOT NULL), so symbols have a
valid `file_memory_id` FK and P2-1 queries can render `file:line`.

## Surfaces

- CLI: `sage-memory code import <graph.json> [--tool <name>]`
  (`--tool` overrides the artifact's `tool` field for provenance).
  Prints `{imported, skipped_malformed, edges_resolved,
  edges_unresolved}`.
- No MCP tool: import is an operator action, not agent surface
  (matches P2-2's reasoning).

## Testing plan

Fixture artifact (small Kotlin graph sage-memory can't scan
natively): import → symbols + relations present with
`source='import:…'`; confidence mapping verified (explicit fact →
resolved, inferred → unresolved, never upgraded); re-import →
replaced, native rows (a small native Go scan) untouched; malformed
entries counted not fatal; `affected`/`hubs` see imported edges with
correct confidence labels; 013 upgrade test from a 012-era DB.
