# sage-memory

Persistent memory for AI coding assistants. Project-aware, zero-config, sub-3ms search.

sage-memory is an [MCP](https://modelcontextprotocol.io) server that gives LLMs long-term memory scoped to your project. Your AI assistant stores what it learns about your codebase — architecture decisions, patterns, gotchas, conventions — and retrieves it in future sessions.

- **91% recall** on natural language queries (BM25 ranking with OR semantics)
- **Sub-3ms search**, ~1,000 writes/sec on real codebases
- **Project-isolated** — each codebase gets its own database at `.sage-memory/`
- **Dual-scope** — project knowledge + global preferences, merged and ranked automatically
- **Zero configuration** — auto-detects project root, creates database, routes queries
- **2 dependencies, ~1,100 lines** — lean, auditable, no ML stack required
- **Works with** Claude Code, Cursor, Windsurf, VS Code, and any MCP-compatible client

```
~/code/billing-service/
  .sage-memory/memory.db    ← this project's knowledge (auto-created)
  src/
  tests/

~/.sage-memory/memory.db    ← your cross-project patterns & preferences
```

## Why

LLMs forget everything between sessions. Every time you open your editor, your AI assistant starts from scratch — re-reading files, re-discovering patterns, re-learning your codebase's quirks. sage-memory fixes this.

When your assistant figures out that "the billing service uses a saga pattern with compensating transactions," it stores that understanding. Next session, when you ask it to add a new payment method, it searches memory first and immediately has the architectural context it needs.

The knowledge lives *with your project*, not in a cloud service. Each project gets its own SQLite database. Your private codebase knowledge never leaves your machine.

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
    "memory": {
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
    "memory": {
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
    "memory": {
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

sage-memory manages two databases transparently:

**Project DB** (`.sage-memory/memory.db` at your project root) stores knowledge specific to this codebase — architecture, patterns, domain logic, debugging insights.

**Global DB** (`~/.sage-memory/memory.db`) stores cross-project knowledge — your coding conventions, preferred tools, style preferences.

Every search query hits both databases. Project results rank higher. You never think about which database to use — `scope: "project"` (default) writes to the project DB, `scope: "global"` writes to global.

### Search pipeline

```
query ─── FTS5 BM25 (OR semantics, stopword removal, prefix matching)
            │
            ├── term-frequency filtering (drops terms matching >20% of corpus)
            │
            ├── optional: sqlite-vec cosine similarity (when neural embedder installed)
            │
            └── Reciprocal Rank Fusion → normalize [0,1]
                  → project priority boost (+10%)
                  → tag match boost (+3% each, cap 15%)
                  → recency tiebreaker (14-day half-life)
                  → deduplicate across DBs
                  → top-k results
```

The key design choice: **FTS5 with OR semantics, not AND.** When you search "how does payment failure handling work," BM25 ranks documents by how many query terms match and how rare those terms are. A document matching 4 of 6 terms scores higher than one matching 1 of 6. AND semantics require ALL terms to match — which returns zero results for most natural language queries.

### Store pipeline

```
content ─── normalize ─── SHA-256 hash ─── dedup check
        ─── INSERT + FTS5 trigger ─── commit        [< 1ms]
        ─── embed + vec INSERT (if neural backend)   [deferred]
```

Embedding is decoupled from storage. The memory is keyword-searchable the instant it's stored. Vector indexing happens separately and only when a neural embedder is installed. If embedding fails, nothing is lost.

## Tools

### sage_memory_store

Store knowledge for later retrieval. The AI assistant calls this when it understands something worth remembering.

```json
{
  "content": "The billing service uses a saga pattern for multi-step payment processing. PaymentOrchestrator coordinates between StripeGateway, LedgerService, and NotificationService. Failures at any step trigger compensating transactions defined in saga_rollback_handlers.",
  "title": "Payment saga orchestration in billing service",
  "tags": ["billing", "payments", "architecture"],
  "scope": "project"
}
```

Content is SHA-256 hashed for automatic deduplication. If the same content is stored twice, sage-memory returns the existing entry's ID instead of creating a duplicate.

### sage_memory_search

Search across project and global knowledge using natural language.

```json
{
  "query": "how does payment failure handling work",
  "tags": ["billing"],
  "limit": 5
}
```

Results include a relevance score, source database label (`project` or `global`), and the full stored content. The assistant uses this to ground its responses in project-specific context.

### sage_memory_update

Update existing knowledge when understanding deepens or code changes. Only provide fields you want to change. Content changes automatically re-index for search.

### sage_memory_delete

Remove knowledge by ID when it becomes outdated or incorrect.

### sage_memory_list

Browse stored memories with pagination. Useful for auditing what the assistant has learned about your codebase.

## Best Workflow: Capture Knowledge

sage-memory is most effective with a deliberate knowledge-capture workflow:

1. **Explore**: Ask your AI assistant to analyze a module, service, or subsystem
2. **Understand**: It reads source code, traces dependencies, identifies patterns
3. **Store**: It persists its understanding via `sage_memory_store` with a descriptive title, detailed content, and relevant tags
4. **Document** (optional): It creates a `docs/ai/knowledge-{name}.md` companion file in your repo for human reference
5. **Retrieve**: On future tasks, it searches memory first for relevant context before reading code

This works because the AI writes both the stored content and later queries using consistent domain vocabulary — making keyword search highly effective without neural embeddings.

Example prompt to trigger this workflow:

> Analyze the authentication system in this project. Understand how it works, what patterns it uses, and store your understanding in memory for future sessions.

## Performance

Benchmarked against 4 real Python codebases (FastAPI, Pydantic, httpx, Rich — 340K lines total) with 50 adversarial queries across 5 categories.

### Scale

| Memories | Store mean | Throughput | Search mean | Search P95 | Recall |
|----------|-----------|------------|-------------|------------|--------|
| 1,000    | 1.0ms     | 1,000/s    | 2.5ms       | 9ms        | 80%    |
| 5,000    | 0.9ms     | 1,100/s    | 12ms        | 56ms       | 81%    |
| 10,000   | 0.9ms     | 1,055/s    | 21ms        | 72ms       | 83%    |
| 22,000   | 1.0ms     | 1,000/s    | 46ms        | 101ms      | 83%    |

Store throughput holds steady at ~1,000 memories/sec regardless of database size. Search stays under 50ms for typical per-project databases (< 15K memories).

### Recall by query category

| Category | Recall | What it tests |
|---|---|---|
| Exact API lookups | 95% | Finding specific classes, functions, APIs by name |
| Scoped queries | 91% | Same query, different project scopes |
| Semantic paraphrases | 66%* | Natural language with no keyword overlap |
| Cross-codebase | 68% | Searching across multiple codebases |
| Adversarial | 60% | Typos, single words, very long queries, edge cases |

\* Semantic recall reaches 85%+ with the optional neural embedder installed (`pip install sage-memory[neural]`).

### LLM-authored content (the real use case)

When tested with genuine capture-knowledge content — LLM-written understanding of the httpx codebase, not raw code chunks — retrieval quality jumps significantly:

| Query type | Recall |
|---|---|
| Exact API lookups | 100% |
| Developer workflow questions | 100% |
| Architecture questions | 100% |
| Semantic paraphrases | 81% |
| Adversarial | 83% |
| **Overall** | **91%** |

LLM-authored knowledge retrieves better because the AI uses consistent domain vocabulary when writing both the stored content and later queries — a natural fit for BM25 keyword ranking.

## Optional: Neural Embeddings

The default installation uses a zero-dependency local embedder (character n-gram TF-IDF hashing) that handles morphological similarity — "authenticate" ↔ "authentication" ↔ "auth" produce similar vectors. This is effective for LLM-authored content where vocabulary is consistent.

For higher recall on semantic queries with vocabulary gaps, install the neural backend:

```bash
pip install sage-memory[neural]
```

This adds [fastembed](https://github.com/qdrant/fastembed) with a 30MB ONNX model. No code changes needed — sage-memory detects the backend automatically and enables hybrid search (FTS5 + vector similarity fused via Reciprocal Rank Fusion).

## Architecture

```
7 files · ~1,100 lines of Python · 2 required dependencies (mcp, sqlite-vec)

src/sage_memory/
├── server.py       213 lines   MCP server, 5 tools, dict-based dispatch
├── search.py       328 lines   Dual-DB search, FTS5 OR, RRF fusion, access tracking
├── store.py        222 lines   Store, update, delete, list, deferred embedding
├── embedder.py     175 lines   Embedder protocol + local + optional neural
├── db.py           190 lines   Project detection, dual DB connections, migrations
└── migrations/
    └── 001.sql      53 lines   Schema: memories + FTS5 + vec0 index
```

### Design principles

**Lean.** 7 source files, ~1,100 lines. No frameworks, no abstractions beyond what's needed. Every line earns its place.

**Two dependencies.** `mcp` (the protocol) and `sqlite-vec` (vector search extension). The neural embedder is optional. No PyTorch, no heavy ML stack by default.

**Project-local databases.** Each project gets its own SQLite file. No cross-project noise. No scaling problems — a single project rarely exceeds 15K memories, which is FTS5's sweet spot.

**Zero configuration.** Auto-detects project root. Auto-creates database. Auto-routes to project or global scope. The developer adds one MCP config block and never thinks about it again.

**Correctness over cleverness.** FTS5 OR because AND breaks natural language queries. Normalized RRF scores because raw scores have incompatible scales. Content-hash dedup because LLMs will store the same insight repeatedly. Deferred embedding because the write path shouldn't depend on the slowest component.

## Development

```bash
git clone https://github.com/<your-org>/sage-memory.git
cd sage-memory
pip install -e ".[dev]"
pytest
```

Run the server locally:

```bash
python -m sage_memory
```

## License

MIT
