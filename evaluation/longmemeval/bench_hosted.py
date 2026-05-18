#!/usr/bin/env python3
"""LongMemEval benchmark with hosted-embedder support.

The standard bench_longmemeval.py creates a fresh per-question
project DB. Each fresh DB defaults to vec_dim=384 (per
migration 001_initial.sql), so sage's resolver picks
FastEmbedder/LocalEmbedder (384d) even when OPENAI_API_KEY is set.
This script bypasses that lock by manually recreating memories_vec +
chunks_vec at the chosen tier's native dim before any store() call.

Usage:
    # Run on full 500q with OpenAI 3-small (1536d)
    OPENAI_API_KEY=sk-... python bench_hosted.py bm25-full

    # Run on 50 questions for a quick smoke
    OPENAI_API_KEY=sk-... python bench_hosted.py bm25-full 50

    # Run only on the single-session-preference type (n=30)
    OPENAI_API_KEY=sk-... python bench_hosted.py bm25-full 999 \
        single-session-preference

Args (positional):
    mode:       bm25 | bm25-full | hybrid-temporal (default: bm25-full)
    limit:      max questions (default: 50; use 999 for "all")
    type_filter: optional question_type filter (e.g.,
                 single-session-preference / multi-session / etc.)

Per-question metrics + per-type breakdown written to stdout.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import sqlite_vec

# Locate the repo root from this file's path.
REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "src"))

from sage_memory.db import (
    override_project_root, close_all, _migrate,
    _connections, get_project_db, get_project_db_path,
)
from sage_memory.embedder import (
    OpenAIEmbedder, VoyageEmbedder, CohereEmbedder, set_embedder,
)
from sage_memory.store import store as sage_store
from sage_memory.search import search as sage_search


# Pick the hosted embedder + native dim from env.
def _pick_hosted_embedder():
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIEmbedder(api_key=os.environ["OPENAI_API_KEY"]), 1536
    if os.environ.get("VOYAGE_API_KEY"):
        return VoyageEmbedder(api_key=os.environ["VOYAGE_API_KEY"]), 512
    if os.environ.get("COHERE_API_KEY"):
        return CohereEmbedder(api_key=os.environ["COHERE_API_KEY"]), 1024
    sys.exit(
        "no hosted-embedder API key found in env "
        "(OPENAI_API_KEY / VOYAGE_API_KEY / COHERE_API_KEY required)"
    )


EMBEDDER, DIM = _pick_hosted_embedder()
set_embedder(EMBEDDER)
print(
    f"[setup] {type(EMBEDDER).__name__} active "
    f"(dim={DIM}, quality={EMBEDDER.quality})",
    flush=True,
)


def _open_at_dim(path: Path, dim: int) -> sqlite3.Connection:
    """Custom replacement for db._open that creates vec0 tables at the
    chosen dim. Bypasses migration 001's 384d default."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA cache_size = -2000")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _migrate(conn)
    # Recreate vec0 tables at the chosen dim (migration default is 384).
    conn.execute("DROP TABLE memories_vec")
    conn.execute("DROP TABLE chunks_vec")
    conn.execute(
        f"CREATE VIRTUAL TABLE memories_vec USING vec0("
        f"memory_id TEXT PRIMARY KEY, embedding float[{dim}])"
    )
    conn.execute(
        f"CREATE VIRTUAL TABLE chunks_vec USING vec0("
        f"chunk_id TEXT PRIMARY KEY, embedding float[{dim}])"
    )
    conn.execute(
        "UPDATE corpus_meta SET value = ? WHERE key = 'vec_dim'",
        (str(dim),),
    )
    conn.commit()
    _connections[str(path)] = conn
    return conn


# Make sage_search hit ONLY the project DB (skip the 384d global DB
# which would cause a dim-mismatch on vec queries).
import sage_memory.search as _search_mod
import sage_memory.db as _db_mod


def _project_only_dbs():
    proj = _db_mod.get_project_db()
    return [("project", proj)] if proj is not None else []


_search_mod.get_all_dbs = _project_only_dbs


def _ingest_and_search(entry, mode, project_root: Path):
    """Mirrors bench_longmemeval._ingest_and_search but pre-creates
    the project DB at the chosen vec_dim before storing.

    Returns (ranked_indices, corpus_ids).
    """
    override_project_root(project_root)
    db_path = get_project_db_path(project_root)
    _open_at_dim(db_path, DIM)

    sessions = entry["haystack_sessions"]
    session_ids = entry["haystack_session_ids"]
    dates = entry["haystack_dates"]
    include_assistant = (mode == "bm25-full")
    corpus_ids: list[str] = []

    for session, sess_id, date in zip(sessions, session_ids, dates):
        if include_assistant:
            turns = [t["content"] for t in session]
        else:
            turns = [t["content"] for t in session if t["role"] == "user"]
        if not turns:
            continue
        doc = "\n".join(turns)
        corpus_ids.append(sess_id)
        sage_store(
            content=doc,
            title=f"[{sess_id}] {doc[:80]}",
            tags=["session", f"sid:{sess_id}", f"date:{date}"],
            scope="project",
        )

    if not corpus_ids:
        return [], corpus_ids

    r = sage_search(
        query=entry["question"],
        limit=min(50, len(corpus_ids)),
        # Skip LLM stages — we're isolating the vector channel's effect.
        expand=False, rerank=False,
    )
    r.pop("timings", None)

    id_to_idx = {cid: i for i, cid in enumerate(corpus_ids)}
    ranked: list[int] = []
    seen: set[str] = set()
    for result in r.get("results", []):
        title = result.get("title", "")
        m = re.match(r"\[([^\]]+)\]", title)
        if m:
            found = m.group(1)
            if found in id_to_idx and found not in seen:
                ranked.append(id_to_idx[found])
                seen.add(found)
    for i in range(len(corpus_ids)):
        if i not in seen and corpus_ids[i] not in seen:
            ranked.append(i)
            seen.add(corpus_ids[i])
    return ranked, corpus_ids


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "bm25-full"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    qtype_filter = sys.argv[3] if len(sys.argv) > 3 else None

    data_path = (
        REPO / "evaluation/longmemeval/data/longmemeval_s_cleaned.json"
    )
    if not data_path.exists():
        sys.exit(
            f"dataset not found at {data_path}. "
            "Download via the steps in evaluation/longmemeval/README.md"
        )
    data = json.loads(data_path.read_text())
    if qtype_filter:
        data = [e for e in data if e["question_type"] == qtype_filter]
        print(
            f"[filter] question_type={qtype_filter}: {len(data)} questions",
            flush=True,
        )

    tmp = Path(tempfile.mkdtemp(prefix="bench_hosted_"))
    print(f"mode={mode} limit={limit} tmp={tmp}", flush=True)

    recall_at = {k: 0 for k in (1, 3, 5, 10)}
    by_type_r5: dict[str, list[int]] = {}
    n = 0
    t0 = time.perf_counter()

    for i, entry in enumerate(data[:limit]):
        proj = tmp / f"q_{entry['question_id']}"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / ".sage").mkdir(exist_ok=True)

        ranked, corpus_ids = _ingest_and_search(entry, mode, proj)
        correct = set(entry["answer_session_ids"])

        n += 1
        qtype = entry["question_type"]
        for k in (1, 3, 5, 10):
            top_k_ids = {
                corpus_ids[i] for i in ranked[:k] if i < len(corpus_ids)
            }
            hit = 1 if (top_k_ids & correct) else 0
            recall_at[k] += hit
            if k == 5:
                by_type_r5.setdefault(qtype, []).append(hit)

        close_all()
        override_project_root(None)

        elapsed = time.perf_counter() - t0
        print(
            f"  [{i+1}/{len(data[:limit])}] q={entry['question_id']} "
            f"type={qtype} sessions={len(corpus_ids)} "
            f"R@5_so_far={recall_at[5]/n:.3f} "
            f"cum_elapsed={elapsed:.1f}s",
            flush=True,
        )

    elapsed = time.perf_counter() - t0
    print(
        f"\n=== Results (mode={mode}, n={n}, time={elapsed:.1f}s) ==="
    )
    for k in (1, 3, 5, 10):
        print(f"  R@{k} = {recall_at[k]/n:.3f}")
    print("  Per-type R@5:")
    for t, hits in sorted(by_type_r5.items()):
        print(
            f"    {t:35} n={len(hits):>3}  "
            f"R@5={sum(hits)/len(hits):.3f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
