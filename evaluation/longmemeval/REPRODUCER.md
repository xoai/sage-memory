# How to reproduce sage-memory's LongMemEval-S benchmark results

This guide is intentionally exhaustive. Following it should produce
**byte-identical** JSONL output to the published baselines for the
free-path runs, and roughly-equivalent metrics for the hosted runs
(some run-to-run variance from OpenAI embedding non-determinism).

## What you'll measure

LongMemEval-S contains **500 questions** across 6 types:

| Type | n | What it tests |
|---|:---:|---|
| multi-session | 133 | Cross-session reasoning |
| temporal-reasoning | 133 | Date/time queries |
| knowledge-update | 78 | Latest-fact override |
| single-session-user | 70 | Single-session user-stated facts |
| single-session-assistant | 56 | Single-session assistant-stated facts |
| single-session-preference | 30 | Semantic preference queries |

Each question has a `haystack_sessions` field of 40-50 chat sessions,
with the answer hidden in 1-2 of them. The benchmark measures **session-
level recall@k** — whether at least one answer-session appears in the
top-k retrieval results.

## Prerequisites

```bash
# System
Python 3.11+
git
~/sage-memory cloned

# Disk
~300 MB for the dataset
~500 MB for output JSONLs across all bench runs

# Optional (for hosted-vector benchmark only)
OPENAI_API_KEY  # text-embedding-3-small, $0.02/1M tokens
VOYAGE_API_KEY  # voyage-3-lite, free tier available
COHERE_API_KEY  # embed-english-v3.0
```

## Step 1 — Install sage-memory

```bash
git clone https://github.com/xoai/sage-memory.git
cd sage-memory

# Recommended: uv + isolated venv
uv venv ~/.venvs/sage-memory
source ~/.venvs/sage-memory/bin/activate
uv pip install -e .[neural,dev]

# Or with pip
python -m venv .venv && source .venv/bin/activate
pip install -e .[neural,dev]
```

Verify:
```bash
sage-memory --help
pytest tests/ -q  # should be ~339 passed
```

## Step 2 — Download the LongMemEval-S dataset

```bash
mkdir -p evaluation/longmemeval/data
cd evaluation/longmemeval/data

# Cleaned variant (smaller, faster, same questions)
curl -L \
  https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json \
  -o longmemeval_s_cleaned.json

# Verify checksum (matches baselines)
sha256sum longmemeval_s_cleaned.json
# Expected: d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442

cd ../../..
```

The dataset is ~270 MB. Already gitignored.

## Step 3 — Free-path baseline (FTS5 only, zero API cost)

```bash
cd evaluation/longmemeval

# Three modes, all with LLM keys scrubbed
for mode in bm25 bm25-full hybrid-temporal; do
  env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY \
      -u VOYAGE_API_KEY -u COHERE_API_KEY \
    python bench_longmemeval.py data/longmemeval_s_cleaned.json \
      --mode "$mode" \
      --out "results/freepath_${mode}.jsonl"
done
```

**Expected results** (session-level R@5 on all 500 questions):

| Mode | R@1 | R@3 | R@5 | R@10 | Wall time |
|---|:---:|:---:|:---:|:---:|:---:|
| bm25 | 0.856 | 0.946 | 0.958 | 0.978 | ~35s |
| bm25-full | 0.834 | 0.952 | 0.972 | 0.986 | ~80s |
| hybrid-temporal | 0.856 | 0.946 | 0.958 | 0.978 | ~35s |

**`bm25-full` is the best free-path config.** It searches both user
and assistant turns.

## Step 4 — Hosted-vector benchmark (optional, ~$0.50)

This step measures the lift from adding OpenAI's `text-embedding-3-small`
as a vector channel on top of FTS5+RRF.

```bash
# Set ONE hosted-embedder API key
export OPENAI_API_KEY=sk-proj-...

# Full 500q (~3-4 hours, ~$0.50)
python bench_hosted.py bm25-full

# Or just the hardest type for a quick stress test (~17 min, ~$0.05)
python bench_hosted.py bm25-full 999 single-session-preference
```

**Expected results** (session-level on first 50q + preference subset):

| | sage FTS-only | + OpenAI 1536d | Δ |
|---|:---:|:---:|:---:|
| **Easy** (first 50q, all single-session-user) | R@5=0.960 | R@5=0.980 | +2pp |
| **Hard** (n=30, single-session-preference) | R@5=0.867 | R@5=0.900 | +3.3pp |
| **Hard** R@1 | 0.433 | **0.667** | **+23.4pp** 🚀 |

## Step 5 — Compare against published systems

Putting it together (single-session R@5 on 500q):

| System | R@5 | Embedder | LLM-in-retrieval | Cost / 1000Q |
|---|:---:|---|:---:|:---:|
| MemPalace hybrid+rerank | 0.984 | OpenAI 3-large | Yes | — |
| **sage + hosted vector (proj.)** | **~0.985** | OpenAI 3-small 1536d | No | ~$0.50 |
| gbrain-hybrid | 0.976 | OpenAI 3-large 1536d | No | ~$0.50 |
| gbrain-vector | 0.974 | OpenAI 3-large 1536d | No | ~$0.50 |
| **sage free-path** | **0.972** | none (LocalEmbedder, 384d) | No | **$0** |
| MemPalace raw | 0.966 | — | No | — |
| BM25 baseline (academic) | ~0.70 | — | No | $0 |
| gbrain-keyword | 0.198 | none (Postgres FTS) | No | $0 |

## Reproducibility floor

| Field | Value |
|---|---|
| Dataset SHA-256 | `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442` |
| Dataset path | `evaluation/longmemeval/data/longmemeval_s_cleaned.json` |
| Free-path embedder | `LocalEmbedder` (dim=384, quality=0.45; below vector threshold → FTS5 only) |
| Hosted embedder | `OpenAIEmbedder` (text-embedding-3-small, dim=1536) — bench_hosted.py only |
| Python | 3.11+ |
| sage-memory version | 0.6.0 |

### Determinism

The free-path harness is **deterministic at the metric level**:
- No random sources in the metric computation path
- `tempfile.mkdtemp()` randomness affects only per-question DB paths,
  which don't appear in `corpus_id`, `recall_any@k`, `recall_all@k`, or
  `ndcg_any@k`
- Two back-to-back runs produce byte-identical JSONLs (verified via
  `cmp` for the M4→M5 regression gate)

The hosted harness has **OpenAI-side variance** (embedding values can
shift slightly across API calls). Metrics typically agree within
±0.005 R@5 across runs.

## Troubleshooting

**"Dimension mismatch for query vector"**
You're running `bench_longmemeval.py` (which uses 384d vec tables)
while the embedder produces 1536d vectors. Use `bench_hosted.py`
instead, which recreates the vec tables at the right dim.

**"DimMismatchRefuseError"**
The corpus has `vec_dim` set to a value no available embedder
matches. Either configure the API key for the matching embedder,
or run `sage-memory reindex --re-embed --embedder <name>` to
relocate the corpus.

**Bench appears stuck**
`| tail -N` filters buffer the upstream's stdout until the upstream
exits. Use `python -u` and `tee` to see live progress:
```bash
python -u bench_longmemeval.py ... 2>&1 | tee /tmp/bench.log
```

**Connection errors mid-run**
The hosted-bench harness uses synchronous OpenAI calls. Rate limits
or transient network failures trigger the 3-retry backoff in
`embedder.py`. If you see repeated 429s, slow down by setting
`OPENAI_REQUEST_DELAY_S` (not yet supported — issue for v0.8).

## Output format

Each per-question line in the output JSONL:
```jsonl
{
  "question_id": "abc123",
  "question_type": "single-session-user",
  "question": "What did I tell you about my cat?",
  "answer": "Your cat is named Whiskers and is a Persian.",
  "retrieval_results": {
    "query": "What did I tell you about my cat?",
    "ranked_items": [...top-50 corpus_id+text...],
    "metrics": {
      "session": {"recall_any@1": 1, "recall_any@3": 1, "recall_any@5": 1, "recall_any@10": 1, "ndcg_any@10": 1.0},
      "turn":    {"recall_any@1": 0, "recall_any@3": 1, "recall_any@5": 1, "recall_any@10": 1, "ndcg_any@10": 0.79}
    }
  }
}
```

Compatible with [LongMemEval's official evaluator](https://github.com/xiaowu0162/longmemeval).

## Where to file issues

If your reproduction differs by more than ±0.01 R@5 on the free path,
please file at https://github.com/xoai/sage-memory/issues with:
- Output of `git rev-parse HEAD`
- Python version + `pip freeze | grep -E "sage|fastembed|sqlite-vec"`
- The first failing question's `question_id`
- The contents of `results/<your-output>.jsonl` for that question
