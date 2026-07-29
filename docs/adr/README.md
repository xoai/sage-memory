# ADR Index

Architecture Decision Records for sage-memory, reconstructed from the
code comments that cite them (P1-6). Every `ADR-NNN` reference in
`src/` has a file here. Where the code does not record the rationale,
the gap is marked explicitly rather than invented.

| ADR | Title | Status | Key modules |
|-----|-------|--------|-------------|
| [ADR-001](ADR-001-memory-model.md) | Memory model: chunks, entities, embedding metadata | Accepted | `migrations/004–006`, `worker.py` |
| [ADR-002](ADR-002-chunking.md) | Structural-first chunking | Accepted | `chunker.py`, `store.py` |
| [ADR-003](ADR-003-extraction-worker-dedup.md) | Extraction queue, worker, dedup | Accepted | `worker.py`, `dedup.py`, `extractor.py`, `cli_queue.py` |
| [ADR-004](ADR-004-retrieval-pipeline.md) | Retrieval pipeline: channels, RRF, rerank, config cascade | Accepted (+ 2026-05-17 amendment) | `search.py`, `graph_channel.py`, `expand.py`, `config.py` |
| [ADR-005](ADR-005-embedder-cascade.md) | Embedder cascade + corpus dim lock | Accepted | `embedder.py`, `migrations/006` |
| [ADR-007](ADR-007-mcp-transports.md) | MCP server transports (FastMCP) | Accepted (rev 2) | `server_fastmcp.py`, `cli_serve.py` |
| [ADR-008](ADR-008-hub-federation.md) | Hub federation | Accepted (rev 2) | `hub/`, `cli_hub.py` |
| [ADR-009](ADR-009-hub-ownership.md) | Hub ownership: PID + heartbeat writer discipline | Accepted (rev 3) | `hub/ownership.py`, `graph.py`, `db.py` |
| [ADR-010](ADR-010-subscription-auth.md) | Subscription auth (OAuth) for the worker LLM | Accepted | `auth/`, `llm.py`, `cli_auth.py` |

Note: ADR-006 does not exist — no citation for it appears anywhere in
the tree; the numbering skips it.

## Milestone glossary

Code comments cite milestones (`M1`–`M5`) and review findings
(`T11 Major #1 fix`, `M2 review C2`, `rev 3 C1`). Decode:

- **M1** — memory model + embedder cascade + chunk storage
  (0.9.x–0.10.x era; migrations 001–006).
- **M2** — chunk-aware search + hub federation read path (0.13.0 cycle,
  "team MCP transports": hub `init/add/remove/list/status/search`).
- **M3** — hub write path: ownership, routed store, import, release
  (0.13.0 cycle). **M3a/M3b** — earlier retrieval sub-milestones
  (graph channel, RRF weighting, embedder quality gating).
- **M4** — query expansion + rerank (retrieval pipeline stages 1/5);
  also M4.x of the 0.13.0 cycle: subscription auth + Docker images.
- **M5** — config cascade, dedup worker, extraction_queue retention,
  rerank min-coverage gate.
- **T-numbers** (`T1`…`T11`) — task numbers within a milestone;
  `T11 Major #N` / `Mx review CN` are review findings fixed in-place,
  cited so future readers can trace why the code looks the way it does.
- **rev N** — revision of the spec/ADR the comment refers to
  (e.g. ADR-009 rev 2 = per-DB ownership semantics).

## Ground rules for ADR edits

These files are **reconstructions**. When you change a decision, edit
the ADR *and* the citing comments together. New decisions get the next
number (ADR-011), never reuse ADR-006.
