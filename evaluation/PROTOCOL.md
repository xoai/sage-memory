# sage-memory Evaluation Protocol

**Version:** 1.0
**Date:** 2025-03-18

## Purpose

This document defines four evaluations that test what sage-memory uniquely claims. Unlike LOCOMO-style benchmarks (which test end-to-end conversational extraction pipelines), these evaluations target sage-memory's specific differentiators: self-learning effectiveness, knowledge accumulation, retrieval quality, and graph-enhanced recall.

## Why Not LOCOMO?

LOCOMO tests a full pipeline: conversation → automatic extraction → storage → retrieval → answer generation. Mem0, OpenMemory, and SimpleMem own this entire pipeline — their LLM extracts facts from conversations automatically.

sage-memory doesn't auto-extract. The LLM decides what to store, guided by skills. Running LOCOMO would test our extraction prompt quality, not our storage and retrieval engine. We'd be benchmarking the wrong layer.

Instead, we test what we own and what no competitor measures.

---

## Evaluation 1: Self-Learning Effectiveness

### What it proves

The agent makes fewer mistakes over time. This is sage-memory's core differentiator — no competitor publishes behavioral improvement metrics.

### Protocol

**Setup:** A set of 20 tasks across 5 domains (Stripe integration, Docker builds, database migrations, auth middleware, CI pipeline). Each task has 1-2 known gotchas that an uninformed agent would hit.

**Ground truth:** For each task, we know the correct approach AND the common mistake. Example: "Implement Stripe webhook handler" → gotcha: must use raw body before JSON parsing. The mistake is using parsed body, causing signature verification failure.

**Phase 1 — Baseline (no memory):**
Run each task with an LLM. Score whether the agent makes the known mistake. This establishes the baseline mistake rate.

**Phase 2 — Learning (store prevention rules):**
For each task where the agent made a mistake, store a prevention rule via `sage_memory_store` with the self-learning skill's four-part format. This simulates the self-learning capture phase.

**Phase 3 — Re-test (with memory):**
Run the same 20 tasks again. Before each task, the agent calls `sage_memory_search(filter_tags: ["self-learning"])`. Score whether the agent avoids the previously-made mistakes.

**Phase 4 — Transfer (new tasks, same domains):**
Run 20 NEW tasks in the same 5 domains. These tasks involve the same gotchas but in different contexts (e.g., "Implement GitHub webhook handler" instead of "Implement Stripe webhook handler"). Score whether prevention rules transfer.

### Metrics

| Metric | Definition |
|---|---|
| Baseline mistake rate | % of tasks where the agent makes the known mistake (Phase 1) |
| Post-learning mistake rate | % of tasks where the mistake recurs after learning (Phase 3) |
| Mistake avoidance rate | 1 - (post-learning rate / baseline rate) |
| Transfer rate | % of new tasks where the prevention rule transfers (Phase 4) |
| Prevention recall | % of stored prevention rules that are retrieved when relevant |

**Target:** Mistake avoidance rate ≥ 80%. Transfer rate ≥ 50%.

### Data Requirements

- 20 initial tasks with known gotchas (human-authored)
- 20 transfer tasks (same domains, different contexts)
- LLM API access for agent simulation
- LLM-as-judge for mistake detection scoring

### Implementation Notes

This evaluation requires actual LLM API calls. The test harness provides the framework — seed the tasks, run phases, score results. The LLM calls can use any model (Claude, GPT-4, etc.) via the Anthropic or OpenAI API.

Can be run without LLM calls in "simulated" mode using pre-authored agent responses for protocol validation.

---

## Evaluation 2: Knowledge Accumulation Over Sessions

### What it proves

The agent gives better answers about a codebase after storing knowledge than without. Memory is useful, not just stored.

### Protocol

**Setup:** A real codebase (httpx — already used in our benchmarks) and 20 questions about it ranging from simple ("what HTTP methods does the Client support?") to architectural ("how does the redirect handling preserve security?").

**Phase 1 — No memory baseline:**
An LLM answers the 20 questions with NO memory and NO access to the codebase. This measures raw model knowledge.

**Phase 2 — Code-only baseline:**
The LLM answers with access to key source files but no stored memory. This measures what reading code alone provides.

**Phase 3 — Memory-assisted:**
Run `sage learn` on the httpx codebase (simulated: store 15-20 memories covering architecture, patterns, conventions, and gotchas). Then the LLM answers the same 20 questions using only `sage_memory_search` results (no direct code access). This measures what accumulated memory provides.

**Phase 4 — Memory + code:**
The LLM answers with both memory search results AND code access. This measures whether memory adds value on top of code reading.

### Metrics

| Metric | Definition |
|---|---|
| No-memory score | LLM-judge score on Phase 1 answers |
| Code-only score | LLM-judge score on Phase 2 answers |
| Memory-only score | LLM-judge score on Phase 3 answers |
| Memory+code score | LLM-judge score on Phase 4 answers |
| Memory lift | Memory-only score - No-memory score |
| Combined lift | Memory+code score - Code-only score |

**Target:** Memory lift ≥ 30 points. Combined lift ≥ 10 points.

### Scoring

LLM-as-judge (binary: correct/incorrect, plus a 0-5 quality scale) comparing each answer against a human-authored ground truth. Run 3 times, report mean ± std.

---

## Evaluation 3: Retrieval Quality

### What it proves

sage-memory's search engine finds the right content reliably across query types and corpus sizes. This is our existing benchmark formalized as a reproducible evaluation.

### Protocol

**Corpus:** LLM-authored knowledge entries about 4 real Python codebases:
- FastAPI (web framework, ~107K lines)
- Pydantic (data validation, ~163K lines)
- httpx (HTTP client, ~17K lines)
- Rich (terminal formatting, ~51K lines)

**Corpus sizes tested:** 500, 1K, 5K, 10K, 22K chunks.

**Queries:** 50 developer questions across 5 categories:

| Category | Count | Example |
|---|---|---|
| Exact API lookup | 10 | "AsyncClient send request" |
| Scoped queries | 10 | Same query across different project scopes |
| Semantic paraphrase | 10 | "how does the library handle following links" (no keyword overlap) |
| Cross-codebase | 10 | Queries that span multiple codebases |
| Adversarial | 10 | Typos, single words, very long queries |

**Ground truth:** Each query has a set of expected memory IDs. A result is "relevant" if its content matches the expected topic (verified by keyword presence in title + content).

### Metrics

| Metric | Definition |
|---|---|
| Recall@k | % of relevant memories found in top-k results |
| Precision@k | % of top-k results that are relevant |
| MRR | Mean Reciprocal Rank of first relevant result |
| Latency P50/P95 | Search latency percentiles |
| Throughput | Stores per second |

Report per-category and overall.

**Target:** Overall recall ≥ 80%. Exact API recall ≥ 95%. Latency P95 < 50ms at 10K scale.

### Additional: OR vs AND comparison

Run the same queries against an AND-based FTS5 backend (same schema, same content) to demonstrate the OR advantage quantitatively. No competitor names — just "OR semantics" vs "AND semantics."

---

## Evaluation 4: Graph-Enhanced Recall

### What it proves

Graph edges improve precision for entity-linked queries without degrading recall. The ontology integration works.

### Protocol

**Setup:** 25 seed learnings (from the self-learning test suite) + 4 ontology entities with edge tags. Create graph edges via `sage_memory_link` connecting learnings to entities.

**Queries:** 8 task-based queries, each with a known set of relevant learnings.

**Three retrieval strategies compared:**

1. **Keyword only:** `sage_memory_search(query, filter_tags: ["self-learning"])`
2. **Edge tags:** `sage_memory_search(filter_tags: ["self-learning", "edge:{entity_id}"])`
3. **Graph traversal:** `sage_memory_graph(id, relation: "applies_to", direction: "inbound")`

### Metrics

| Metric | Keyword | Edge tags | Graph |
|---|---|---|---|
| Precision@k | measured | measured | measured |
| Recall@k | measured | measured | measured |
| Latency | measured | measured | measured |
| MCP calls needed | 1 | 1 | 1 |

**Target:** Graph precision ≥ 0.90 (vs keyword ~0.41 from prior test). Graph recall ≥ keyword recall.

### Additional: CASCADE correctness

Delete an entity, verify all its edges are removed, verify graph traversal returns empty for that entity.

---

## Reproducibility

All evaluations use:
- sage-memory v0.5.0 (SQLite, FTS5, local embedder)
- Python 3.11+
- Deterministic seed data (included in `evaluation/seed/`)
- No external API calls for evaluations 3 and 4
- LLM API calls for evaluations 1 and 2 (with temperature=0 for reproducibility)

Results include: raw data (JSON), computed metrics, and the commands to reproduce.

---

## Relationship to Existing Benchmarks

| Benchmark | What it tests | Applicable to sage-memory? |
|---|---|---|
| LOCOMO | Full pipeline: extract → store → retrieve → answer | No — we don't auto-extract |
| LoCoMo-10 (SimpleMem) | Same as LOCOMO, smaller scale | No — same reason |
| Our Eval 1 | Self-learning behavioral improvement | Yes — unique to us |
| Our Eval 2 | Knowledge accumulation utility | Yes — tests our core value |
| Our Eval 3 | Retrieval quality at scale | Yes — tests our engine |
| Our Eval 4 | Graph-enhanced precision | Yes — tests our graph layer |

Evaluations 1 and 2 are novel — no competitor publishes these metrics. Evaluation 3 is standard IR evaluation applied to our specific domain. Evaluation 4 tests our graph layer against our own keyword baseline.
