# sage-memory

A local memory system for AI agents. Project-aware, zero-config, sub-3ms search, graph-traversable.

sage-memory is an [MCP](https://modelcontextprotocol.io) server that gives AI coding assistants persistent, structured memory. It stores what the AI learns about your codebase and retrieves it in future sessions — architecture decisions, entity relationships, past mistakes, conventions. Three kinds of memory, one unified system.

- **91% recall** on natural language queries (BM25 with OR semantics)
- **Sub-3ms search**, sub-0.3ms graph traversal, ~1,000 writes/sec
- **Project-isolated** — each codebase gets its own `.sage-memory/` database
- **Graph-native** — typed, directed edges between memories with cycle-safe multi-hop traversal
- **Dual-scope** — project knowledge + global preferences, merged and ranked automatically
- **Zero configuration** — auto-detects project root, creates database, routes queries
- **2 dependencies, ~1,500 lines** — lean, auditable, no ML stack required
- **Works with** Claude Code, Cursor, Windsurf, VS Code, and any MCP-compatible client

## Philosophy

AI agents today have no continuity. Each session starts blank — the assistant re-reads files, re-discovers patterns, re-learns conventions. Accumulated understanding is lost. sage-memory fixes this by giving agents three kinds of persistent memory that mirror how humans build expertise:

**Knowledge** — what you understand. Architecture decisions, conventions, domain logic. "The billing service uses a saga pattern because the team needed atomic multi-service operations with audit trails."

**Structure** — how things connect. Entity relationships, dependency graphs, ownership. "PaymentService depends on StripeGateway and LedgerService. Task A blocks Task B. Alice owns the billing module."

**Warnings** — what went wrong. Mistakes, gotchas, corrections. "Stripe webhooks require the raw request body — Express body parser breaks signature verification with a misleading 400 error."

These aren't three separate systems. They're three facets of one knowledge base, stored in one database, searchable in one query. When an agent starts working on the billing module, a single search returns: how billing works (knowledge), what it connects to (structure), and what went wrong last time (warnings). This is how human experts think about a system — simultaneously holding what, how, and watch-out-for.

The memory lives with your project, not in a cloud service. Each project gets its own SQLite database. Your private codebase knowledge never leaves your machine.

## Setup

Add sage-memory to your MCP client config. With [uv](https://docs.astral.sh/uv/), it installs and runs automatically — no manual `pip install` needed.

> **Don't have uv?** It's a fast, modern Python package manager. Install it in one command:
>
> macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
>
> Windows: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
>
> See the [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/) for more options.

### Claude Code

In `~/.claude.json` (or your project's `.claude.json`):

```json
{
  "mcpServers": {
    "sage-memory": {
      "command": "uvx",
      "args": ["sage-memory"]
    }
  }
}
```

### Cursor

In `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "sage-memory": {
      "command": "uvx",
      "args": ["sage-memory"]
    }
  }
}
```

That's it. No paths, no tokens, no database URLs. sage-memory detects your project root automatically.

<details>
<summary><b>Alternative: install with pip</b></summary>

If you prefer managing the installation yourself:

```bash
pip install sage-memory
```

Then use `"command": "sage-memory"` in your MCP config instead of `uvx`:

```json
{
  "mcpServers": {
    "sage-memory": {
      "command": "sage-memory"
    }
  }
}
```

For neural embeddings (higher recall on semantic queries):

```bash
pip install sage-memory[neural]
```

</details>

## How It Works

### Two databases, automatic routing

```
~/code/billing-service/
  .sage-memory/memory.db    ← this project's knowledge (auto-created)
  src/
  tests/

~/.sage-memory/memory.db    ← your cross-project patterns & preferences
```

**Project DB** stores knowledge specific to this codebase. **Global DB** stores cross-project patterns. Every search hits both; project results rank higher. `scope: "project"` (default) writes to project, `scope: "global"` writes to global.

### Search pipeline

```
query → tokenize → remove stopwords → term-frequency filter (drop >20% terms)
      → FTS5 BM25 (OR semantics) + optional filter_tags WHERE
      → optional: sqlite-vec cosine similarity (when neural embedder installed)
      → Reciprocal Rank Fusion → normalize [0,1]
      → project priority boost → tag boost → recency tiebreaker
      → deduplicate across DBs → top-k results
```

The key design choice: **FTS5 with OR semantics, not AND.** When you search "how does payment failure handling work," BM25 ranks documents by how many query terms match and how rare those terms are. AND semantics require ALL terms to match — which returns zero results for most natural language queries.

### Graph traversal

```
sage_memory_graph(id, relation, direction, depth)
      → WITH RECURSIVE CTE on edges table
      → cycle detection via visited set
      → depth-limited BFS
      → returns connected memories + edge paths
```

Graph traversal is a separate tool call, not integrated into search. The agent decides when structural context is worth the extra call. This keeps search fast and graph optional.

## Tools

### sage_memory_store

Store knowledge for later retrieval.

```json
{
  "content": "The billing service uses a saga pattern for multi-step payment processing. PaymentOrchestrator coordinates between StripeGateway, LedgerService, and NotificationService.",
  "title": "Payment saga orchestration via PaymentOrchestrator",
  "tags": ["billing", "saga", "payments", "architecture"],
  "scope": "project"
}
```

Content is SHA-256 hashed for automatic deduplication.

### sage_memory_search

Search across project and global knowledge. Supports `filter_tags` (hard AND filter for namespace isolation) and `tags` (soft ranking boost).

```json
{
  "query": "how does payment failure handling work",
  "filter_tags": ["self-learning"],
  "limit": 5
}
```

### sage_memory_update

Partial update by ID. Only provide fields you want to change.

### sage_memory_delete

Delete by ID. CASCADE automatically removes all graph edges connected to this memory.

### sage_memory_list

Browse stored memories with AND tag filtering.

```json
{
  "tags": ["self-learning", "gotcha"],
  "limit": 20
}
```

### sage_memory_link

Create or delete typed, directed edges between memories.

```json
{
  "source_id": "abc123",
  "target_id": "def456",
  "relation": "depends_on",
  "properties": {"confidence": 0.9}
}
```

Supports any relation type: `depends_on`, `blocks`, `has_task`, `assigned_to`, `contains`, `applies_to`, `relates_to`, or custom. Self-loops rejected. Upsert on duplicate edges. Properties stored as JSON.

### sage_memory_graph

Cycle-safe multi-hop traversal from a starting memory.

```json
{
  "id": "abc123",
  "relation": "depends_on",
  "direction": "outbound",
  "depth": 2
}
```

Returns connected memories and edges within N hops. Direction: `outbound` (source→target), `inbound` (target→source), or `both`. Depth limit 1-5.

## Skills

sage-memory ships with three first-party skills that teach AI agents how to use the tools effectively. Each skill works with MCP (full capability) or filesystem fallback (reduced but functional).

### memory

Teaches the agent when to remember and when to recall. Three layers:

1. **Automatic recall** — at session/task start, search for relevant context before reading files
2. **Automatic remember** — during work, store insights at natural completion points (3-8 per task)
3. **Deliberate learning** (`sage learn`) — structured knowledge capture that produces memory entries + a knowledge report

The memory skill defines the unified knowledge facets model: knowledge (default), structure (tagged `ontology`), and warnings (tagged `self-learning`). One search returns all three.

### ontology

Typed knowledge graph. Entities (Task, Person, Project, Event, Document) stored as memories with JSON content. Relationships stored as graph edges via `sage_memory_link`. Supports: validation rules, cardinality constraints, cycle detection for blocking/dependency relations, schema extensions for custom types.

What `sage_memory_graph` enables: "show me everything the payment service depends on, 2 hops deep" — one call instead of N sequential searches.

### self-learning

Captures mistakes so they're not repeated. Five types: gotcha, correction, convention, api-drift, error-fix. Every learning includes a four-part structure (what happened, why wrong, what's correct, prevention rule) and is isolated from regular knowledge via `filter_tags: ["self-learning"]`.

Learnings link to ontology entities via `sage_memory_link(relation: "applies_to")`, enabling graph-based targeted recall: "show me all past mistakes connected to this task."

## Capture Knowledge Workflow

The most effective way to use sage-memory:

1. **Explore** — ask your AI assistant to analyze a module or subsystem
2. **Understand** — it reads code, traces dependencies, identifies patterns
3. **Store** — it persists understanding via `sage_memory_store` with descriptive titles and domain tags
4. **Link** — it connects related memories via `sage_memory_link` to build an architecture graph
5. **Document** — it creates a `docs/ai/knowledge-{name}.md` companion file
6. **Retrieve** — in future sessions, it searches memory first for relevant context

Example prompt:

> Analyze the authentication system in this project. Understand how it works, what patterns it uses, and store your understanding in memory for future sessions.

## Performance

Benchmarked against 4 real Python codebases (FastAPI, Pydantic, httpx, Rich — 340K lines total).

### Scale

| Memories | Store | Throughput | Search mean | Search P95 | Recall |
|----------|-------|------------|-------------|------------|--------|
| 1,000    | 1.0ms | 1,000/s    | 2.5ms       | 9ms        | 80%    |
| 5,000    | 0.9ms | 1,100/s    | 12ms        | 56ms       | 81%    |
| 10,000   | 0.9ms | 1,055/s    | 21ms        | 72ms       | 83%    |
| 22,000   | 1.0ms | 1,000/s    | 46ms        | 101ms      | 83%    |

### Graph operations

| Operation | P50 | P95 |
|-----------|-----|-----|
| Create edge | 0.19ms | 0.35ms |
| Traverse (depth 1-3) | 0.17ms | 0.30ms |

### LLM-authored content (the real use case)

When tested with genuine capture-knowledge content — LLM-written understanding, not raw code chunks:

| Query type | Recall |
|---|---|
| Exact API lookups | 100% |
| Developer workflow questions | 100% |
| Architecture questions | 100% |
| Semantic paraphrases | 81% |
| **Overall** | **91%** |

## Optional: Neural Embeddings

The default installation uses a zero-dependency local embedder (character n-gram TF-IDF) that handles morphological similarity. For higher recall on semantic queries, install the neural backend:

```bash
pip install sage-memory[neural]
```

This adds [fastembed](https://github.com/qdrant/fastembed) with a 30MB ONNX model. sage-memory detects it automatically and enables hybrid search (FTS5 + vector similarity via Reciprocal Rank Fusion).

## Architecture

```
8 source files · ~1,500 lines Python · 70 lines SQL · 2 dependencies

src/sage_memory/
├── server.py       300 lines   7 MCP tools, dict-based dispatch
├── search.py       384 lines   Dual-DB search, FTS5 OR, RRF, filter_tags
├── store.py        233 lines   Store, update, delete, list
├── graph.py        194 lines   Link management, cycle-safe BFS traversal
├── embedder.py     175 lines   Embedder protocol + local + optional neural
├── db.py           190 lines   Project detection, dual DB, migrations
└── migrations/
    ├── 001.sql      53 lines   memories + FTS5 + vec0
    └── 002.sql      17 lines   edges table

skills/                         3 first-party skills (usable independently)
├── memory/                     Knowledge persistence + capture workflow
├── ontology/                   Typed knowledge graph
└── self-learning/              Mistake detection + prevention rules

docs/
├── adr-architecture.md         Full architectural decision record
├── skill-authoring.md          Guide for building skills on sage-memory
└── storage-protocol.md         Tag/content/scope conventions

tests/
└── test_all.py                 49 tests across 8 suites
```

### Design principles

**Lean.** ~1,500 lines. No frameworks, no abstractions beyond what's needed.

**Two dependencies.** `mcp` and `sqlite-vec`. Neural embedder is optional.

**Project-local.** Each project gets its own SQLite file. No cross-project noise.

**Zero configuration.** Auto-detects project root. Auto-creates database. Auto-routes queries.

**Correctness over cleverness.** FTS5 OR because AND breaks natural language queries. Normalized RRF because raw scores have incompatible scales. CASCADE deletes because orphaned edges are silent corruption. Deferred embedding because the write path shouldn't depend on the slowest component.

**Skills are instructions, not code.** The intelligence lives in skill files that shape LLM behavior. The server is a dumb, fast storage layer. This separation means skills can be rewritten for different LLMs without changing sage-memory, and sage-memory can evolve without breaking skills.

## Development

```bash
git clone https://github.com/xoai/sage-memory.git
cd sage-memory
pip install -e ".[dev]"
PYTHONPATH=src python tests/test_all.py
```

Run the server locally:

```bash
python -m sage_memory
```

## License

MIT
