#!/usr/bin/env python3
"""LongMemEval benchmark integration for sage-memory.

Pipeline: ingest chat history → store in sage-memory → search → generate answer → output for evaluation.

Two ingestion modes:
  --mode raw       Store each session as-is (fast, no LLM calls for ingestion)
  --mode extract   Use LLM to extract key facts from sessions (better recall, slower)

Two LLM backends:
  --llm anthropic  Use Claude via Anthropic API (default)
  --llm openai     Use GPT via OpenAI API

Usage:
  # Step 1: Run the benchmark
  python run_longmemeval.py \
    --data_file data/longmemeval_oracle.json \
    --mode raw \
    --llm anthropic \
    --output results/sage_memory_oracle.jsonl

  # Step 2: Evaluate (requires OpenAI API for GPT-4o judge, or use Claude judge below)
  python evaluate_with_claude.py \
    --hyp_file results/sage_memory_oracle.jsonl \
    --ref_file data/longmemeval_oracle.json \
    --output results/sage_memory_oracle_eval.jsonl

Prerequisites:
  pip install sage-memory
  export ANTHROPIC_API_KEY=sk-...   (for --llm anthropic)
  export OPENAI_API_KEY=sk-...      (for --llm openai or evaluation)

  Download data:
    cd data/
    wget https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json
    wget https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
"""

import argparse
import json
import os
import sys
import time
import shutil
import tempfile
from pathlib import Path

# Add sage-memory to path
SAGE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SAGE_DIR / "src"))
os.environ["MCP_MEMORY_EMBEDDER"] = "local"

from sage_memory.db import override_project_root, close_all
from sage_memory.store import store, list_memories
from sage_memory.search import search


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LLM clients
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def call_anthropic(system: str, prompt: str, max_tokens: int = 1024) -> str:
    import urllib.request
    body = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data["content"][0]["text"]


def call_openai(system: str, prompt: str, max_tokens: int = 1024) -> str:
    import urllib.request
    body = json.dumps({
        "model": "gpt-4o-mini",
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Ingestion: chat sessions → sage-memory
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def format_session(session_turns: list[dict], date: str) -> str:
    """Format a chat session into a single text block."""
    lines = [f"[Date: {date}]"]
    for turn in session_turns:
        role = turn["role"].capitalize()
        lines.append(f"{role}: {turn['content']}")
    return "\n".join(lines)


def ingest_raw(sessions: list, dates: list, session_ids: list):
    """Store each session as a single memory entry."""
    for session, date, sid in zip(sessions, dates, session_ids):
        text = format_session(session, date)

        # Extract a title from first user message
        user_msgs = [t["content"] for t in session if t["role"] == "user"]
        title = user_msgs[0][:100] if user_msgs else f"Session {sid}"

        # Tags from date and session ID
        tags = ["chat-history", f"date:{date}", f"session:{sid}"]

        store(content=text, title=title, tags=tags, scope="project")


def ingest_extract(sessions: list, dates: list, session_ids: list, call_llm):
    """Extract key facts from each session using LLM, then store."""
    system = (
        "Extract the key facts, preferences, events, and decisions from this chat session. "
        "Output a bulleted list of factual statements. Each fact should be self-contained "
        "and include the date if time-relevant. Be specific — include names, places, numbers."
    )

    for session, date, sid in zip(sessions, dates, session_ids):
        text = format_session(session, date)

        # Extract facts via LLM
        try:
            facts = call_llm(system, f"Session date: {date}\n\n{text}", max_tokens=512)
        except Exception as e:
            print(f"  Warning: extraction failed for {sid}: {e}")
            facts = text  # fallback to raw

        title = f"Facts from session {sid} ({date})"
        tags = ["chat-history", "extracted", f"date:{date}", f"session:{sid}"]

        store(content=facts, title=title, tags=tags, scope="project")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Retrieval + Generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def answer_question(question: str, question_date: str, call_llm,
                    topk: int = 10) -> dict:
    """Search sage-memory and generate an answer."""
    t0 = time.perf_counter()

    # Search for relevant memories
    results = search(query=question, limit=topk)
    search_time = (time.perf_counter() - t0) * 1000

    # Build context from retrieved memories
    context_parts = []
    for r in results["results"]:
        context_parts.append(f"[{r.get('title', 'Memory')}]\n{r['content']}")
    context = "\n\n---\n\n".join(context_parts)

    # Generate answer
    system = (
        "You are a helpful assistant with access to chat history memories. "
        "Answer the question based on the relevant memories provided. "
        "Answer step by step: first extract relevant information from the "
        "memories, then reason to get the answer. If the information is not "
        "in the memories, say so."
    )
    prompt = (
        f"Retrieved Memories:\n\n{context}\n\n"
        f"Current Date: {question_date}\n"
        f"Question: {question}\n"
        f"Answer (step by step):"
    )

    t1 = time.perf_counter()
    try:
        answer = call_llm(system, prompt, max_tokens=512)
    except Exception as e:
        answer = f"Error generating answer: {e}"
    gen_time = (time.perf_counter() - t1) * 1000

    return {
        "answer": answer,
        "n_memories_retrieved": len(results["results"]),
        "search_time_ms": round(search_time, 1),
        "gen_time_ms": round(gen_time, 1),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main pipeline
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_benchmark(data_file: str, mode: str, llm_backend: str,
                  output_file: str, topk: int = 10, limit: int = 0):
    """Run LongMemEval benchmark with sage-memory."""

    call_llm = call_anthropic if llm_backend == "anthropic" else call_openai

    # Load dataset
    print(f"Loading {data_file}...")
    with open(data_file) as f:
        dataset = json.load(f)

    if limit > 0:
        dataset = dataset[:limit]

    print(f"  {len(dataset)} questions loaded")
    print(f"  Mode: {mode}, LLM: {llm_backend}, TopK: {topk}")

    tmpdir = Path(tempfile.mkdtemp())
    results = []
    total_store_time = 0
    total_search_time = 0
    total_memories = 0

    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    for i, entry in enumerate(dataset):
        qid = entry["question_id"]
        question = entry["question"]
        question_date = entry["question_date"]
        sessions = entry["haystack_sessions"]
        dates = entry["haystack_dates"]
        session_ids = entry["haystack_session_ids"]

        print(f"  [{i+1}/{len(dataset)}] {qid}: {question[:60]}...", end=" ", flush=True)

        # Fresh database per question (isolate each question's context)
        close_all()
        proj = tmpdir / f"q_{qid}"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / ".git").mkdir(exist_ok=True)
        override_project_root(proj)

        # Ingest
        t0 = time.perf_counter()
        if mode == "raw":
            ingest_raw(sessions, dates, session_ids)
        else:
            ingest_extract(sessions, dates, session_ids, call_llm)
        store_time = (time.perf_counter() - t0) * 1000

        # Count stored
        listing = list_memories(limit=1)
        n_stored = listing["total"]

        # Answer
        result = answer_question(question, question_date, call_llm, topk=topk)

        # Record
        output_entry = {
            "question_id": qid,
            "hypothesis": result["answer"],
        }
        results.append(output_entry)

        # Stats
        total_store_time += store_time
        total_search_time += result["search_time_ms"]
        total_memories += n_stored

        print(f"stored={n_stored} search={result['search_time_ms']:.0f}ms")

        # Write incrementally (resume-safe)
        with open(output_file, "a") as f:
            f.write(json.dumps(output_entry) + "\n")

        # Cleanup per-question DB to save disk
        close_all()
        shutil.rmtree(proj, ignore_errors=True)

    # Summary
    n = len(dataset)
    print(f"\n{'='*60}")
    print(f"  BENCHMARK COMPLETE")
    print(f"{'='*60}")
    print(f"  Questions:        {n}")
    print(f"  Mode:             {mode}")
    print(f"  Avg memories/q:   {total_memories/max(n,1):.0f}")
    print(f"  Avg store time:   {total_store_time/max(n,1):.0f}ms")
    print(f"  Avg search time:  {total_search_time/max(n,1):.0f}ms")
    print(f"  Output:           {output_file}")
    print(f"\n  Next: evaluate with LongMemEval's evaluate_qa.py:")
    print(f"    python src/evaluation/evaluate_qa.py gpt-4o {output_file} {data_file}")
    print(f"  Or use evaluate_with_claude.py (no OpenAI needed):")
    print(f"    python evaluate_with_claude.py --hyp {output_file} --ref {data_file}")

    shutil.rmtree(tmpdir, ignore_errors=True)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run LongMemEval with sage-memory")
    parser.add_argument("--data_file", required=True, help="Path to longmemeval JSON")
    parser.add_argument("--mode", choices=["raw", "extract"], default="raw",
                        help="raw=store sessions directly, extract=LLM extracts facts first")
    parser.add_argument("--llm", choices=["anthropic", "openai"], default="anthropic",
                        help="LLM backend for generation (and extraction in extract mode)")
    parser.add_argument("--output", required=True, help="Output JSONL file")
    parser.add_argument("--topk", type=int, default=10, help="Top-K memories to retrieve")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of questions (0=all)")
    args = parser.parse_args()

    # Clear output file if exists
    if os.path.exists(args.output):
        os.remove(args.output)

    run_benchmark(args.data_file, args.mode, args.llm, args.output,
                  topk=args.topk, limit=args.limit)
