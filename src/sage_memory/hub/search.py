"""M2.3 — hub fan-out search.

Per ADR-008 §"Federation semantics": opens each ``searchable: true``
project DB read-only + the global DB, runs the FTS5 primitive per-DB,
merges via the shared ``search.rrf_fuse`` helper, and returns the
top-K records with a ``source`` field set to the hub project name
(or ``"global"`` for the global DB).

Single source of truth for RRF: ``sage_memory.search.rrf_fuse``. Tests
verify module-source identity to guard against accidental copy-paste
in a future refactor (auto-review MINOR-substantive #6).

**Pipeline descope (M2 review C2):** v1 uses FTS5 only — no vector /
graph / LLM stages on the cross-project path. Per-project
``search.search()`` still runs the full pipeline for in-project
queries. The plan M2.3 originally called for "the existing search.py
pipeline per-DB in parallel via asyncio.gather"; the descope to
FTS-only-sequential is documented in decisions.md (2026-05-24 entry)
and tracked for future work. The shared ``rrf_fuse`` keeps the merge
algorithm single-sourced even while the channel set feeding into
it differs between in-project and cross-project paths.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from .. import search as _search
from ..db import get_global_db_path
from ..search import rrf_fuse  # re-export for the M2.3 module-identity test
from . import config as _hub_config

logger = logging.getLogger("sage_memory.hub.search")


# Per-DB FTS top-K cap before RRF fuses. Larger than the user-facing
# limit so the merge has enough candidates to rank meaningfully.
_PER_DB_FTS_LIMIT = 50


__all__ = ["fan_out_search", "rrf_fuse"]


def fan_out_search(
    query: str,
    *,
    hub_path: Path | None = None,
    hub_projects: list[str] | None = None,
    limit: int = 5,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Search across hub-registered projects + global, merge via RRF.

    Args:
      query: free-text FTS5 query.
      hub_path: override the default ``~/.sage-hub.yaml`` location.
      hub_projects: when not None, restricts the fan-out to the named
        projects (must be a subset of ``searchable: true`` projects
        in the hub config). When None, fans out over ALL searchable
        projects.
      limit: top-K results to return.
      tags: hard tag filter (AND logic, all must match).

    Returns:
      ``{results: [...], total: int, query: str, sources: [str, ...]}``
      where each result carries a ``source`` field set to the hub
      project name (or ``"global"``).
    """
    # M2 review M4: hub_projects=[] is a malformed call — distinct
    # from hub_projects=None (which means "all searchable"). Reject
    # explicitly so callers can't get a silently empty envelope.
    if hub_projects is not None and len(hub_projects) == 0:
        raise ValueError(
            "hub_projects must be a non-empty list when provided "
            "(pass None to fan out over all searchable projects)"
        )

    cfg_path = hub_path or _hub_config.DEFAULT_HUB_PATH
    if cfg_path.exists():
        cfg = _hub_config.load(cfg_path)
        targets = [p for p in cfg.projects if p.searchable]
        if hub_projects is not None:
            requested = set(hub_projects)
            unknown = requested - {p.name for p in targets}
            if unknown:
                raise ValueError(
                    f"hub project(s) not registered or not searchable: "
                    f"{sorted(unknown)}"
                )
            targets = [p for p in targets if p.name in requested]
    else:
        targets = []

    sources: list[tuple[str, Path]] = [
        (p.name, p.path / ".sage-memory" / "memory.db") for p in targets
    ]
    # Always include global. The canonical global DB path comes from
    # db.get_global_db_path() — sourcing it here keeps the hub
    # federation in sync with whatever the rest of sage-memory uses
    # as the global DB filename (M2 review C1 — fixed-string drift).
    global_db = get_global_db_path()
    if global_db.exists():
        sources.append(("global", global_db))

    # Per-source FTS lookup. Each entry: (source_name, ranked_id_list,
    # id → row cache). Missing DBs contribute nothing.
    per_source_ranked: list[tuple[str, list[str], dict[str, dict]]] = []
    for source_name, db_path in sources:
        if not db_path.exists():
            continue
        ids, cache = _fts_lookup(db_path, query, tags)
        if ids:
            per_source_ranked.append((source_name, ids, cache))

    if not per_source_ranked:
        return {
            "results": [],
            "total": 0,
            "query": query,
            "sources": [name for name, _ in sources],
        }

    # Fuse via the shared RRF helper. Items are (source_name, memory_id)
    # tuples to keep results disambiguated across DBs that happen to
    # share a memory id (vanishingly unlikely with random uuid hex,
    # but the tuple is the correct primitive).
    ranked_lists = [
        [(name, mid) for mid in ids]
        for name, ids, _ in per_source_ranked
    ]
    scored = rrf_fuse(ranked_lists)
    ordered = sorted(scored.items(), key=lambda kv: -kv[1])

    caches: dict[str, dict[str, dict]] = {
        name: cache for name, _, cache in per_source_ranked
    }

    results: list[dict] = []
    for (source_name, memory_id), score in ordered[:limit]:
        row = caches[source_name].get(memory_id)
        if row is None:
            continue
        record = dict(row)
        record["source"] = source_name
        record["rrf_score"] = score
        results.append(record)

    return {
        "results": results,
        "total": len(scored),
        "query": query,
        "sources": [name for name, _ in sources],
    }


def _fts_lookup(
    db_path: Path,
    query: str,
    tags: list[str] | None,
) -> tuple[list[str], dict[str, dict]]:
    """Open the project DB read-only and run search.py's FTS primitive."""
    # ``file:<path>?mode=ro`` URI form gives us read-only without
    # changing sage-memory's global connection cache.
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        logger.warning(
            "hub.search: could not open %s read-only: %s", db_path, exc,
        )
        return [], {}
    conn.row_factory = sqlite3.Row
    # M2 review M1: dropped sqlite_vec load — this path is FTS-only
    # per the M2.3 descope (see decisions.md). When the fan-out
    # graduates to the full pipeline in a future milestone, the vec
    # extension load returns here, gated by a quality check.
    try:
        tag_where, tag_params = _search._build_tag_filter(tags)
        return _search._fts_search(
            conn, query, _PER_DB_FTS_LIMIT, tag_where, tag_params,
        )
    finally:
        conn.close()
