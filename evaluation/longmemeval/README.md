# LongMemEval Benchmark for sage-memory

Integration of [LongMemEval](https://github.com/xiaowu0162/longmemeval) (ICLR 2025) with sage-memory.

LongMemEval tests five long-term memory abilities across 500 questions: information extraction, multi-session reasoning, knowledge updates, temporal reasoning, and abstention.

📋 **For a step-by-step reproducer walkthrough** (clone → install → run →
verify against published numbers), see **[REPRODUCER.md](REPRODUCER.md)**.

📊 **For the latest benchmark report** with results vs gbrain and the
LongMemEval BM25 baseline, see **[REPORT.md](REPORT.md)**.

## Headline Numbers (500 questions, bm25-full mode)

| Config | R@1 | R@3 | **R@5** | R@10 | Cost |
|---|---:|---:|---:|---:|---|
| Free-path (FTS5+RRF, Local 384d) | 0.834 | 0.952 | **0.972** | 0.986 | $0 |
| Hosted (OpenAI 1536d) | 0.886 | 0.970 | **0.986** | 0.992 | ~$0.50 |
| gbrain (published) | — | — | 0.976 | — | hosted |
| LongMemEval BM25 baseline | — | — | ~0.70 | — | $0 |

See [REPORT.md](REPORT.md) for per-question-type breakdown and methodology.

## Quick Start

```bash
# 1. Install sage-memory
pip install sage-memory

# 2. Download dataset
mkdir -p data
cd data
wget https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json
wget https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
cd ..

# 3. Set API key
export ANTHROPIC_API_KEY=sk-...

# 4. Run (quick test — 20 questions, oracle data)
python run_longmemeval.py \
  --data_file data/longmemeval_oracle.json \
  --mode raw --llm anthropic \
  --output results/sage_oracle_raw.jsonl \
  --limit 20

# 5. Evaluate with Claude
python evaluate.py \
  --hyp results/sage_oracle_raw.jsonl \
  --ref data/longmemeval_oracle.json \
  --llm anthropic

# Or evaluate with OpenAI (compatible with official LongMemEval)
export OPENAI_API_KEY=sk-...
python evaluate.py \
  --hyp results/sage_oracle_raw.jsonl \
  --ref data/longmemeval_oracle.json \
  --llm openai --model gpt-4o
```

## Retrieval Benchmark (bench_longmemeval.py)

Direct comparison with MemPal/ChromaDB. Measures retrieval only — Recall@k and NDCG@k — no LLM calls needed.

```bash
# BM25 baseline (sage-memory's strength — FTS5 OR semantics)
python bench_longmemeval.py data/longmemeval_s_cleaned.json --mode bm25

# BM25 on both user + assistant turns
python bench_longmemeval.py data/longmemeval_s_cleaned.json --mode bm25-full

# BM25 + keyword overlap re-ranking
python bench_longmemeval.py data/longmemeval_s_cleaned.json --mode hybrid

# BM25 + keyword + temporal date proximity boost
python bench_longmemeval.py data/longmemeval_s_cleaned.json --mode hybrid-temporal

# Quick test (20 questions)
python bench_longmemeval.py data/longmemeval_oracle.json --mode bm25 --limit 20
```

Outputs Recall@k and NDCG@k at session and turn level, per question type breakdown, and a JSONL log compatible with LongMemEval's evaluation pipeline.

## Hosted-Vector Benchmark (bench_hosted.py)

Tests sage-memory with a hosted embedder (OpenAI / Voyage / Cohere) for
side-by-side comparison against the free-path (FTS5-only) baseline.

The bench harness's standard mode runs each question against a fresh
per-question DB. Fresh DBs default to `vec_dim=384` (per migration 001),
so the embedder resolver picks FastEmbedder/LocalEmbedder even when
`OPENAI_API_KEY` is set. `bench_hosted.py` works around this by
recreating `memories_vec` + `chunks_vec` at the hosted embedder's
native dim before any `store()` call.

```bash
# 1. Set the hosted-embedder API key (one of)
export OPENAI_API_KEY=sk-...     # → text-embedding-3-small, 1536d
export VOYAGE_API_KEY=pa-...     # → voyage-3-lite, 512d
export COHERE_API_KEY=...        # → embed-english-v3.0, 1024d

# 2. Run benchmark
# Full 500q on bm25-full mode (~3 hours, ~$0.50 with OpenAI)
python bench_hosted.py bm25-full

# 50 questions (~25 min, ~$0.05)
python bench_hosted.py bm25-full 50

# Filter by question_type (n=30 for preference, ~17 min)
python bench_hosted.py bm25-full 999 single-session-preference
```

Outputs aggregate R@1/R@3/R@5/R@10 plus per-question-type R@5 breakdown.

Modes match the free-path bench (`bm25` / `bm25-full` / `hybrid-temporal`).
LLM stages (expand/rerank) are forced off so the comparison isolates the
vector-channel contribution.

### When to use which bench

| | `bench_longmemeval.py` | `bench_hosted.py` |
|---|---|---|
| Default embedder | LocalEmbedder (384d) | OpenAI/Voyage/Cohere (auto) |
| Free-path scrub | Recommended | Not applicable |
| Cost | $0 | ~$0.05 per 50q with OpenAI |
| What it measures | Pure FTS5 + chunk RRF | Adds the vector channel |
| Use for | The headline number | Marginal lift from hosted vector |

## QA Benchmark (run_longmemeval.py)

Full pipeline for QA evaluation — requires LLM API.

## How It Works

```
LongMemEval question
  │
  ├── haystack_sessions (40-500 chat sessions)
  │
  ▼
┌─────────────────────┐
│  Ingest into         │  raw mode: store sessions as-is
│  sage-memory         │  extract mode: LLM extracts facts first
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│  sage_memory_search  │  BM25 OR semantics, top-K retrieval
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│  Generate answer     │  Claude or GPT with retrieved context
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│  LLM-as-judge        │  Claude or GPT-4o evaluates correctness
└─────────────────────┘
```

Each question gets its own isolated sage-memory database. No cross-question leakage.

## Modes

### Raw mode (`--mode raw`)
Stores each chat session verbatim as a single memory entry. Fast — no LLM calls during ingestion. Relies on sage-memory's BM25 search to find relevant sessions.

Best for: quick testing, baseline measurement.

### Extract mode (`--mode extract`)
Uses an LLM to extract key facts from each session before storing. Better recall — extracted facts are denser and more searchable than raw conversation text.

Best for: maximum accuracy, comparing against other memory systems.

## Data Files

| File | Sessions/question | Difficulty | Recommended for |
|------|-------------------|------------|-----------------|
| `longmemeval_oracle.json` | ~5-10 (evidence only) | Easiest | Development, quick testing |
| `longmemeval_s_cleaned.json` | ~40 (~115K tokens) | Medium | Standard benchmark |
| `longmemeval_m_cleaned.json` | ~500 | Hardest | Stress testing |

## Evaluation

### With Claude
```bash
export ANTHROPIC_API_KEY=sk-...
python evaluate.py \
  --hyp results/sage_oracle_raw.jsonl \
  --ref data/longmemeval_oracle.json \
  --llm anthropic
```

### With OpenAI
```bash
export OPENAI_API_KEY=sk-...
python evaluate.py \
  --hyp results/sage_oracle_raw.jsonl \
  --ref data/longmemeval_oracle.json \
  --llm openai --model gpt-4o
```

### With LongMemEval's official evaluator
```bash
export OPENAI_API_KEY=sk-...
cd /path/to/longmemeval/src/evaluation
python evaluate_qa.py gpt-4o /path/to/sage_oracle_raw.jsonl /path/to/longmemeval_oracle.json
```

All three produce the same output format.

## Reproducibility Floor

Captured as part of M0 milestone (initiative `retrieval-upgrade`,
2026-05-16). Required for per-milestone gate comparisons.

| Field | Value |
|-------|-------|
| dataset SHA-256 | `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442` |
| dataset path | `data/longmemeval_s_cleaned.json` (500 questions) |
| sage-memory git SHA | `37cd54ac4cc2fa9b5ecd4aabe94e18fc4b0f7553` (short: `37cd54a`) |
| Python version | `3.12.3` |
| Venv | `~/.venvs/sage-memory` (uv venv + `uv pip install -e .[neural,dev]`) |
| Active embedder | `LocalEmbedder` (name=`local`, dim=384, quality=0.45) — note: not exercised by `bm25`/`bm25-full`/`hybrid-temporal` modes; harness uses FTS5 BM25 only |
| `SAGE_EVAL_SEED` | `none` — no random sources in metric path; harness is deterministic |

### Exact commands per baseline

All baselines were captured in a free-path-scrubbed sub-shell to
guarantee no LLM keys leak into the retrieval-only run:

```bash
cd evaluation/longmemeval

# bm25 baseline
env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u VOYAGE_API_KEY -u COHERE_API_KEY \
  python bench_longmemeval.py data/longmemeval_s_cleaned.json \
    --mode bm25 \
    --out results/baseline_37cd54a_20260516_bm25.jsonl

# bm25-full baseline
env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u VOYAGE_API_KEY -u COHERE_API_KEY \
  python bench_longmemeval.py data/longmemeval_s_cleaned.json \
    --mode bm25-full \
    --out results/baseline_37cd54a_20260516_bm25-full.jsonl

# hybrid-temporal baseline
env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u VOYAGE_API_KEY -u COHERE_API_KEY \
  python bench_longmemeval.py data/longmemeval_s_cleaned.json \
    --mode hybrid-temporal \
    --out results/baseline_37cd54a_20260516_hybrid-temporal.jsonl
```

### Determinism note

The harness is deterministic at the metric level — no random sources
in the metric computation path. `tempfile.mkdtemp()` is the only
randomness, but its random suffix affects only per-question DB paths,
which never appear in `corpus_id`, `recall_any@k`, `recall_all@k`, or
`ndcg_any@k`. Two back-to-back runs produce byte-identical fingerprints
on `(question_id, corpus_id_sequence, metrics)`. Per-milestone gates
therefore use single runs (no median-of-3 fallback needed).

Full audit at `.sage/work/20260516-retrieval-upgrade/M0/audit.md`
(local-only). Determinism-verification log at `.sage/work/20260516-retrieval-upgrade/M0/runs/determinism-verify.log`.

## Expected Cost

| Config | Questions | API calls | Estimated cost |
|--------|-----------|-----------|----------------|
| Oracle + raw + 20 limit | 20 | ~40 | ~$0.20 |
| Oracle + raw + all | 500 | ~1000 | ~$5 |
| S + extract + all | 500 | ~20,000+ | ~$50+ |

Extract mode calls the LLM once per session for fact extraction, which significantly increases API usage for larger datasets.

## Output Format

Compatible with LongMemEval's evaluation pipeline:

```jsonl
{"question_id": "q_001", "hypothesis": "The user mentioned they prefer Italian food..."}
{"question_id": "q_002", "hypothesis": "Based on the conversation from March 5th..."}
```
