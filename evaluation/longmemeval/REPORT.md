# sage-memory on LongMemEval-S — Benchmark Report

**Dataset:** LongMemEval-S (`longmemeval_s_cleaned.json`, 500 questions, ~115K tokens per question across ~50 chat sessions, SHA-256 `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`)
**System under test:** sage-memory git SHA `aafe814` (post-v0.7 bootstrap fix)
**Date:** 2026-05-18
**Reproducer:** see [REPRODUCER.md](REPRODUCER.md)

---

## Headline

| Config | R@1 | R@3 | **R@5** | R@10 | API cost |
|---|---|---|---|---|---|
| **Free-path** (FTS5 + RRF, LocalEmbedder 384d) | 0.834 | 0.952 | **0.972** | 0.986 | **$0** |
| **Hosted-vector** (FTS5 + RRF + OpenAI 1536d) | 0.886 | 0.970 | **0.986** | 0.992 | ~$0.50 per 500q |
| Hosted-vector Δ over free-path | +5.2pp | +1.8pp | **+1.4pp** | +0.6pp | |

- **Free-path 97.2% R@5** is the headline number for users with no embedder API key. Pure FTS5 BM25 + chunk-level RRF fusion, 384d local embeddings used only for the vector channel (which contributes little to R@5 on this dataset). No LLM calls in the retrieval path.
- **Hosted-vector 98.6% R@5** demonstrates the ADR-005 cascade is doing real work when a hosted embedder is configured. The +1.4pp lift comes mostly from semantic question types (single-session-preference, temporal-reasoning).

---

## Comparison with published systems

| System | R@5 on LongMemEval-S | Embedder | Notes |
|---|---|---|---|
| sage-memory (free-path) | **0.972** | LocalEmbedder 384d (free) | Pure FTS5+RRF, no API |
| sage-memory (hosted) | **0.986** | OpenAI 3-small 1536d | +1.4pp lift, ~$0.50 per 500q |
| gbrain (published) | 0.976 | (hosted, see paper) | Published baseline |
| LongMemEval-paper BM25 | ~0.70 | n/a | Naïve BM25 only |

sage-memory's free-path matches gbrain within noise (0.972 vs 0.976) while consuming zero API budget. The hosted-vector configuration overtakes gbrain by 1.0pp.

---

## Per-type R@5 breakdown

| Question type | n | Free-path | Hosted | Δ |
|---|---:|---:|---:|---:|
| knowledge-update | 78 | 0.987 | **1.000** | +1.3pp |
| multi-session | 133 | 0.992 | **1.000** | +0.8pp |
| single-session-assistant | 56 | **1.000** | **1.000** | 0 |
| single-session-preference | 30 | 0.867 | **0.900** | +3.3pp |
| single-session-user | 70 | 0.971 | **0.986** | +1.5pp |
| temporal-reasoning | 133 | 0.955 | **0.977** | +2.2pp |

Three types saturate to 1.000 with hosted vector. The largest lifts come from `single-session-preference` (semantic paraphrase queries) and `temporal-reasoning` (date-keyed lookups where the vector channel adds discrimination beyond FTS5 token overlap).

---

## Methodology

**Retrieval pipeline (free-path):**
- Each question gets an isolated SQLite DB.
- 40-55 chat sessions per question are ingested as memories (one memory per session, full user+assistant turns in `bm25-full` mode).
- `sage_memory_search(query, limit=50)` runs the standard six-stage pipeline: FTS5 BM25 (token + literal), chunk-level RRF, optional re-ranking. LLM rerank disabled for this benchmark to isolate the retrieval-channel contribution.
- The top-K corpus_ids are extracted and compared against `answer_session_ids`. Recall is "any" (hit if any gold session lands in top-K).

**Hosted-vector path** uses the same pipeline but with `OPENAI_API_KEY` set, which makes the resolver pick `OpenAIEmbedder` (1536d). `bench_hosted.py` recreates the `memories_vec` and `chunks_vec` virtual tables at 1536d before any `store()` call so the vec0 dim matches.

**LLM stages off.** Both runs disable query expansion and LLM re-ranking (`expand=False, rerank=False`). The report measures pure retrieval channel performance, not generation.

---

## Reproducibility

- All numbers reproduce byte-for-byte. The harness is deterministic — no random sources in the metric path. See REPRODUCER.md for the exact commands.
- Free-path baseline log: `.sage/work/20260516-retrieval-upgrade/M0/runs/bm25-full.log`
- Hosted-vector run log: `/tmp/bench_hosted_500_all.log` (regenerable)
- Result JSONLs in `evaluation/longmemeval/results/` are gitignored (large + regenerable).

To re-run:

```bash
# free-path (no API key)
python evaluation/longmemeval/bench_longmemeval.py \
  evaluation/longmemeval/data/longmemeval_s_cleaned.json --mode bm25-full

# hosted-vector (with API key)
OPENAI_API_KEY=sk-... python evaluation/longmemeval/bench_hosted.py bm25-full
```

---

## Cost

| Run | Wall time | API cost |
|---|---|---|
| Free-path 500q | ~36 sec | $0 |
| Hosted 500q (OpenAI 3-small) | ~4h 43m | ~$0.50 |

Hosted run cost is dominated by embedding generation (~50 sessions × 500 questions = 25K embedding calls @ $0.00002/1K tokens for text-embedding-3-small).

---

## What this benchmark does NOT cover

- **End-to-end QA accuracy.** This report measures retrieval recall only. For full QA (retrieve → generate → judge), run `run_longmemeval.py` + `evaluate.py` — see README.md.
- **Ingest extraction quality.** sage-memory does not auto-extract facts from conversations; the LLM (or harness) decides what to store. LongMemEval's raw-ingest mode bypasses this layer entirely.
- **Larger haystacks.** LongMemEval-M (~500 sessions per question) is a stress test for the same pipeline. Numbers not yet captured.
