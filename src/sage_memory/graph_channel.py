"""Graph-proximity channel — M3b (T1).

The third RRF channel: seed entities from a query, BFS over the
heterogeneous memory/entity graph (entity-mediated relations +
direct memory edges), rank candidate memories by distance and
relation weight. The channel is read-only.

Architecture per ADR-004 and M3b spec rev 5:

- Empty-table fast-path: skip all work when the entities table is
  empty (free-path floor — graph empty → 3-channel RRF degrades
  byte-for-byte to 2-channel = M2 behavior).
- Two-layer BFS:
  - Hop type 1 (entity-mediated): mentions → entities (canonical
    resolved) → relations → mentions back to memories.
  - Hop type 2 (memory-direct): edges.source_id → target_id
    (outbound-only; bidirectional symmetry is deferred — graph
    channel uses asymmetric trust as a ranking signal).
- Three rank curves dispatched by SAGE_GRAPH_RANK_CURVE env var
  (linear default, harmonic softer-falloff, type-weighted bonus).
- canonical_id resolved on read via COALESCE — M5 dedup can rewrite
  canonical_ids without touching mentions rows.
- Orphan filter: final SELECT INNER JOINs memories so deleted
  memory ids never surface.

The channel returns a list of internal dicts with keys
`memory_id`, `rank`, `seed_entity_id`, `distance`. The MCP surface
in search.py exposes only the existing M2 fields (the dict shape
is internal to fusion).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Literal


logger = logging.getLogger("sage_memory.graph_channel")


# ─── Constants ────────────────────────────────────────────────────

EPS = 1e-6
DEFAULT_RANK_CURVE = "linear"
_VALID_CURVES = ("linear", "harmonic", "type-weighted")

DEFAULT_DEPTH_CAP = 2
_RESULT_CAP_MULTIPLIER = 5     # spec: channel cap = limit × 5

# Relation weights per ADR-004 / spec
RELATION_WEIGHTS = {
    # Auto-relation defaults (LLM-extracted, controlled vocab)
    "implements":     1.0,
    "contains":       1.0,
    "depends_on":     1.0,
    "mentions":       0.7,
    "references":     0.7,
    "relates_to":     0.7,
    "contradicts":    1.1,
    "supersedes":     1.1,
    "alternative_to": 1.1,
    "derived_from":   1.0,
}
# Manual edges (from `edges` table, user-curated) get a uniform
# weight regardless of free-text relation column.
MANUAL_EDGE_WEIGHT = 1.2

# Type-weighted curve bonuses (multiplicative on top of relation_weight)
MANUAL_EDGE_BONUS = 1.5
AUTO_TYPE_BONUS = {
    "contradicts":    1.4,
    "supersedes":     1.4,
    "alternative_to": 1.4,
    "implements":     1.0,
    "contains":       1.0,
    "depends_on":     1.0,
    "derived_from":   1.0,
    "mentions":       0.6,
    "references":     0.6,
    "relates_to":     0.6,
}
_AUTO_TYPE_BONUS_DEFAULT = 0.8  # for any unknown rel_type


# ─── Public API ───────────────────────────────────────────────────


def graph_proximity(
    db,
    query: str,
    *,
    limit: int = 50,
    depth_cap: int = DEFAULT_DEPTH_CAP,
    rank_curve: str | None = None,
) -> list[dict]:
    """Return ranked memory candidates from the entity graph.

    Returns list of `{memory_id, rank, seed_entity_id, distance}`
    dicts, sorted by `rank` ascending then `memory_id` (determinism
    for A6 cmp byte-identity).

    Args:
        db: sqlite3 Connection (file-backed; worker not needed).
        query: the search query string. Tokens are normalized and
            looked up against `entities.name_normalized` AND
            `mentions.surface_form LIKE '%token%'`.
        limit: max candidates returned. Channel internally over-
            fetches `limit × _RESULT_CAP_MULTIPLIER`.
        depth_cap: BFS depth cap. Defaults to 2 per ADR-004.
        rank_curve: override the rank curve. None → env var or
            module default.

    Raises:
        ValueError: SAGE_GRAPH_RANK_CURVE is set to an unknown value
            (fail-fast for ablation safety per spec rev 5).
    """
    # Empty-table fast-path (free-path floor invariant; A2)
    exists_row = db.execute(
        "SELECT EXISTS(SELECT 1 FROM entities)"
    ).fetchone()
    if not exists_row[0]:
        return []

    curve = _resolve_curve(rank_curve)
    seed_entities = _seed_entities_from_query(db, query)
    if not seed_entities:
        return []

    # visited maps memory_id → (distance, last_hop_source_table,
    # last_hop_relation_type, seed_entity_id). The hop metadata
    # is what the type-weighted curve dispatches on.
    visited: dict[str, _HopRecord] = {}

    # Seed memories at distance 0 — found via mentions of any seed entity
    for seed_eid in seed_entities:
        for memory_id in _memories_mentioning(db, seed_eid):
            _consider_candidate(
                visited, memory_id,
                _HopRecord(
                    distance=0,
                    source_table="seed",
                    relation_type=None,
                    seed_entity_id=seed_eid,
                ),
                curve,
            )

    # BFS expansion: hop types 1 (entity-mediated) and 2 (memory-direct)
    for depth in range(1, depth_cap + 1):
        frontier_inputs = [
            mid for mid, rec in visited.items() if rec.distance == depth - 1
        ]
        if not frontier_inputs:
            break

        # Hop type 1: entity-mediated (mentions → relations → mentions)
        for memory_id in frontier_inputs:
            origin_seed = visited[memory_id].seed_entity_id
            for neighbor in _entity_mediated_neighbors(db, memory_id):
                _consider_candidate(
                    visited, neighbor.memory_id,
                    _HopRecord(
                        distance=depth,
                        source_table="relations",
                        relation_type=neighbor.relation_type,
                        seed_entity_id=origin_seed,
                    ),
                    curve,
                )

        # Hop type 2: memory-direct (edges, outbound-only)
        for memory_id in frontier_inputs:
            origin_seed = visited[memory_id].seed_entity_id
            for neighbor in _memory_direct_neighbors(db, memory_id):
                _consider_candidate(
                    visited, neighbor.memory_id,
                    _HopRecord(
                        distance=depth,
                        source_table="edges",
                        relation_type=neighbor.relation_type,
                        seed_entity_id=origin_seed,
                    ),
                    curve,
                )

    # Orphan filter (A13): INNER JOIN memories on the candidate set
    if not visited:
        return []
    placeholders = ",".join("?" * len(visited))
    alive_rows = db.execute(
        f"SELECT id FROM memories WHERE id IN ({placeholders})",
        list(visited.keys()),
    ).fetchall()
    alive_ids = {r["id"] for r in alive_rows}

    # Materialize, drop orphans, sort deterministically
    results = [
        {
            "memory_id": mid,
            "rank": _rank_for(rec, curve),
            "seed_entity_id": rec.seed_entity_id,
            "distance": rec.distance,
        }
        for mid, rec in visited.items()
        if mid in alive_ids
    ]
    # Sort: rank ascending, then memory_id ascending (determinism for cmp)
    results.sort(key=lambda r: (r["rank"], r["memory_id"]))

    # Apply channel cap (popular-entity blow-up bound per spec)
    cap = limit * _RESULT_CAP_MULTIPLIER
    if len(results) > cap:
        logger.warning(
            "graph_channel: query produced %d candidates; capping at %d",
            len(results), cap,
        )
        results = results[:cap]
    return results[:limit]


# ─── Internal types + helpers ─────────────────────────────────────


class _HopRecord:
    """Visited-set value: tracks hop metadata for rank computation."""
    __slots__ = ("distance", "source_table", "relation_type",
                 "seed_entity_id")

    def __init__(
        self, *, distance: int,
        source_table: Literal["seed", "relations", "edges"],
        relation_type: str | None,
        seed_entity_id: str,
    ) -> None:
        self.distance = distance
        self.source_table = source_table
        self.relation_type = relation_type
        self.seed_entity_id = seed_entity_id


class _Neighbor:
    __slots__ = ("memory_id", "relation_type")

    def __init__(self, memory_id: str, relation_type: str) -> None:
        self.memory_id = memory_id
        self.relation_type = relation_type


def _resolve_curve(override: str | None) -> str:
    """Resolve the active rank curve. Override > env > default.

    Raises ValueError on unknown value (fail-fast per spec).
    """
    raw = override or os.environ.get("SAGE_GRAPH_RANK_CURVE") \
        or DEFAULT_RANK_CURVE
    if raw not in _VALID_CURVES:
        raise ValueError(
            f"SAGE_GRAPH_RANK_CURVE={raw!r} is not a valid curve. "
            f"Expected one of {_VALID_CURVES!r}."
        )
    return raw


def _normalize_token(token: str) -> str:
    """Mirror extractor.normalize_name — keep dedup keys consistent."""
    text = token.lower().strip()
    text = re.sub(r"['’]", "", text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _seed_entities_from_query(db, query: str) -> list[str]:
    """Find seed entity IDs from query tokens.

    Two seeding paths combined:
    (a) name_normalized matches a token from the query
    (b) any mentions.surface_form contains a token from the query
        (LIKE %token%, case-insensitive)

    Returns canonical IDs (resolved via COALESCE).
    """
    tokens = [t for t in re.split(r"\s+", query.strip()) if t]
    normalized_tokens = [_normalize_token(t) for t in tokens if t]
    normalized_tokens = [t for t in normalized_tokens if t]
    if not normalized_tokens:
        return []

    seen: set[str] = set()
    # Path (a): exact name_normalized match
    placeholders = ",".join("?" * len(normalized_tokens))
    name_rows = db.execute(
        f"SELECT COALESCE(canonical_id, id) AS eid "
        f"FROM entities WHERE name_normalized IN ({placeholders})",
        normalized_tokens,
    ).fetchall()
    for row in name_rows:
        seen.add(row["eid"])

    # Path (b): surface_form LIKE %token%
    for token in normalized_tokens:
        like_rows = db.execute(
            "SELECT DISTINCT COALESCE(e.canonical_id, e.id) AS eid "
            "FROM mentions m "
            "JOIN entities e ON e.id = m.entity_id "
            "WHERE LOWER(m.surface_form) LIKE '%' || ? || '%'",
            (token,),
        ).fetchall()
        for row in like_rows:
            seen.add(row["eid"])

    return sorted(seen)  # deterministic order for downstream determinism


def _memories_mentioning(db, entity_id: str) -> list[str]:
    """Memories mentioning a given (canonical) entity — resolves
    through canonical_id so any merged-into-this-canonical entities
    surface their mentions too."""
    rows = db.execute(
        "SELECT DISTINCT m.memory_id "
        "FROM mentions m "
        "JOIN entities e ON e.id = m.entity_id "
        "WHERE COALESCE(e.canonical_id, e.id) = ? "
        "ORDER BY m.memory_id",
        (entity_id,),
    ).fetchall()
    return [r["memory_id"] for r in rows]


def _entity_mediated_neighbors(db, memory_id: str) -> list[_Neighbor]:
    """For a given memory, find neighbor memories reached by:
      memory_id → (mentions) → entities → (relations) → entities
                → (mentions) → neighbor memory_id.
    Each neighbor is tagged with the relation_type that bridged
    the entity hop.

    **Backward-compat OR clauses (canonical_id vs id):** the JOINs
    `relations` and `mentions` predicates both look for entity_id
    matches against BOTH `COALESCE(canonical_id, id)` and `id`.

    Coverage matrix:
    - **M3a state (canonical_id always NULL):** COALESCE returns
      `id`, so the two predicates collapse to the same value — the
      OR is redundant but harmless.
    - **Post-M5 — seed entity merged later:** if BFS reaches an
      entity X via M_seed's mention (m_source.entity_id = X.id),
      and M5 later sets X.canonical_id = Y, the OR catches BOTH
      relations written for X (pre-merge, source = X.id) AND
      relations written for Y (post-merge, source = Y.id).
    - **Post-M5 — DIFFERENT entity merged INTO the seed's canonical:**
      NOT covered by this OR. To find legacy relations written for
      entity B where B.canonical_id = seed_entity.id, the SQL would
      need to expand to the full "canonical group" (`SELECT id FROM
      entities WHERE COALESCE(canonical_id, id) = seed.id`). That's
      M5's responsibility — either rewrite mentions/relations rows
      at dedup time (eager) OR extend this query (lazy). Out of
      scope for M3b.

    This implements the spec rev 5 "canonical resolution on read"
    pattern without requiring M5 to rewrite every mentions/relations
    row at canonicalization time for the common case (entity-self-
    merged-later).
    """
    rows = db.execute(
        """
        SELECT DISTINCT
            m_target.memory_id AS memory_id,
            r.relation_type AS relation_type
          FROM mentions m_source
          JOIN entities e_source ON e_source.id = m_source.entity_id
          JOIN relations r
            ON r.source_entity_id = COALESCE(e_source.canonical_id, e_source.id)
            OR r.source_entity_id = e_source.id
          JOIN entities e_target ON e_target.id = r.target_entity_id
          JOIN mentions m_target
            ON m_target.entity_id = COALESCE(e_target.canonical_id, e_target.id)
            OR m_target.entity_id = e_target.id
         WHERE m_source.memory_id = ?
           AND m_target.memory_id != ?
         ORDER BY m_target.memory_id, r.relation_type
        """,
        (memory_id, memory_id),
    ).fetchall()
    return [_Neighbor(r["memory_id"], r["relation_type"]) for r in rows]


def _memory_direct_neighbors(db, memory_id: str) -> list[_Neighbor]:
    """For a given memory, find direct outbound edge neighbors via
    the `edges` table (memory→memory, user-curated). Outbound-only:
    `edges.source_id = memory_id` (NOT bidirectional)."""
    rows = db.execute(
        "SELECT DISTINCT target_id AS memory_id, relation "
        "FROM edges WHERE source_id = ? "
        "ORDER BY target_id",
        (memory_id,),
    ).fetchall()
    return [_Neighbor(r["memory_id"], r["relation"]) for r in rows]


def _consider_candidate(
    visited: dict, memory_id: str, candidate: _HopRecord, curve: str,
) -> None:
    """Update visited dict with the candidate iff it strictly improves.

    Strict less-than means iteration order is irrelevant on ties —
    first-seen value persists (deterministic tie-break per spec).
    """
    existing = visited.get(memory_id)
    if existing is None:
        visited[memory_id] = candidate
        return
    # Different depths: keep the shallower (matches ADR-004 visited semantics)
    if candidate.distance < existing.distance:
        visited[memory_id] = candidate
        return
    if candidate.distance > existing.distance:
        return
    # Same depth: compare ranks; strictly lower wins
    if _rank_for(candidate, curve) < _rank_for(existing, curve):
        visited[memory_id] = candidate


def _rank_for(rec: _HopRecord, curve: str) -> float:
    """Compute rank for a hop record under the active curve."""
    d = rec.distance
    if d == 0:
        # Seed memories: rank = 1 across all curves
        return 1.0
    w = _weight_for(rec)
    if curve == "linear":
        return 1.0 + d * (1.0 / max(w, EPS))
    if curve == "harmonic":
        # H(d) = sum_{i=1..d} 1/i
        h = sum(1.0 / i for i in range(1, d + 1))
        return 1.0 + h * (1.0 / max(w, EPS))
    if curve == "type-weighted":
        if rec.source_table == "edges":
            bonus = MANUAL_EDGE_BONUS
        else:
            bonus = AUTO_TYPE_BONUS.get(
                rec.relation_type, _AUTO_TYPE_BONUS_DEFAULT,
            )
        return 1.0 + d * (1.0 / max(w * bonus, EPS))
    # _resolve_curve already validates; unreachable.
    raise ValueError(f"unknown rank curve: {curve}")


def _weight_for(rec: _HopRecord) -> float:
    """Base relation weight for the hop (before type-weighted bonus)."""
    if rec.source_table == "edges":
        return MANUAL_EDGE_WEIGHT
    if rec.source_table == "relations":
        return RELATION_WEIGHTS.get(rec.relation_type, 0.7)
    # seed (distance 0) — weight irrelevant; rank short-circuits to 1
    return 1.0
