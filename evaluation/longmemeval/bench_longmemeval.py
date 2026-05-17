#!/usr/bin/env python3
"""
sage-memory × LongMemEval Benchmark
=====================================

Evaluates sage-memory's retrieval against the LongMemEval benchmark.
Direct comparison with MemPal's ChromaDB-based retrieval.

For each of the 500 questions:
1. Ingest all haystack sessions into a fresh sage-memory database
2. Search with the question using FTS5 BM25 OR semantics
3. Score retrieval against ground-truth answer sessions

Outputs:
- Recall@k and NDCG@k at session and turn level
- Per-question-type breakdown
- JSONL log compatible with LongMemEval's evaluation scripts

Modes:
    bm25          — FTS5 BM25 OR on user turns (default, sage-memory's strength)
    bm25-full     — BM25 on both user + assistant turns
    hybrid        — BM25 + keyword overlap re-ranking
    hybrid-temporal — hybrid + date proximity boost for temporal questions

Usage:
    python bench_longmemeval.py data/longmemeval_s_cleaned.json
    python bench_longmemeval.py data/longmemeval_s_cleaned.json --mode hybrid-temporal
    python bench_longmemeval.py data/longmemeval_s_cleaned.json --limit 20
    python bench_longmemeval.py data/longmemeval_oracle.json --mode bm25-full
"""

import os
import sys
import re
import json
import argparse
import math
import time
import shutil
import tempfile
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta

# Add sage-memory to path
_SAGE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_SAGE_ROOT / "src"))
os.environ["MCP_MEMORY_EMBEDDER"] = "local"

from sage_memory.db import override_project_root, close_all
from sage_memory.store import store as sage_store
from sage_memory.search import search as sage_search


# M3b (T4): channel-ablation harness. Set by --channels-disable CLI
# flag; threaded to every sage_search() call as the `channels=` kwarg.
# None = all available (default); list = explicit subset.
_ACTIVE_CHANNELS: list[str] | None = None
_ALL_CHANNELS = ["bm25", "vector", "graph"]


# M4 (T6): expand/rerank ablation. When True, the corresponding flag
# is forced to False on every sage_search() call (overrides config).
# False (default) preserves the M3b call shape (omits the kwarg,
# letting search() use the 3-state matrix default — None = consult
# LLM-key availability).
_EXPAND_DISABLE: bool = False
_RERANK_DISABLE: bool = False


def _channels_kwarg():
    """Returns the `channels=` kwarg value for sage_search calls.
    None preserves M2 / pre-M3b call shape; list activates the M3b
    explicit-channels override."""
    return _ACTIVE_CHANNELS


def _expand_rerank_kwargs() -> dict:
    """M4 (T6): returns kwargs dict for the expand/rerank params.

    Only includes a key when its disable flag is set — preserves M3b
    byte-identity on the default path (no flags → empty dict → no
    kwarg → same call shape).
    """
    kw = {}
    if _EXPAND_DISABLE:
        kw["expand"] = False
    if _RERANK_DISABLE:
        kw["rerank"] = False
    return kw


# M3b focused-bench: when True, spawn+drain a worker per question
# after ingest so M3a's entity extraction populates the graph before
# search runs. Costs ~50 LLM calls per question (one per session).
_POPULATE_GRAPH: bool = False


def _drain_worker_inline():
    """Spawn the M3a worker against the current project DB, drain
    the extraction queue, stop. Bench-only helper; not part of the
    sage-memory production API."""
    from sage_memory.db import get_project_db_path, _active_project
    from sage_memory.worker import Worker

    # `_active_project` was set by `override_project_root()` in _fresh_db
    if _active_project is None:
        return
    db_path = str(get_project_db_path(_active_project))
    w = Worker(db_path, poll_interval_ms=100, shutdown_timeout_s=60.0)
    w.start()
    # Each question's haystack has ~50 sessions → ~50 extract calls.
    # Haiku takes ~1-2s per call; budget 180s with margin.
    w._wait_for_queue_empty(timeout_s=180.0)
    w.stop()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# METRICS (identical to MemPal benchmark for fair comparison)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def dcg(relevances, k):
    score = 0.0
    for i, rel in enumerate(relevances[:k]):
        score += rel / math.log2(i + 2)
    return score


def ndcg(rankings, correct_ids, corpus_ids, k):
    relevances = [1.0 if corpus_ids[idx] in correct_ids else 0.0 for idx in rankings[:k]]
    ideal = sorted(relevances, reverse=True)
    idcg = dcg(ideal, k)
    if idcg == 0:
        return 0.0
    return dcg(relevances, k) / idcg


def evaluate_retrieval(rankings, correct_ids, corpus_ids, k):
    top_k_ids = set(corpus_ids[idx] for idx in rankings[:k])
    recall_any = float(any(cid in top_k_ids for cid in correct_ids))
    recall_all = float(all(cid in top_k_ids for cid in correct_ids))
    ndcg_score = ndcg(rankings, correct_ids, corpus_ids, k)
    return recall_any, recall_all, ndcg_score


def session_id_from_corpus_id(corpus_id):
    if "_turn_" in corpus_id:
        return corpus_id.rsplit("_turn_", 1)[0]
    return corpus_id


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SHARED TEMP DIR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TMPDIR = Path(tempfile.mkdtemp())


def _fresh_db(name="q"):
    """Create a fresh sage-memory project database."""
    close_all()
    proj = _TMPDIR / name
    if proj.exists():
        shutil.rmtree(proj)
    proj.mkdir(parents=True)
    (proj / ".git").mkdir()
    override_project_root(proj)
    return proj


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SAGE-MEMORY RETRIEVER MODES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STOP_WORDS = frozenset(
    "what when where who how which did do was were have has had is are the a an "
    "my me i you your their it its in on at to for of with by from ago last that "
    "this there about get got give gave buy bought made make".split()
)


def extract_keywords(text):
    words = re.findall(r"\b[a-z]{3,}\b", text.lower())
    return [w for w in words if w not in STOP_WORDS]


def keyword_overlap(query_kws, doc_text):
    doc_lower = doc_text.lower()
    if not query_kws:
        return 0.0
    hits = sum(1 for kw in query_kws if kw in doc_lower)
    return hits / len(query_kws)


def parse_question_date(date_str):
    try:
        return datetime.strptime(date_str.split(" (")[0], "%Y/%m/%d")
    except Exception:
        return None


def parse_time_offset_days(question):
    q = question.lower()
    patterns = [
        (r"(\d+)\s+days?\s+ago", lambda m: (int(m.group(1)), 2)),
        (r"a\s+couple\s+(?:of\s+)?days?\s+ago", lambda m: (2, 2)),
        (r"yesterday", lambda m: (1, 1)),
        (r"a\s+week\s+ago", lambda m: (7, 3)),
        (r"(\d+)\s+weeks?\s+ago", lambda m: (int(m.group(1)) * 7, 5)),
        (r"last\s+week", lambda m: (7, 3)),
        (r"a\s+month\s+ago", lambda m: (30, 7)),
        (r"(\d+)\s+months?\s+ago", lambda m: (int(m.group(1)) * 30, 10)),
        (r"last\s+month", lambda m: (30, 7)),
        (r"last\s+year", lambda m: (365, 30)),
        (r"a\s+year\s+ago", lambda m: (365, 30)),
        (r"recently", lambda m: (14, 14)),
    ]
    for pattern, extractor in patterns:
        m = re.search(pattern, q)
        if m:
            return extractor(m)
    return None


def _ingest_and_search(entry, granularity, n_results, include_assistant=False):
    """
    Core ingest+search using sage-memory. Returns the standard interface:
    (rankings, corpus, corpus_ids, corpus_timestamps)

    Stores each session/turn via sage_memory_store, then searches
    via sage_memory_search with BM25 OR semantics.
    """
    sessions = entry["haystack_sessions"]
    session_ids = entry["haystack_session_ids"]
    dates = entry["haystack_dates"]

    corpus = []
    corpus_ids = []
    corpus_timestamps = []

    # Build corpus and ingest into sage-memory
    for session, sess_id, date in zip(sessions, session_ids, dates):
        if granularity == "session":
            if include_assistant:
                turns = [t["content"] for t in session]
            else:
                turns = [t["content"] for t in session if t["role"] == "user"]
            if turns:
                doc = "\n".join(turns)
                corpus.append(doc)
                corpus_ids.append(sess_id)
                corpus_timestamps.append(date)

                # Store in sage-memory with session ID in title for mapping
                sage_store(
                    content=doc,
                    title=f"[{sess_id}] {doc[:80]}",
                    tags=["session", f"sid:{sess_id}", f"date:{date}"],
                    scope="project",
                )
        else:  # turn granularity
            turn_num = 0
            for turn in session:
                if include_assistant or turn["role"] == "user":
                    corpus.append(turn["content"])
                    turn_id = f"{sess_id}_turn_{turn_num}"
                    corpus_ids.append(turn_id)
                    corpus_timestamps.append(date)

                    sage_store(
                        content=turn["content"],
                        title=f"[{turn_id}] {turn['content'][:80]}",
                        tags=["turn", f"sid:{sess_id}", f"tid:{turn_id}", f"date:{date}"],
                        scope="project",
                    )
                    turn_num += 1

    if not corpus:
        return [], corpus, corpus_ids, corpus_timestamps

    # M3b (focused-bench): optionally drain the M3a worker before
    # searching so the graph channel has populated entities to work
    # with. Gated by --populate-graph flag (sets _POPULATE_GRAPH).
    if _POPULATE_GRAPH:
        _drain_worker_inline()

    # Search with sage-memory
    query = entry["question"]
    r = sage_search(query=query, limit=min(n_results, len(corpus)), channels=_channels_kwarg(), **_expand_rerank_kwargs())

    # M4 (T5) — A10 bench-mode strip. The new `timings` field on
    # search()'s return is for runtime debugging via MCP; bench
    # JSONLs must remain byte-identical to M3b baselines on the
    # free path. Strip BEFORE any downstream consumer touches `r`.
    r.pop("timings", None)

    # Map results back to corpus indices via session/turn ID in title
    id_to_idx = {}
    for i, cid in enumerate(corpus_ids):
        id_to_idx[cid] = i

    ranked_indices = []
    seen = set()
    for result in r.get("results", []):
        title = result.get("title", "")
        # Extract the ID from [sess_id] or [turn_id] prefix
        m = re.match(r"\[([^\]]+)\]", title)
        if m:
            found_id = m.group(1)
            if found_id in id_to_idx and found_id not in seen:
                ranked_indices.append(id_to_idx[found_id])
                seen.add(found_id)

    # Append any un-retrieved items at the end
    for i in range(len(corpus)):
        if i not in seen and corpus_ids[i] not in seen:
            ranked_indices.append(i)
            seen.add(corpus_ids[i])

    return ranked_indices, corpus, corpus_ids, corpus_timestamps


def retrieve_bm25(entry, granularity="session", n_results=50):
    """BM25 mode: FTS5 OR semantics on user turns only."""
    _fresh_db(f"q_{entry['question_id']}")
    return _ingest_and_search(entry, granularity, n_results, include_assistant=False)


def retrieve_bm25_full(entry, granularity="session", n_results=50):
    """BM25-full mode: FTS5 OR on both user + assistant turns."""
    _fresh_db(f"q_{entry['question_id']}")
    return _ingest_and_search(entry, granularity, n_results, include_assistant=True)


def retrieve_hybrid(entry, granularity="session", n_results=50, hybrid_weight=0.30):
    """
    Hybrid mode: sage-memory BM25 search + keyword overlap re-ranking.

    sage-memory already uses BM25 OR which is strong. The hybrid layer
    adds a keyword overlap score that catches cases where a specific
    keyword (person name, product name) is in the document but BM25
    doesn't rank it high enough.
    """
    _fresh_db(f"q_{entry['question_id']}")

    sessions = entry["haystack_sessions"]
    session_ids = entry["haystack_session_ids"]
    dates = entry["haystack_dates"]
    question = entry["question"]

    corpus = []
    corpus_ids = []
    corpus_timestamps = []

    for session, sess_id, date in zip(sessions, session_ids, dates):
        if granularity == "session":
            user_turns = [t["content"] for t in session if t["role"] == "user"]
            if user_turns:
                doc = "\n".join(user_turns)
                corpus.append(doc)
                corpus_ids.append(sess_id)
                corpus_timestamps.append(date)
                sage_store(
                    content=doc,
                    title=f"[{sess_id}] {doc[:80]}",
                    tags=["session", f"sid:{sess_id}", f"date:{date}"],
                    scope="project",
                )
        else:
            turn_num = 0
            for turn in session:
                if turn["role"] == "user":
                    corpus.append(turn["content"])
                    turn_id = f"{sess_id}_turn_{turn_num}"
                    corpus_ids.append(turn_id)
                    corpus_timestamps.append(date)
                    sage_store(
                        content=turn["content"],
                        title=f"[{turn_id}] {turn['content'][:80]}",
                        tags=["turn", f"sid:{sess_id}", f"tid:{turn_id}", f"date:{date}"],
                        scope="project",
                    )
                    turn_num += 1

    if not corpus:
        return [], corpus, corpus_ids, corpus_timestamps

    # Search with sage-memory (get more results for re-ranking)
    r = sage_search(query=question, limit=min(n_results, len(corpus)), channels=_channels_kwarg(), **_expand_rerank_kwargs())
    r.pop("timings", None)  # M4 (T5) A10 strip

    id_to_idx = {cid: i for i, cid in enumerate(corpus_ids)}
    query_keywords = extract_keywords(question)

    # Score results: BM25 rank position + keyword overlap
    scored = []
    for rank, result in enumerate(r.get("results", [])):
        title = result.get("title", "")
        m = re.match(r"\[([^\]]+)\]", title)
        if not m:
            continue
        found_id = m.group(1)
        if found_id not in id_to_idx:
            continue
        idx = id_to_idx[found_id]

        # BM25 rank as distance proxy (lower rank = better)
        bm25_dist = rank / max(len(r["results"]), 1)

        # Keyword overlap on original corpus text
        overlap = keyword_overlap(query_keywords, corpus[idx])
        fused = bm25_dist * (1.0 - hybrid_weight * overlap)
        scored.append((idx, fused, found_id))

    scored.sort(key=lambda x: x[1])

    ranked_indices = []
    seen = set()
    for idx, _, cid in scored:
        if cid not in seen:
            ranked_indices.append(idx)
            seen.add(cid)

    for i in range(len(corpus)):
        if corpus_ids[i] not in seen:
            ranked_indices.append(i)
            seen.add(corpus_ids[i])

    return ranked_indices, corpus, corpus_ids, corpus_timestamps


def retrieve_hybrid_temporal(entry, granularity="session", n_results=50, hybrid_weight=0.30):
    """
    Hybrid-temporal mode: BM25 + keyword overlap + date proximity boost.

    Parses relative time expressions from the question ("a week ago",
    "10 days ago") and boosts sessions whose date falls near the target.
    """
    _fresh_db(f"q_{entry['question_id']}")

    sessions = entry["haystack_sessions"]
    session_ids = entry["haystack_session_ids"]
    dates = entry["haystack_dates"]
    question = entry["question"]
    question_date = parse_question_date(entry.get("question_date", ""))

    corpus = []
    corpus_ids = []
    corpus_timestamps = []

    for session, sess_id, date in zip(sessions, session_ids, dates):
        if granularity == "session":
            user_turns = [t["content"] for t in session if t["role"] == "user"]
            if user_turns:
                doc = "\n".join(user_turns)
                corpus.append(doc)
                corpus_ids.append(sess_id)
                corpus_timestamps.append(date)
                sage_store(
                    content=doc,
                    title=f"[{sess_id}] {doc[:80]}",
                    tags=["session", f"sid:{sess_id}", f"date:{date}"],
                    scope="project",
                )
        else:
            turn_num = 0
            for turn in session:
                if turn["role"] == "user":
                    corpus.append(turn["content"])
                    turn_id = f"{sess_id}_turn_{turn_num}"
                    corpus_ids.append(turn_id)
                    corpus_timestamps.append(date)
                    sage_store(
                        content=turn["content"],
                        title=f"[{turn_id}] {turn['content'][:80]}",
                        tags=["turn", f"sid:{sess_id}", f"tid:{turn_id}", f"date:{date}"],
                        scope="project",
                    )
                    turn_num += 1

    if not corpus:
        return [], corpus, corpus_ids, corpus_timestamps

    r = sage_search(query=question, limit=min(n_results, len(corpus)), channels=_channels_kwarg(), **_expand_rerank_kwargs())
    r.pop("timings", None)  # M4 (T5) A10 strip

    id_to_idx = {cid: i for i, cid in enumerate(corpus_ids)}
    query_keywords = extract_keywords(question)

    # Temporal setup
    time_offset = parse_time_offset_days(question)
    target_date = None
    if time_offset and question_date:
        days_back, tolerance = time_offset
        target_date = question_date - timedelta(days=days_back)

    scored = []
    for rank, result in enumerate(r.get("results", [])):
        title = result.get("title", "")
        m = re.match(r"\[([^\]]+)\]", title)
        if not m:
            continue
        found_id = m.group(1)
        if found_id not in id_to_idx:
            continue
        idx = id_to_idx[found_id]

        bm25_dist = rank / max(len(r["results"]), 1)
        overlap = keyword_overlap(query_keywords, corpus[idx])
        fused = bm25_dist * (1.0 - hybrid_weight * overlap)

        # Temporal proximity boost
        if target_date:
            sess_date = parse_question_date(corpus_timestamps[idx])
            if sess_date:
                delta_days = abs((sess_date - target_date).days)
                tol = time_offset[1]
                if delta_days <= tol:
                    fused *= 0.60  # 40% boost
                elif delta_days <= tol * 3:
                    boost = 0.40 * (1.0 - (delta_days - tol) / (tol * 2))
                    fused *= (1.0 - boost)

        scored.append((idx, fused, found_id))

    scored.sort(key=lambda x: x[1])

    ranked_indices = []
    seen = set()
    for idx, _, cid in scored:
        if cid not in seen:
            ranked_indices.append(idx)
            seen.add(cid)

    for i in range(len(corpus)):
        if corpus_ids[i] not in seen:
            ranked_indices.append(i)
            seen.add(corpus_ids[i])

    return ranked_indices, corpus, corpus_ids, corpus_timestamps


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MODE DISPATCH
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MODES = {
    "bm25": retrieve_bm25,
    "bm25-full": retrieve_bm25_full,
    "hybrid": retrieve_hybrid,
    "hybrid-temporal": retrieve_hybrid_temporal,
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BENCHMARK RUNNER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def run_benchmark(data_file, granularity="session", limit=0, out_file=None,
                  mode="bm25", hybrid_weight=0.30,
                  question_type=None):
    with open(data_file) as f:
        data = json.load(f)

    if question_type:
        data = [q for q in data if q.get("question_type") == question_type]

    if limit > 0:
        data = data[:limit]

    retrieve_fn = MODES[mode]

    print(f"\n{'=' * 60}")
    print(f"  sage-memory × LongMemEval Benchmark")
    print(f"{'=' * 60}")
    print(f"  Data:        {Path(data_file).name}")
    print(f"  Questions:   {len(data)}")
    print(f"  Granularity: {granularity}")
    print(f"  Mode:        {mode}")
    print(f"  Engine:      FTS5 BM25 OR semantics (sage-memory v0.5)")
    print(f"{'─' * 60}\n")

    ks = [1, 3, 5, 10, 30, 50]
    metrics_session = defaultdict(list)
    metrics_turn = defaultdict(list)
    per_type = defaultdict(lambda: defaultdict(list))
    results_log = []
    start_time = datetime.now()

    for i, entry in enumerate(data):
        qid = entry["question_id"]
        qtype = entry["question_type"]
        question = entry["question"]
        answer_sids = set(entry["answer_session_ids"])

        # Run retrieval
        if mode in ("hybrid", "hybrid-temporal"):
            rankings, corpus, corpus_ids, corpus_timestamps = retrieve_fn(
                entry, granularity=granularity, n_results=50, hybrid_weight=hybrid_weight)
        else:
            rankings, corpus, corpus_ids, corpus_timestamps = retrieve_fn(
                entry, granularity=granularity, n_results=50)

        if not rankings:
            print(f"  [{i+1:4}/{len(data)}] {qid[:30]:30} SKIP (empty)")
            continue

        # Session-level IDs
        session_level_ids = [session_id_from_corpus_id(cid) for cid in corpus_ids]
        session_correct = answer_sids

        # Turn-level correct
        turn_correct = set()
        for cid in corpus_ids:
            if session_id_from_corpus_id(cid) in answer_sids:
                turn_correct.add(cid)

        entry_metrics = {"session": {}, "turn": {}}

        for k in ks:
            ra, rl, nd = evaluate_retrieval(rankings, session_correct, session_level_ids, k)
            metrics_session[f"recall_any@{k}"].append(ra)
            metrics_session[f"recall_all@{k}"].append(rl)
            metrics_session[f"ndcg_any@{k}"].append(nd)
            entry_metrics["session"][f"recall_any@{k}"] = ra

            ra_t, rl_t, nd_t = evaluate_retrieval(rankings, turn_correct, corpus_ids, k)
            metrics_turn[f"recall_any@{k}"].append(ra_t)
            metrics_turn[f"recall_all@{k}"].append(rl_t)
            metrics_turn[f"ndcg_any@{k}"].append(nd_t)

        per_type[qtype]["recall_any@5"].append(metrics_session["recall_any@5"][-1])
        per_type[qtype]["recall_any@10"].append(metrics_session["recall_any@10"][-1])
        per_type[qtype]["ndcg_any@10"].append(metrics_session["ndcg_any@10"][-1])

        # Log
        ranked_items = []
        for idx in rankings[:50]:
            if idx < len(corpus):
                ranked_items.append({
                    "corpus_id": corpus_ids[idx],
                    "text": corpus[idx][:500],
                    "timestamp": corpus_timestamps[idx],
                })

        results_log.append({
            "question_id": qid,
            "question_type": qtype,
            "question": question,
            "answer": entry["answer"],
            "retrieval_results": {
                "query": question,
                "ranked_items": ranked_items,
                "metrics": entry_metrics,
            },
        })

        r5 = metrics_session["recall_any@5"][-1]
        r10 = metrics_session["recall_any@10"][-1]
        status = "HIT" if r5 > 0 else ("hit@10" if r10 > 0 else "miss")
        print(f"  [{i+1:4}/{len(data)}] {qid[:30]:30} R@5={r5:.0f} R@10={r10:.0f}  {status}")

        # Cleanup per-question DB
        close_all()
        proj = _TMPDIR / f"q_{qid}"
        if proj.exists():
            shutil.rmtree(proj, ignore_errors=True)

    elapsed = (datetime.now() - start_time).total_seconds()

    # ── Print results ─────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"  RESULTS — sage-memory ({mode} mode, {granularity} granularity)")
    print(f"{'=' * 60}")
    print(f"  Time: {elapsed:.1f}s ({elapsed/max(len(data),1):.2f}s per question)\n")

    print("  SESSION-LEVEL METRICS:")
    for k in ks:
        vals = metrics_session[f"recall_any@{k}"]
        if vals:
            ra = sum(vals) / len(vals)
            nd = sum(metrics_session[f"ndcg_any@{k}"]) / len(vals)
            print(f"    Recall@{k:2}: {ra:.3f}    NDCG@{k:2}: {nd:.3f}")

    print("\n  TURN-LEVEL METRICS:")
    for k in ks:
        vals = metrics_turn[f"recall_any@{k}"]
        if vals:
            ra = sum(vals) / len(vals)
            nd = sum(metrics_turn[f"ndcg_any@{k}"]) / len(vals)
            print(f"    Recall@{k:2}: {ra:.3f}    NDCG@{k:2}: {nd:.3f}")

    print("\n  PER-TYPE BREAKDOWN (session recall_any@10):")
    for qtype, vals in sorted(per_type.items()):
        r10 = sum(vals["recall_any@10"]) / len(vals["recall_any@10"])
        n = len(vals["recall_any@10"])
        print(f"    {qtype:35} R@10={r10:.3f}  (n={n})")

    print(f"\n{'=' * 60}\n")

    if out_file:
        Path(out_file).parent.mkdir(parents=True, exist_ok=True)
        with open(out_file, "w") as f:
            for entry in results_log:
                f.write(json.dumps(entry) + "\n")
        print(f"  Results saved to: {out_file}")

    # Cleanup
    shutil.rmtree(_TMPDIR, ignore_errors=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="sage-memory × LongMemEval Benchmark")
    parser.add_argument("data_file", help="Path to longmemeval JSON")
    parser.add_argument("--granularity", choices=["session", "turn"], default="session")
    parser.add_argument("--limit", type=int, default=0, help="Limit to N questions (0=all)")
    parser.add_argument("--mode", choices=list(MODES.keys()), default="bm25",
                        help="Retrieval mode (default: bm25)")
    parser.add_argument("--out", default=None, help="Output JSONL file")
    parser.add_argument("--hybrid-weight", type=float, default=0.30,
                        help="Keyword overlap weight for hybrid modes (default: 0.30)")
    parser.add_argument(
        "--channels-disable", default="",
        help=(
            "M3b (T4) ablation harness: comma-separated channels to "
            "disable (subset of bm25,vector,graph). E.g., "
            "--channels-disable=graph runs 2-channel ablation; "
            "--channels-disable=vector,graph runs BM25-only. Empty "
            "(default) runs the full 3-channel retrieval."
        ),
    )
    parser.add_argument(
        "--populate-graph", action="store_true",
        help=(
            "M3b focused-bench: spawn+drain the M3a worker after "
            "each question's ingest so entity extraction populates "
            "the graph BEFORE search. Requires ANTHROPIC_API_KEY or "
            "OPENAI_API_KEY. Adds ~50 LLM calls per question."
        ),
    )
    parser.add_argument(
        "--expand-disable", action="store_true",
        help=(
            "M4 (T6) ablation: force expand=False on every search call "
            "regardless of LLM-key availability. Used for 3-way "
            "ablation runs (all_on / expand_off / rerank_off)."
        ),
    )
    parser.add_argument(
        "--rerank-disable", action="store_true",
        help=(
            "M4 (T6) ablation: force rerank=False on every search "
            "call. Used for 3-way ablation runs alongside "
            "--expand-disable."
        ),
    )
    parser.add_argument(
        "--question-type", default=None,
        help=(
            "Filter dataset to a single question_type "
            "(multi-session | temporal-reasoning | knowledge-update | "
            "single-session-user | single-session-assistant | "
            "single-session-preference). Useful for focused ablation "
            "on the subset where graph channel should matter most."
        ),
    )
    args = parser.parse_args()

    if not args.out:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        args.out = f"results/sage_{args.mode}_{args.granularity}_{ts}.jsonl"

    # M3b (T4): translate --channels-disable into the _ACTIVE_CHANNELS
    # global. Empty flag → None (preserves M2 / pre-M3b call shape).
    # Non-empty → explicit list of channels to KEEP.
    if args.channels_disable:
        disabled = {c.strip() for c in args.channels_disable.split(",")
                    if c.strip()}
        unknown = disabled - set(_ALL_CHANNELS)
        if unknown:
            parser.error(
                f"--channels-disable: unknown channel(s) {unknown!r}; "
                f"valid values are {_ALL_CHANNELS!r}"
            )
        kept = [c for c in _ALL_CHANNELS if c not in disabled]
        _ACTIVE_CHANNELS = kept  # noqa: F841 — module-global mutation
        # Promote the local rebinding to the module level
        globals()["_ACTIVE_CHANNELS"] = kept

    if args.populate_graph:
        globals()["_POPULATE_GRAPH"] = True

    # M4 (T6): expand/rerank ablation flags. Mutate module globals
    # so the search-call helper picks them up. Empty (default) → False
    # → kwargs dict empty → search() call shape unchanged from M3b.
    if args.expand_disable:
        globals()["_EXPAND_DISABLE"] = True
    if args.rerank_disable:
        globals()["_RERANK_DISABLE"] = True

    run_benchmark(
        args.data_file,
        args.granularity,
        args.limit,
        args.out,
        args.mode,
        args.hybrid_weight,
        question_type=args.question_type,
    )
