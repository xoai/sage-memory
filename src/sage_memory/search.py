"""Hybrid search across project + global databases.

Pipeline:
  1. Build FTS5 query (OR, stopwords, prefix match, term-frequency filtering)
  2. For each active DB (project first, then global):
     a. FTS5 BM25 keyword search → ranked list A
     b. sqlite-vec cosine (if embedder quality ≥ threshold) → ranked list B
     c. Weighted RRF fusion(A, B) → per-DB candidates
  3. Merge across DBs: project results get priority boost
  4. Apply tag boost + recency tiebreaker
  5. Return top-k

The FTS5 query builder now filters terms by discriminative power:
high-frequency terms that appear in >20% of documents are dropped,
keeping only terms that actually help BM25 differentiate results.
"""

from __future__ import annotations

import json
import math
import os
import re
import time

import logging

from .db import get_db, get_all_dbs
from .embedder import get_embedder, serialize_vec
from . import graph_channel
from . import llm
from . import expand as expand_mod
from . import rerank as rerank_mod


logger = logging.getLogger("sage-memory")

# RRF constant
_RRF_K = 60

# Below this, skip vec search entirely
_VEC_QUALITY_THRESHOLD = 0.6

# M3b (T2): vec_weight floor per ADR-004 §"auto resolution" —
# keeps LocalEmbedder (q=0.45, hypothetical if it ever passes the
# vec threshold by config override) from contributing too little to
# the RRF. Hosted-API tiers (q ≥ 0.85) are unaffected by this floor.
_VEC_WEIGHT_FLOOR = 0.5

# M3b (T2): default graph channel weight per ADR-004.
_GRAPH_WEIGHT = 0.7

# M5 T8 — Rerank min-coverage gate (per ADR-004 amendment
# 2026-05-17). When the LLM rerank covers < this fraction of the
# top-K head, fall back to pure RRF order (skip the blend math).
# Side-steps the partial-coverage dilution observed on LongMemEval.
# Configurable via SAGE_RERANK_MIN_COVERAGE.
_RERANK_MIN_COVERAGE = float(
    os.environ.get("SAGE_RERANK_MIN_COVERAGE", "0.5")
)

# M4 (T3): position-blend curve per ADR-004 §"Rerank position-blend".
# Three w_rrf values for positions [1-3], [4-10], [11+]. Configurable
# via SAGE_RERANK_BLEND_CURVE as a comma-separated triplet. Read at
# import; reload required to change.
def _parse_blend_curve() -> tuple[float, float, float]:
    raw = os.environ.get("SAGE_RERANK_BLEND_CURVE", "0.75,0.6,0.4")
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 3:
        raise ValueError(
            f"SAGE_RERANK_BLEND_CURVE must have 3 comma-separated "
            f"floats, got {len(parts)}: {raw!r}"
        )
    return tuple(float(p) for p in parts)  # type: ignore[return-value]


_BLEND_CURVE: tuple[float, float, float] = _parse_blend_curve()

# When filter_tags is present, over-fetch from FTS5/vec by this multiplier
# to compensate for candidates removed by tag filtering.
_FILTER_OVERFETCH = 3

# Temporal decay half-life: 14 days
_DECAY_HALF_LIFE = 14 * 86400

# Max fraction of corpus a term can match before it's dropped from the query.
# At 50K docs, a term matching >10K docs is noise, not signal.
_MAX_DOC_FREQUENCY_RATIO = 0.20

# Batched access tracking
_access_buffer: list[tuple[str, float, object]] = []  # (id, timestamp, db)
_ACCESS_FLUSH_SIZE = 20

# FTS stopwords
_STOP = frozenset(
    "a an the is are was were be been being have has had do does did "
    "will would shall should may might can could to of in for on with "
    "at by from as into through during before after above below between "
    "out off over under again further then once here there when where "
    "why how all each every both few more most other some such no nor "
    "not only own same so than too very and but or if this that it its "
    "what which who whom whose need use add get set make also just "
    "def self cls return import class async await none true false "
    "function method code file work using used way like new "
    "data type types value values based".split()
)


def search(*, query: str, scope: str = "project",
           tags: list[str] | None = None,
           filter_tags: list[str] | None = None,
           limit: int = 5,
           strategy: str = "hybrid",
           channels: list[str] | None = None,
           expand: bool | None = None,
           rerank: bool | None = None) -> dict:
    """Search memories across project + global DBs.

    tags: boost matching results (+3% per match). Does not exclude.
    filter_tags: hard WHERE filter (AND logic). Only memories matching
                 ALL filter_tags are returned. Use for namespace isolation.

    M3b additions (per spec rev 5):
    - channels: subset of {"bm25","vector","graph"}. None = all available
      (M2-equivalent + graph). [] = no channels run; returns empty.
    - expand: RESERVED for M4. Accepted; emits DEBUG log; no-op in M3b.
    - rerank: RESERVED for M4. Same accept-and-log-no-op contract.
    """
    # M4 (T5): per-stage timings instrumentation. All deltas use
    # time.perf_counter() (monotonic, high-resolution). Bench harness
    # strips `timings` before JSONL write to preserve A10 byte-identity.
    _t_total_start = time.perf_counter()
    _timings = {
        "expansion_ms": 0.0, "retrieval_ms": 0.0, "fusion_ms": 0.0,
        "dedup_ms": 0.0, "rerank_ms": 0.0, "scoring_ms": 0.0,
        "total_ms": 0.0,
    }

    query = query.strip()
    if len(query) < 2:
        _timings["total_ms"] = round(
            (time.perf_counter() - _t_total_start) * 1000.0, 3,
        )
        return {
            "results": [], "total": 0, "query": query,
            "timings": _timings,
        }

    # M3b (T3): explicit empty channels list → no-op return.
    if channels == []:
        _timings["total_ms"] = round(
            (time.perf_counter() - _t_total_start) * 1000.0, 3,
        )
        return {
            "results": [], "total": 0, "query": query,
            "reason": "no_channels_selected",
            "timings": _timings,
        }

    # M4 (T4): resolve expand/rerank 3-state matrix per spec A9.
    expand_enabled = _resolve_llm_stage_enabled("expand", expand)
    rerank_enabled = _resolve_llm_stage_enabled("rerank", rerank)

    # Channel resolution: None means all available; explicit list filters.
    active_channels = (
        set(channels) if channels is not None
        else {"bm25", "vector", "graph"}
    )

    tags = [t.lower().strip() for t in (tags or []) if t.strip()]
    now = time.time()
    embedder = get_embedder()
    # M3b (T3): channels gate overlays strategy. "vector" must be in
    # active_channels AND strategy must permit vec AND embedder must
    # pass the quality threshold.
    use_vec = (
        "vector" in active_channels
        and strategy in ("hybrid", "semantic")
        and embedder.quality >= _VEC_QUALITY_THRESHOLD
    )
    use_bm25 = "bm25" in active_channels
    use_graph = "graph" in active_channels

    # Over-fetch when filter_tags present — filtering removes candidates,
    # so we need more from FTS5/vec to fill the requested limit.
    overfetch = _FILTER_OVERFETCH if filter_tags else 1
    candidates_per_leg = limit * 2 * overfetch

    # Build tag filter SQL fragments (reused by both FTS and vec)
    tag_where, tag_params = _build_tag_filter(filter_tags)

    # Determine which databases to search
    if scope == "global":
        from .db import get_global_db
        dbs = [("global", get_global_db())]
    else:
        dbs = get_all_dbs()  # project first, then global

    # Embed query once if needed
    query_vec = embedder.embed(query) if use_vec else None

    # ── M4 (T4): query expansion ──────────────────────────────────
    # Defaults: no expansion (preserves M3b behavior on free path).
    lex_variants: list[str] = []   # extra FTS queries beyond `query`
    vec_query_str: str = query     # effective query for vec channel
    hyde_doc: str | None = None    # optional second vec query
    if expand_enabled and use_bm25:
        # T5: expansion_ms covers the probe + decision + LLM call.
        # NEVER 0.0 — the probe always runs at minimum.
        _t_exp = time.perf_counter()
        seed = _seed_bm25_probe(db=dbs[0][1], query=query)
        variants = expand_mod.expand_query(query, seed)
        # Dedup lex variants against `query` and each other; cap at 5
        # extra queries (defensive — LLM contract says ≤3 but cap
        # protects against runaway).
        seen = {query}
        for v in variants.get("lex", []):
            if v and v not in seen:
                lex_variants.append(v)
                seen.add(v)
            if len(lex_variants) >= 5:
                break
        v_vec = variants.get("vec") or query
        vec_query_str = v_vec
        hyde_doc = variants.get("hyde")
        _timings["expansion_ms"] = round(
            (time.perf_counter() - _t_exp) * 1000.0, 3,
        )

    # ── Search each DB and collect scored candidates ──────────

    all_candidates: list[tuple[dict, float, str]] = []  # (row_dict, score, db_label)

    for db_label, db in dbs:
        # M3a (T6b): embed_pending/embed_pending_chunks calls REMOVED
        # from the search path. The worker is the sole writer to
        # memories_vec/chunks_vec via reembed tasks enqueued at write
        # time. Search is now strictly read-only.

        # FTS5 search — memories (gated by active_channels)
        if use_bm25:
            fts_ids, row_cache = _fts_search(
                db, query, candidates_per_leg, tag_where, tag_params,
            )
            chunk_fts_ids, chunk_rows = _fts_search_chunks(
                db, query, candidates_per_leg, tag_where, tag_params,
            )
            row_cache.update(chunk_rows)
            # M4 (T4): fold each dedup'd lex variant as an additional
            # FTS5 query. Variants extend the bm25 channel's input
            # list (RRF naturally handles dupes via positional decay).
            for variant in lex_variants:
                var_ids, var_rows = _fts_search(
                    db, variant, candidates_per_leg,
                    tag_where, tag_params,
                )
                fts_ids = fts_ids + var_ids
                row_cache.update(var_rows)
        else:
            fts_ids, chunk_fts_ids = [], []
            row_cache = {}

        # Vec search — memories. M4 (T4): use vec_query_str (the
        # expand-derived vec form) when expand fired; falls back to
        # original `query` otherwise (M3b parity on free path).
        vec_ids: list[str] = []
        if use_vec and query_vec and strategy != "keyword":
            effective_vec = (
                embedder.embed(vec_query_str)
                if vec_query_str != query else query_vec
            )
            vec_ids, vec_rows = _vec_search(
                db, effective_vec, candidates_per_leg,
                tag_where, tag_params,
            )
            row_cache.update(vec_rows)
            # M4 (T4): hyde document, when present, produces a
            # second embedding query that extends the vec channel.
            if hyde_doc:
                hyde_vec = embedder.embed(hyde_doc)
                hyde_ids, hyde_rows = _vec_search(
                    db, hyde_vec, candidates_per_leg,
                    tag_where, tag_params,
                )
                vec_ids = vec_ids + hyde_ids
                row_cache.update(hyde_rows)

        # Vec search — chunks (M2). Uses the original query_vec
        # (chunks don't benefit from vec-query rephrasing; M5 may
        # revisit).
        chunk_vec_ids: list[str] = []
        if use_vec and query_vec and strategy != "keyword":
            chunk_vec_ids, chunk_vec_rows = _vec_search_chunks(
                db, query_vec, candidates_per_leg, tag_where, tag_params,
            )
            row_cache.update(chunk_vec_rows)

        if strategy == "keyword":
            vec_ids = []
            chunk_vec_ids = []

        # M3b (T2): graph proximity channel. Empty-table fast-path
        # makes this near-free when entities are unpopulated (free-
        # path floor — A6 invariant). Returns memory_ids already
        # ranked; we extract just the order for RRF. Gated by
        # active_channels (T3).
        if use_graph:
            graph_results = graph_channel.graph_proximity(
                db, query, limit=candidates_per_leg,
            )
            graph_ids = [r["memory_id"] for r in graph_results]
        else:
            graph_ids = []
        # Fetch any graph-only memory rows so they can be scored.
        # Memories already in fts_ids/vec_ids are in row_cache; only
        # the strictly-graph-only ones need a back-fill query.
        graph_only_ids = [
            mid for mid in graph_ids if mid not in row_cache
        ]
        if graph_only_ids:
            placeholders = ",".join("?" * len(graph_only_ids))
            graph_only_rows = db.execute(
                f"SELECT * FROM memories WHERE id IN ({placeholders}) "
                f"AND status = 'active'",
                graph_only_ids,
            ).fetchall()
            for row in graph_only_rows:
                row_cache[row["id"]] = row

        if (not fts_ids and not vec_ids and not chunk_fts_ids
                and not chunk_vec_ids and not graph_ids):
            continue

        # Weighted RRF — chunk channels share the same weights as their
        # memory-level counterparts. A memory that hits via both its own
        # memories_fts AND a chunk's chunks_fts accumulates both
        # contributions (boost), which is the intended chunk-aware behavior.
        fts_weight = 1.0
        # M3b (T2): vec_weight floor per ADR-004 alignment.
        vec_weight = (
            max(_VEC_WEIGHT_FLOOR, embedder.quality)
            if (vec_ids or chunk_vec_ids) else 0.0
        )
        graph_weight = _GRAPH_WEIGHT if graph_ids else 0.0

        raw: dict[str, float] = {}
        for rank, mid in enumerate(fts_ids, 1):
            raw[mid] = raw.get(mid, 0.0) + fts_weight / (_RRF_K + rank)
        for rank, mid in enumerate(chunk_fts_ids, 1):
            raw[mid] = raw.get(mid, 0.0) + fts_weight / (_RRF_K + rank)
        for rank, mid in enumerate(vec_ids, 1):
            raw[mid] = raw.get(mid, 0.0) + vec_weight / (_RRF_K + rank)
        for rank, mid in enumerate(chunk_vec_ids, 1):
            raw[mid] = raw.get(mid, 0.0) + vec_weight / (_RRF_K + rank)
        for rank, mid in enumerate(graph_ids, 1):
            raw[mid] = raw.get(mid, 0.0) + graph_weight / (_RRF_K + rank)

        if not raw:
            continue

        # Normalize RRF to [0, 1]
        max_rrf = max(raw.values())
        min_rrf = min(raw.values())
        rrf_range = max_rrf - min_rrf if max_rrf > min_rrf else 1.0

        for mid, rrf_raw in raw.items():
            row = row_cache.get(mid)
            if not row:
                continue

            relevance = (rrf_raw - min_rrf) / rrf_range

            # Tag boost: +3% per match, cap 15%
            item_tags = json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"]
            tag_bonus = min(len(set(item_tags) & set(tags)) * 0.03, 0.15) if tags else 0.0

            # Recency tiebreaker: [0, 0.05]
            age = max(now - row["accessed_at"], 0.0)
            recency = 0.05 * math.exp(-math.log(2) * age / _DECAY_HALF_LIFE)

            # Project DB gets a priority boost over global
            db_boost = 0.10 if db_label == "project" else 0.0

            score = relevance + tag_bonus + recency + db_boost
            all_candidates.append((dict(row), round(score, 6), db_label))

    # ── Sort and deduplicate across DBs ───────────────────────

    all_candidates.sort(key=lambda x: x[1], reverse=True)

    # M4 (T4): apply rerank + position-blend on the top-K head.
    if rerank_enabled and len(all_candidates) >= 2:
        _t_rr = time.perf_counter()
        all_candidates = _apply_rerank_blend(
            query=query,
            all_candidates=all_candidates,
            top_k=rerank_mod._TOP_K_DEFAULT,
        )
        _timings["rerank_ms"] = round(
            (time.perf_counter() - _t_rr) * 1000.0, 3,
        )

    seen_hashes: set[str] = set()
    results: list[dict] = []

    for row, score, db_label in all_candidates:
        h = row["content_hash"]
        if h in seen_hashes:
            continue
        seen_hashes.add(h)

        results.append({
            "id": row["id"],
            "title": row["title"],
            "content": row["content"],
            "tags": json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"],
            "score": score,
            "source": db_label,
        })

        # Buffer access tracking
        _access_buffer.append((row["id"], now, None))

        if len(results) >= limit:
            break

    # Flush access buffer if needed
    _flush_access(dbs)

    _timings["total_ms"] = round(
        (time.perf_counter() - _t_total_start) * 1000.0, 3,
    )
    return {
        "results": results, "total": len(all_candidates),
        "query": query, "timings": _timings,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tag filter builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M4 (T4) — expand/rerank stage resolution + seed probe + apply
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _resolve_llm_stage_enabled(
    stage_name: str, param_value: bool | None,
) -> bool:
    """A9 three-state matrix for expand / rerank MCP params.

    - param is None: enabled iff llm.is_configured() — default.
    - param is True: force enable. If LLM unconfigured, WARN and
      return False (caller skips). The WARN surfaces the contract
      violation explicitly (consistent with failure_visibility=warn).
    - param is False: force disable. Skip silently.
    """
    if param_value is False:
        return False
    configured = llm.is_configured()
    if param_value is True and not configured:
        logger.warning(
            "search: %s=True requested but no LLM key configured; "
            "skipping", stage_name,
        )
        return False
    if param_value is True:
        return True
    # param is None
    return configured


def _seed_bm25_probe(
    *, db, query: str, limit: int = 3,
) -> list[tuple[str, float]]:
    """Pre-retrieval FTS5 probe for expand's strong-signal decision.

    Runs the SAME _build_fts_query as the main retrieval — keeps the
    strong-signal decision consistent with what the main search would
    actually fetch. Returns top-`limit` (memory_id, raw_bm25_score)
    pairs ordered by relevance (lowest raw_bm25_score first).

    Failures (no query terms, no FTS table, exception) return [] —
    expand.py treats empty as "no strong signal possible → run LLM".
    """
    fts_q = _build_fts_query(db, query)
    if not fts_q:
        return []
    try:
        rows = db.execute(
            "SELECT m.id AS id, "
            "bm25(memories_fts, 10.0, 3.0, 1.0) AS bm25_score "
            "FROM memories m "
            "JOIN memories_fts fts ON m.rowid = fts.rowid "
            "WHERE m.status = 'active' AND memories_fts MATCH ? "
            "ORDER BY bm25_score LIMIT ?",
            (fts_q, limit),
        ).fetchall()
    except Exception:
        return []
    return [(r["id"], float(r["bm25_score"])) for r in rows]


def _apply_rerank_blend(
    *,
    query: str,
    all_candidates: list,
    top_k: int,
) -> list:
    """Take top-K of all_candidates, call rerank, blend, re-sort head.

    Tail (positions > top_k) keeps its existing order (already sorted
    by score). Per spec A7: position-blend uses the 1-indexed RRF
    rank (the ORIGINAL position before reranking).
    """
    head = all_candidates[:top_k]
    tail = all_candidates[top_k:]

    # Build rerank input: id is the position-1-indexed RRF rank;
    # this lets us round-trip llm_score back to the original entry.
    rerank_input = [
        {
            "id": pos,  # 1-indexed RRF position; doubles as join key
            "memory_id": row["id"],
            "content": row.get("content", ""),
            "rrf_score": float(score),
        }
        for pos, (row, score, _) in enumerate(head, start=1)
    ]
    scored = rerank_mod.rerank(query, rerank_input)

    # Map id → llm_score.
    llm_by_pos = {c["id"]: c.get("llm_score") for c in scored}

    # M5 T8 / ADR-004 amendment — min-coverage gate. When the LLM
    # only scored a small fraction of the head, the partial-blend
    # math demotes the LLM-confirmed best below its un-scored
    # siblings (because non-None llm_score shrinks the candidate
    # via the w_rrf multiplier; None-branch leaves it unchanged).
    # Fall back to pure RRF for the head when coverage is below
    # the configured threshold.
    coverage = (
        sum(1 for v in llm_by_pos.values() if v is not None)
        / max(1, len(head))
    )
    if coverage < _RERANK_MIN_COVERAGE:
        logger.debug(
            "search: rerank coverage %.2f < %.2f; skipping blend, "
            "keeping pure RRF order (per ADR-004 amendment)",
            coverage, _RERANK_MIN_COVERAGE,
        )
        return all_candidates

    # Blend each head candidate. The position passed to _blended_score
    # is the original 1-indexed RRF rank (per spec A7) — NOT the new
    # post-blend rank.
    new_head = []
    for pos, (row, score, db_label) in enumerate(head, start=1):
        llm_score = llm_by_pos.get(pos)
        blended = _blended_score(
            rrf_score=score, llm_score=llm_score, position=pos,
        )
        new_head.append((row, blended, db_label))

    # Re-sort head by blended score (descending).
    new_head.sort(key=lambda x: x[1], reverse=True)
    return new_head + tail


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M4 (T3) — position-blend math
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _blended_score(
    rrf_score: float, llm_score: float | None, position: int,
) -> float:
    """Combine RRF score with LLM rerank score via position-dependent
    weight curve. Per ADR-004 §"Rerank position-blend" + spec A7.

    Args:
        rrf_score: post-fusion RRF score for the candidate (any float).
        llm_score: LLM rerank score in [0, 1], OR None when the LLM
            either failed or did not score this candidate. None
            triggers the A7 branch: return rrf_score unchanged (do
            NOT coerce None to 0 — that would down-weight the
            candidate below all reranked entries).
        position: 1-indexed RRF rank. Positions 1-3 use _BLEND_CURVE[0]
            (default 0.75), 4-10 use [1] (0.6), 11+ use [2] (0.4).

    Returns: blended score = w_rrf * rrf_score + (1 - w_rrf) * llm_score
    """
    if position < 1:
        raise ValueError(
            f"_blended_score: position must be 1-indexed (got {position})"
        )
    if llm_score is None:
        return rrf_score
    if position <= 3:
        w_rrf = _BLEND_CURVE[0]
    elif position <= 10:
        w_rrf = _BLEND_CURVE[1]
    else:
        w_rrf = _BLEND_CURVE[2]
    return w_rrf * rrf_score + (1.0 - w_rrf) * llm_score


def _build_tag_filter(filter_tags: list[str] | None) -> tuple[str, list]:
    """Build SQL WHERE fragment for hard tag filtering (AND logic).

    Returns ("m.tags LIKE ? AND m.tags LIKE ?", [params...]) or ("", []).
    """
    if not filter_tags:
        return "", []

    clauses = []
    params = []
    for tag in filter_tags:
        clauses.append("m.tags LIKE ?")
        params.append(f'%"{tag.lower().strip()}"%')

    return " AND ".join(clauses), params


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FTS5 search with smart query building
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _fts_search(db, query: str, limit: int,
                tag_where: str, tag_params: list
                ) -> tuple[list[str], dict[str, dict]]:
    fts_q = _build_fts_query(db, query)
    if not fts_q:
        return [], {}

    sql = """SELECT m.*, bm25(memories_fts, 10.0, 3.0, 1.0) AS bm25_score
             FROM memories m
             JOIN memories_fts fts ON m.rowid = fts.rowid
             WHERE m.status = 'active' AND memories_fts MATCH ?"""
    params: list = [fts_q]

    if tag_where:
        sql += f" AND {tag_where}"
        params.extend(tag_params)

    sql += " ORDER BY bm25_score LIMIT ?"
    params.append(limit)

    try:
        rows = db.execute(sql, params).fetchall()
    except Exception:
        return [], {}

    ids = [r["id"] for r in rows]
    cache = {r["id"]: dict(r) for r in rows}
    return ids, cache


def _build_fts_query(db, query: str) -> str:
    """Build an FTS5 OR query with term-frequency filtering.

    Steps:
      1. Tokenize and remove stopwords
      2. Check document frequency for each term via fts5vocab
      3. Drop terms that appear in >20% of documents (too common to be useful)
      4. Join remaining terms with OR and prefix matching
    """
    cleaned = re.sub(r'[^\w\s]', " ", query)
    cleaned = re.sub(r"\b(AND|OR|NOT|NEAR)\b", " ", cleaned, flags=re.IGNORECASE)
    words = [w.lower() for w in cleaned.split()
             if len(w) >= 2 and w.lower() not in _STOP]

    if not words:
        return ""

    # Term-frequency filtering: drop ubiquitous terms
    try:
        total_docs = db.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
        if total_docs > 100:  # only filter when corpus is large enough
            threshold = int(total_docs * _MAX_DOC_FREQUENCY_RATIO)
            filtered = []
            for w in words:
                # Check how many docs contain this term (prefix match)
                count = db.execute(
                    """SELECT COUNT(*) c FROM memories_fts
                       WHERE memories_fts MATCH ?""",
                    (f"{w}*",),
                ).fetchone()["c"]
                if count <= threshold:
                    filtered.append(w)

            # If all terms were filtered, keep the least common ones
            if not filtered and words:
                filtered = words[:3]
            words = filtered
    except Exception:
        pass  # If vocab check fails, use all words

    if not words:
        return ""

    return " OR ".join(f"{w}*" for w in words)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Vec search
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _vec_search(db, query_vec: list[float], limit: int,
                tag_where: str, tag_params: list
                ) -> tuple[list[str], dict[str, dict]]:
    vec_bytes = serialize_vec(query_vec)

    rows = db.execute(
        """SELECT memory_id, distance FROM memories_vec
           WHERE embedding MATCH ? ORDER BY distance LIMIT ?""",
        (vec_bytes, limit),
    ).fetchall()

    if not rows:
        return [], {}

    ids = [r["memory_id"] for r in rows]
    ph = ",".join("?" for _ in ids)

    sql = f"SELECT * FROM memories WHERE id IN ({ph}) AND status = 'active'"
    params = list(ids)

    if tag_where:
        sql += f" AND {tag_where}"
        params.extend(tag_params)

    mem_rows = db.execute(sql, params).fetchall()

    cache = {r["id"]: dict(r) for r in mem_rows}
    ordered = [mid for mid in ids if mid in cache]
    return ordered, cache


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Chunk-aware search (M2 — T4)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Both helpers below query the chunk-level indexes (chunks_fts /
# chunks_vec) and dedup the results to parent memory_id using a
# window function: ROW_NUMBER() PARTITION BY memory_id ORDER BY
# (score, chunks.chunk_index ASC). Tie-breaker on equal score is
# `chunks.chunk_index` ASC, so the per-memory winner is deterministic
# (matched_chunk_id is reproducible).


def _fts_search_chunks(db, query: str, limit: int,
                       tag_where: str, tag_params: list
                       ) -> tuple[list[str], dict[str, dict]]:
    """BM25 search over chunks_fts, deduped to parent memory_id.

    Returns ([memory_id, ...], {memory_id: memory_row_dict}). The list
    is ordered by best-chunk score per memory (ascending bm25, i.e.
    best first). Each memory appears at most once."""
    fts_q = _build_fts_query(db, query)
    if not fts_q:
        return [], {}

    sql = """
        WITH chunk_hits AS (
            SELECT c.memory_id,
                   bm25(chunks_fts, 1.0) AS bm25_score,
                   c.chunk_index,
                   ROW_NUMBER() OVER (
                       PARTITION BY c.memory_id
                       ORDER BY bm25(chunks_fts, 1.0), c.chunk_index
                   ) AS rn
            FROM chunks c
            JOIN chunks_fts fts ON c.rowid = fts.rowid
            WHERE chunks_fts MATCH ?
        )
        SELECT m.*, ch.bm25_score
          FROM chunk_hits ch
          JOIN memories m ON ch.memory_id = m.id
         WHERE ch.rn = 1 AND m.status = 'active'
    """
    params: list = [fts_q]

    if tag_where:
        sql += f" AND {tag_where}"
        params.extend(tag_params)

    sql += " ORDER BY ch.bm25_score LIMIT ?"
    params.append(limit)

    try:
        rows = db.execute(sql, params).fetchall()
    except Exception:
        return [], {}

    ids = [r["id"] for r in rows]
    cache = {r["id"]: dict(r) for r in rows}
    return ids, cache


def _vec_search_chunks(db, query_vec: list[float], limit: int,
                       tag_where: str, tag_params: list
                       ) -> tuple[list[str], dict[str, dict]]:
    """Vector search over chunks_vec, deduped to parent memory_id.

    Same return shape as `_vec_search` but operating on chunks. Best-
    chunk-per-memory wins (lowest distance), tie-broken by chunk_index."""
    vec_bytes = serialize_vec(query_vec)

    # vec0 doesn't support window functions inside the MATCH query;
    # do the dedup in a CTE wrapping the vec0 results.
    rows = db.execute(
        """SELECT chunk_id, distance FROM chunks_vec
           WHERE embedding MATCH ? ORDER BY distance LIMIT ?""",
        (vec_bytes, max(limit * 3, limit)),  # over-fetch since dedup shrinks
    ).fetchall()
    if not rows:
        return [], {}

    chunk_ids = [r["chunk_id"] for r in rows]
    chunk_to_dist = {r["chunk_id"]: r["distance"] for r in rows}
    ph = ",".join("?" for _ in chunk_ids)

    # Dedup chunks to memories: best (lowest) distance per memory, tied
    # by chunk_index ASC.
    sql_dedup = f"""
        WITH chunk_hits AS (
            SELECT c.memory_id, c.id AS chunk_id, c.chunk_index,
                   ROW_NUMBER() OVER (
                       PARTITION BY c.memory_id
                       ORDER BY c.chunk_index
                   ) AS rn_idx
              FROM chunks c
             WHERE c.id IN ({ph})
        )
        SELECT memory_id, chunk_id FROM chunk_hits
    """
    chunk_rows = db.execute(sql_dedup, chunk_ids).fetchall()

    # Pick best chunk per memory by distance (NOT chunk_index — that's
    # the tie-breaker only). chunks_vec didn't return rows grouped by
    # memory, so we compute the best here.
    best_per_mem: dict[str, tuple[float, int, str]] = {}
    chunk_to_idx_and_mem: dict[str, tuple[str, int]] = {}
    for cr in chunk_rows:
        # We don't have chunk_index in chunk_to_dist mapping; need it for tie-break.
        pass  # placeholder — refactor below

    # Simpler: re-fetch chunks rows for the matched chunk_ids with chunk_index,
    # then compute best per memory.
    chunk_info = db.execute(
        f"SELECT id, memory_id, chunk_index FROM chunks WHERE id IN ({ph})",
        chunk_ids,
    ).fetchall()
    info_map = {r["id"]: (r["memory_id"], r["chunk_index"]) for r in chunk_info}

    for cid in chunk_ids:
        dist = chunk_to_dist[cid]
        mem_id, idx = info_map.get(cid, (None, 999999))
        if mem_id is None:
            continue
        key = (dist, idx)
        if mem_id not in best_per_mem or key < (best_per_mem[mem_id][0], best_per_mem[mem_id][1]):
            best_per_mem[mem_id] = (dist, idx, cid)

    # Order memories by best distance ascending
    ordered_mem_ids = sorted(best_per_mem.keys(), key=lambda m: (best_per_mem[m][0], best_per_mem[m][1]))[:limit]

    if not ordered_mem_ids:
        return [], {}

    # Fetch memory rows (with optional tag filter)
    mem_ph = ",".join("?" for _ in ordered_mem_ids)
    sql_mem = (
        f"SELECT * FROM memories WHERE id IN ({mem_ph}) "
        f"AND status = 'active'"
    )
    params: list = list(ordered_mem_ids)
    if tag_where:
        sql_mem += f" AND {tag_where}"
        params.extend(tag_params)

    mem_rows = db.execute(sql_mem, params).fetchall()
    cache = {r["id"]: dict(r) for r in mem_rows}
    ordered = [mid for mid in ordered_mem_ids if mid in cache]
    return ordered, cache


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Batched access tracking
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _flush_access(dbs: list[tuple[str, object]]) -> None:
    global _access_buffer
    if len(_access_buffer) < _ACCESS_FLUSH_SIZE:
        return

    # Update all accessed memories in all dbs
    for _, db in dbs:
        for mid, ts, _ in _access_buffer:
            try:
                db.execute(
                    "UPDATE memories SET accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
                    (ts, mid),
                )
            except Exception:
                pass
        try:
            db.commit()
        except Exception:
            pass

    _access_buffer = []


def flush_all_access() -> None:
    """Force-flush access buffer (call on shutdown)."""
    global _access_buffer
    if _access_buffer:
        for _, db in get_all_dbs():
            for mid, ts, _ in _access_buffer:
                try:
                    db.execute(
                        "UPDATE memories SET accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
                        (ts, mid),
                    )
                except Exception:
                    pass
            try:
                db.commit()
            except Exception:
                pass
        _access_buffer = []
