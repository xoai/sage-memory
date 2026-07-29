# Design: Structural code-graph queries (`path`, `affected`, `hubs`)

**Task:** P2-1 (SM-CAP-01) · **Depends:** P1-1 (merged) ·
**Brief per:** `06-spec-phase2-capability.md §P2-1`

## Problem

`scan-codebase` persists ~4.3K symbols and ~35K relations on a
mid-size repo, but nothing exposes structural queries.
`sage_memory_graph` traverses the *entity* graph (`entities`/
`relations`) — empty by default. This task exposes the **code**
graph (`code_symbols` + `code_relations`): exposure, not extraction.

## Queries

All deterministic SQL + in-memory traversal. No LLM, no embeddings,
no new dependencies.

### `path <A> <B>`

Shortest path between two symbols.

- **Resolution of A/B:** exact `qualified_name` match first, then
  bare `name`. Multiple candidates → return them (disambiguation
  list with `qualified_name`, `kind`, `file:line`) instead of
  guessing — the caller re-issues with a qualified name.
- **Traversal:** BFS over **resolved** edges
  (`target_symbol_id IS NOT NULL`) for the path itself, because a
  path through a name-match is invented provenance. Unresolved edges
  are excluded from path-finding but reported in a
  `note` when they would have shortened the route? — **No.** Keep v1
  honest and simple: unresolved edges are excluded from `path`
  entirely and the envelope states that (see Confidence).
- **Output:** hop list, each hop =
  `{source_qname, kind, confidence, target_qname, file, line}`.
  CLI renders `A() --calls[resolved]--> B() …`. `truncated: true` +
  `max_depth` field when the cap bites (default depth cap 16, node
  cap 10K visited).

### `affected <X>`

"What breaks if I change X" — reverse traversal (inbound edges) to
depth N (default 2, cap 8).

- Group results by relation kind (`calls`, `imports`), each entry
  `{source_qname, kind, file, line, confidence, depth}`.
- Both edge classes included, **labelled**: `confidence: resolved` =
  fact; `confidence: unresolved` = name-match (see below).
- `--resolved-only` filter drops unresolved rows.
- Bounded: visited-set cycle safety (same approach as
  `graph.py:134-179`), `truncated: true` when depth/result caps bite
  (result cap 500).

### `hubs`

Most-connected symbols — architectural hot spots.

- Degree = count of resolved+unresolved edges where the symbol is
  source or target, split into `out_degree` / `in_degree`
  (unresolved edges count toward the side the row places them;
  unresolved targets join on `target_name = symbols.name`).
- Output: top-K (default 20) with `{qualified_name, kind, file,
  in_degree, out_degree, degree}`.

## Confidence semantics (load-bearing)

Measured ratio on the corpus: ~13% of relations are `resolved`.
Rules:

1. `resolved` edges (`target_symbol_id IS NOT NULL`) are facts.
2. `unresolved` edges carry only `target_name` — they are
   **name-matches**, always rendered with a distinct label
   (`unresolved:name-match` in CLI text, `confidence` field in JSON).
3. `path` uses resolved edges only (a shortest path through a guess
   is a lie). `affected` and `hubs` include both, labelled, with
   `--resolved-only` available.
4. The aggregate `relations_calls_unresolved` count already reported
   by scans stays as-is.

## Indexes (migration `012_code_graph_indexes.sql`, append-only)

Existing: `idx_code_relations_src (source_symbol_id)`,
partial `idx_code_relations_tgt (target_symbol_id) WHERE NOT NULL`,
partial `idx_code_relations_target_name_unresolved (target_name)
WHERE NULL` (P1-1), `idx_code_relations_kind`. Add:

```sql
CREATE INDEX IF NOT EXISTS idx_code_symbols_name
    ON code_symbols(name);
CREATE INDEX IF NOT EXISTS idx_code_symbols_file
    ON code_symbols(file_memory_id);
```

`name` powers A/B/X resolution + `hubs`' unresolved-target join;
`file_memory_id` powers file-scoped renders. Traversal itself is
driven by the existing source/target indexes. Upgrade test per
contract. (011 is P1-3's; 012 is next free.)

## Surfaces

- **CLI**: `sage-memory code path <A> <B> [--max-depth N]
  [--resolved-only]`, `sage-memory code affected <X> [--depth N]
  [--resolved-only]`, `sage-memory code hubs [--limit K]
  [--resolved-only]`. New `cli_code.py`; JSON with `--json`,
  human text otherwise.
- **MCP (additive, invariant 4):** `sage_memory_code_path`,
  `sage_memory_code_affected`, `sage_memory_code_hubs`. Registered in
  `server.py:TOOLS`/`HANDLERS` after `sage_memory_scan_codebase`.
  Envelopes: `{success, ...data}` / `{error}` matching house style.
  **Wire-shape note:** tools/list grows 10 → 13 — the byte-equal
  baseline is regenerated intentionally and called out in the PR
  (spec-compliant additive change).
- Requires the `[codebase]` extra (same gate as
  `sage_memory_scan_codebase` — listed unconditionally, fails with
  the install-hint envelope when the extra is absent).

## Envelope shapes

```
path:     {success, found, hops[], truncated, max_depth,
           candidates_a?, candidates_b?, note?}
affected: {success, symbol, depth, by_kind: {kind: [entries]},
           truncated, resolved_only}
hubs:     {success, hubs[], resolved_only}
```

## Cycle safety & bounds

BFS with a visited set of symbol ids (mirrors `graph.py`'s
visited-nodes discipline). Caps: `path` depth 16 / 10K visited;
`affected` depth 8 / 500 results; `hubs` is a straight SQL aggregate
(no traversal). Every cap that bites sets `truncated: true` —
never imply the whole graph was searched.

## Latency targets

Measured on the 471-file Go corpus (~35K relations): `path` and
`affected` < 200 ms interactive; `hubs` < 500 ms (single aggregate
scan). Recorded in the PR.

## Testing plan

Fixture: small Python + Go projects scanned via `scan()` (real
extraction, no mocks). Cases: path found / not-found / disambiguated
/ unresolved-excluded; affected grouping, depth cap, truncation;
hubs ordering + degree split; `--resolved-only`; MCP additive
registration (10→13, existing tools byte-identical); CLI text
rendering; index migration upgrade test.
