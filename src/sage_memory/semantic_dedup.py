"""Memory-level semantic dedup via embedding cosine similarity.

When a new memory is stored, `find_near_duplicate()` searches
`memories_vec` for an existing memory whose embedding is
near-identical (cosine >= THRESHOLD; ASCII `>=` for grep
consistency with the response field, see `_format_near_dup_entry`).
The signal flows through the
existing `suggested_links` response field with
`confidence: "near_duplicate"` so the agent can decide whether to
link via `supersedes`, merge content, or store as distinct.

No LLM required — uses the existing embedding infrastructure.

Design constraints (see .sage/work/20260524-semantic-dedup/spec.md):
- k=5 vec0 MATCH (k=1 would always return the just-stored self-row
  after exclude_id filtering yields empty)
- Post-LIMIT filtering of `status='active'` + `exclude_id` (vec0
  doesn't support pre-rank WHERE on non-partition columns)
- Defensive normalize at the boundary (HostedEmbedder.embed_batch
  and FastEmbedder don't enforce unit length in our code)
- Cosine ↔ L2-distance identity for unit vectors:
      cos_sim = 1 - (L2_distance ** 2) / 2
"""

from __future__ import annotations

import logging
import math
import sqlite3

from .embedder import serialize_vec


_logger = logging.getLogger("sage_memory.semantic_dedup")

# Tunables — see spec §"Resolved decisions" and §"Why k>1".
THRESHOLD = 0.95
K_NEIGHBORS = 5

# Defensive normalize: only rescale when norm drifts outside this
# tolerance around unit length.
_NORM_TOL = 1e-3


def find_near_duplicate(
    conn,
    *,
    embedding: list[float],
    exclude_id: str | None = None,
    threshold: float = THRESHOLD,
    k: int = K_NEIGHBORS,
) -> dict | None:
    """Return the top eligible neighbor exceeding `threshold`, or None.

    Returns `{"target_id", "target_title", "similarity"}` for the
    closest active memory (other than `exclude_id`) whose cosine
    similarity to `embedding` is >= `threshold`. Returns None when
    no such memory exists, the embedding is malformed (zero norm
    or non-finite), or the underlying sqlite call errors.
    """
    # ── Step 1: defensive normalize ────────────────────────────────
    norm_sq = 0.0
    for x in embedding:
        norm_sq += x * x
    norm = math.sqrt(norm_sq)
    if not math.isfinite(norm) or norm == 0.0:
        # Zero / NaN / inf — treat as missing embedding.
        return None
    if abs(norm - 1.0) > _NORM_TOL:
        embedding = [x / norm for x in embedding]

    # ── Step 2: k-NN MATCH against memories_vec ────────────────────
    try:
        vec_bytes = serialize_vec(embedding)
        vec_rows = conn.execute(
            """SELECT memory_id, distance FROM memories_vec
               WHERE embedding MATCH ?
               ORDER BY distance LIMIT ?""",
            (vec_bytes, k),
        ).fetchall()
    except sqlite3.Error:
        _logger.debug("semantic_dedup: vec query failed", exc_info=True)
        return None

    if not vec_rows:
        return None

    # ── Step 3: post-LIMIT filter on status='active' + exclude_id ──
    ids = [r["memory_id"] for r in vec_rows]
    ph = ",".join("?" for _ in ids)
    try:
        mem_rows = conn.execute(
            f"SELECT id, title FROM memories "
            f"WHERE id IN ({ph}) AND status = 'active'",
            ids,
        ).fetchall()
    except sqlite3.Error:
        _logger.debug("semantic_dedup: title fetch failed", exc_info=True)
        return None

    active_titles = {r["id"]: r["title"] for r in mem_rows}

    # Iterate in distance order; return first non-self, active neighbor
    # whose cosine clears threshold.
    for vr in vec_rows:
        mid = vr["memory_id"]
        if exclude_id is not None and mid == exclude_id:
            continue
        if mid not in active_titles:
            # Filtered out by status check (invalidated / archived).
            continue
        # cos_sim = 1 - (L2**2)/2 for unit vectors.
        cos_sim = 1.0 - (vr["distance"] ** 2) / 2.0
        # Clamp tiny float drift into the valid [-1, 1] band.
        if cos_sim > 1.0:
            cos_sim = 1.0
        elif cos_sim < -1.0:
            cos_sim = -1.0
        if cos_sim >= threshold:
            return {
                "target_id": mid,
                "target_title": active_titles[mid],
                "similarity": cos_sim,
            }
    return None


def _format_near_dup_entry(near_dup: dict) -> dict:
    """Build the `suggested_links` envelope entry for a near-duplicate.

    Reads module THRESHOLD directly (no kwarg). Tests that assert
    on the `reason` string with a custom threshold patch THRESHOLD
    via monkeypatch — see spec §Components for the rationale.
    """
    sim = near_dup["similarity"]
    return {
        "target_id": near_dup["target_id"],
        "target_title": near_dup["target_title"],
        "reason": f"near-duplicate (cosine {sim:.2f} >= {THRESHOLD:.2f})",
        "confidence": "near_duplicate",
        "similarity": round(sim, 2),
    }
