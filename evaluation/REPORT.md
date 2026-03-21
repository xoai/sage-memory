# sage-memory Evaluation Report

**Version:** v0.5.0
**Date:** 2025-03-18

---

## Summary

Four evaluations across two methodologies: deterministic (local, reproducible, no LLM) and live (LLM-as-judge, informational only).

| Eval | What it tests | Method | Key metric | Result |
|------|---------------|--------|------------|--------|
| 1. Self-Learning Retrieval | Right prevention rule for right task | Deterministic | Mean recall | **94%** ✅ |
| 2. Knowledge Context Coverage | Search returns answer-relevant facts | Deterministic | Fact coverage | **100%** ✅ |
| 3. Retrieval Quality | OR vs AND, recall across query types | Deterministic | Mean recall | **93%** ✅ |
| 4. Graph-Enhanced Recall | Precision from graph vs keyword | Deterministic | Precision delta | **+0.23** ✅ |

All deterministic evaluations are fully reproducible. Same data, same results, every run.

---

## Evaluation 1: Self-Learning Retrieval

**Question:** When an agent starts a task, does sage-memory retrieve the correct prevention rule from past mistakes?

### Setup

- 50 prevention rules stored with `filter_tags: ["self-learning"]` across 7 domains
- 8 noise entries (regular architecture knowledge, no self-learning tag)
- 49 task queries, each mapped to 1-3 expected prevention rules
- Scoring: keyword match — at least 50% of the expected learning's keywords must appear in retrieved results

### Results by Domain

| Domain | Tasks | Recall | Precision |
|--------|-------|--------|-----------|
| Stripe/billing | 8 | 100% | 28% |
| Docker | 7 | 100% | 23% |
| Database/Redis | 8 | 100% | 34% |
| Auth | 8 | 88% | 18% |
| CI/CD | 6 | 92% | 21% |
| API/SDK | 6 | 100% | 21% |
| Testing | 6 | 75% | 17% |
| **Overall** | **49** | **94%** | **23%** |

### Key Metrics

| Metric | Value | Target |
|--------|-------|--------|
| Mean recall | 94% | ≥ 80% ✅ |
| Perfect recall rate | 92% | ≥ 70% ✅ |
| Noise isolation | 100% | ≥ 95% ✅ |
| Latency P50 / P95 | 1.1ms / 3.1ms | P95 < 10ms ✅ |

### Failure Analysis

4 of 49 queries had partial or zero recall:

1. **"Fix 401 errors when two API calls fire simultaneously"** → expected L26 (token refresh race condition). BM25 couldn't bridge "401 errors" and "two API calls" to "token refresh race condition." The query uses the symptom; the prevention rule uses the cause.

2. **"Test a function that uses the current timestamp"** → expected L46 (mocking datetime.now). The query says "current timestamp" but the learning says "datetime.now" and "freezegun." Vocabulary gap.

3. Two multi-target queries found 1 of 2 expected learnings — partial recall, not total miss.

**Root cause for misses:** BM25 matches on terms, not semantics. When the query uses completely different vocabulary than the stored content, recall drops. This is the known limitation that neural embeddings would address (optional `sage-memory[neural]` install).

### Precision Note

Precision is 23% because `limit=5` returns 5 results but most tasks only expect 1-2 learnings. The remaining results are other learnings in the same domain — relevant but not the exact expected match. This is acceptable behavior: the agent sees the correct prevention rule plus related learnings from the same area.

---

## Evaluation 2: Knowledge Context Coverage

**Question:** When an agent asks about a codebase, does sage-memory return results containing the facts needed to answer?

### Setup

- 20 knowledge entries about the httpx library (architecture, patterns, conventions)
- 30 developer questions with ground-truth facts
- Scoring: what percentage of expected facts appear in the search results?

### Results

| Metric | Value | Target |
|--------|-------|--------|
| Mean fact coverage | 100% | ≥ 80% ✅ |
| Perfect coverage rate | 100% | ≥ 70% ✅ |
| Questions with results | 100% | = 100% ✅ |
| Latency P50 | 0.7ms | < 5ms ✅ |

Every question's key facts appeared in the top-5 search results. The knowledge is stored in a way that retrieves well for developer queries.

### What This Means

If the LLM has the right facts in context, it can answer correctly. sage-memory's job is to provide those facts. This evaluation proves it does — 100% of the time for this corpus.

The remaining variable is the LLM's reasoning quality, which sage-memory doesn't control. The live eval (below) showed +0.7/5 lift when Claude Sonnet used memory-retrieved context vs answering from training knowledge alone.

---

## Evaluation 3: Retrieval Quality (OR vs AND)

**Question:** How much does OR semantics improve retrieval over AND across different query types?

### Setup

- 20 knowledge entries, 29 queries across 5 categories
- Same corpus searched with OR semantics (sage-memory) and AND semantics (standard FTS5)

### Results

| Category | OR Recall | AND Recall | OR MRR |
|----------|-----------|------------|--------|
| Exact API lookup | 100% | 100% | 1.00 |
| Semantic paraphrase | 78% | 0% | 0.89 |
| Workflow questions | 100% | 20% | 1.00 |
| Architecture questions | 100% | 0% | 0.90 |
| Adversarial | 100% | 20% | 1.00 |
| **Overall** | **93%** | **24%** | **0.95** |

**Latency:** P50=0.7ms, P95=2.9ms
**Acceptable recall (≥50%):** OR: 97%, AND: 24%
**OR advantage: +69% mean recall**

### Analysis

AND only works for exact API lookups where query terms match stored content verbatim. For every other query type — semantic paraphrases, workflow questions, architecture questions — AND returns zero or near-zero results. This is the single most impactful design decision in sage-memory.

---

## Evaluation 4: Graph-Enhanced Recall

**Question:** Do graph edges improve precision for entity-linked queries?

### Setup

- 11 learnings linked to 4 ontology entities via `sage_memory_link`
- Compare keyword search (`filter_tags`) vs graph traversal (`sage_memory_graph`)

### Results

| Entity | Keyword P | Graph P | Keyword R | Graph R | ΔP |
|--------|-----------|---------|-----------|---------|-----|
| task_payment | 0.33 | 1.00 | 0.33 | 1.00 | +0.67 |
| task_stripe | 0.75 | 1.00 | 1.00 | 1.00 | +0.25 |
| task_auth | 1.00 | 1.00 | 1.00 | 1.00 | +0.00 |
| task_docker | 1.00 | 1.00 | 1.00 | 1.00 | +0.00 |
| **Average** | **0.77** | **1.00** | **0.83** | **1.00** | **+0.23** |

**CASCADE test:** ✅ Deleting entity removes all edges automatically.

### Analysis

Graph traversal gives perfect precision and recall. The advantage is largest where learnings use different vocabulary than the entity description (task_payment: +0.67). When keywords align naturally, both methods perform equally.

---

## Supplementary: Live LLM Evaluation (Informational)

We ran Evals 1 and 2 with Claude Sonnet 4 as both agent and judge. These results are **informational only** — the LLM judge had calibration issues that make the numbers unreliable for the behavioral assessment (though the retrieval metrics are trustworthy).

### Eval 1 Live — Self-Learning

| Phase | Metric | Value |
|-------|--------|-------|
| Baseline (no memory) | Mistake rate | 60% (6/10 tasks) |
| With memory | Prevention recall | 100% (6/6 rules retrieved) |
| With memory | Corrected avoidance | 50-67% (3-4 of 6 mistakes avoided) |
| Transfer | Transfer rate | 60% (3/5 new-domain transfers) |

**Key finding:** Claude Sonnet already knows many common gotchas from training (40% of tasks correct at baseline). sage-memory adds most value for **project-specific** knowledge the LLM can't have from training data.

**Judge issue:** Binary "did they make the mistake?" was too coarse. The judge flagged "discussing a precaution" as "making the mistake." Rubric-based scoring recommended for v2.

### Eval 2 Live — Knowledge Accumulation

| Phase | Score | Accuracy |
|-------|-------|----------|
| No memory | 3.7/5 | 80% |
| With memory | 4.4/5 | 100% |
| Lift | +0.7 | +20% |

**Key finding:** Memory lift is moderate for a well-known public library (httpx). Biggest improvements (+2 points) were on internal implementation details the LLM doesn't fully know from training.

---

## Cross-Evaluation Findings

### What the data proves

1. **Retrieval is reliable.** 93-100% recall across all deterministic evaluations. The right content surfaces for the right query.

2. **Namespace isolation is perfect.** `filter_tags: ["self-learning"]` never leaked non-learning content. Zero noise in 49 queries.

3. **OR semantics are essential.** +69% recall over AND. Without OR, sage-memory would be useless for natural language queries.

4. **Graph edges eliminate false positives.** 1.00 precision vs 0.77 keyword. Perfect targeting when entities are linked.

5. **Latency is consistently sub-5ms.** P95 under 3.1ms for all evaluations.

### Known limitations

1. **BM25 can't bridge vocabulary gaps.** When query and content use completely different words for the same concept ("401 errors" vs "token refresh race condition"), recall drops. Neural embeddings mitigate this.

2. **Precision is low with broad limit.** `limit=5` returns 5 results when only 1-2 are expected. This is by design (surfacing related context), but precision metrics look low.

3. **Self-learning value depends on knowledge novelty.** For well-known gotchas, the LLM already knows. Maximum value is for private, project-specific knowledge.

---

## Reproducibility

All deterministic evaluations (recommended):

```bash
# All 4 evals — fully local, no API key needed
PYTHONPATH=src python evaluation/run_eval.py --eval all
PYTHONPATH=src python evaluation/run_eval_deterministic.py
```

Live evaluations (informational):

```bash
ANTHROPIC_API_KEY=<key> PYTHONPATH=src python evaluation/run_eval_live.py --eval all
```

## Files

```
evaluation/
├── PROTOCOL.md                    Detailed evaluation protocol
├── REPORT.md                      This report
├── run_eval.py                    Evals 3 + 4 (local)
├── run_eval12.py                  Evals 1 + 2 simulated mode
├── run_eval_deterministic.py      Evals 1 + 2 deterministic (recommended)
├── run_eval_live.py               Evals 1 + 2 with LLM (informational)
└── seed/
    └── eval1_self_learning_tasks.json   Task seed data
```
