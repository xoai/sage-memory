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
import re
import time

from .db import get_db, get_all_dbs
from .embedder import get_embedder, serialize_vec
from .store import embed_pending

# RRF constant
_RRF_K = 60

# Below this, skip vec search entirely
_VEC_QUALITY_THRESHOLD = 0.6

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
           strategy: str = "hybrid") -> dict:
    """Search memories across project + global DBs.

    tags: boost matching results (+3% per match). Does not exclude.
    filter_tags: hard WHERE filter (AND logic). Only memories matching
                 ALL filter_tags are returned. Use for namespace isolation.
    """
    query = query.strip()
    if len(query) < 2:
        return {"results": [], "total": 0, "query": query}

    tags = [t.lower().strip() for t in (tags or []) if t.strip()]
    now = time.time()
    embedder = get_embedder()
    use_vec = strategy in ("hybrid", "semantic") and embedder.quality >= _VEC_QUALITY_THRESHOLD

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

    # ── Search each DB and collect scored candidates ──────────

    all_candidates: list[tuple[dict, float, str]] = []  # (row_dict, score, db_label)

    for db_label, db in dbs:
        # Ensure pending embeddings are processed
        if use_vec:
            embed_pending(db, batch_size=20)

        # FTS5 search
        fts_ids, row_cache = _fts_search(db, query, candidates_per_leg, tag_where, tag_params)

        # Vec search
        vec_ids: list[str] = []
        if use_vec and query_vec and strategy != "keyword":
            vec_ids, vec_rows = _vec_search(db, query_vec, candidates_per_leg, tag_where, tag_params)
            row_cache.update(vec_rows)

        if strategy == "keyword":
            vec_ids = []

        if not fts_ids and not vec_ids:
            continue

        # Weighted RRF
        fts_weight = 1.0
        vec_weight = embedder.quality if vec_ids else 0.0

        raw: dict[str, float] = {}
        for rank, mid in enumerate(fts_ids, 1):
            raw[mid] = raw.get(mid, 0.0) + fts_weight / (_RRF_K + rank)
        for rank, mid in enumerate(vec_ids, 1):
            raw[mid] = raw.get(mid, 0.0) + vec_weight / (_RRF_K + rank)

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

    return {"results": results, "total": len(all_candidates), "query": query}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tag filter builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


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
             WHERE memories_fts MATCH ?"""
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

    sql = f"SELECT * FROM memories WHERE id IN ({ph})"
    params = list(ids)

    if tag_where:
        sql += f" AND {tag_where}"
        params.extend(tag_params)

    mem_rows = db.execute(sql, params).fetchall()

    cache = {r["id"]: dict(r) for r in mem_rows}
    ordered = [mid for mid in ids if mid in cache]
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
