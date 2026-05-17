"""M5 T3 — Entity dedup algorithm (shared between CLI --sync + worker).

Per ADR-003 §Dedup:
  1. SELECT entities WHERE canonical_id IS NULL AND mention_count >= 2
  2. Group by type
  3. For each pair within a group, compute cosine similarity on the
     entity name's embedding
  4. For pairs with cosine > threshold (default 0.9):
       if llm_confirm: ask LLM "Are these the same entity?" → yes/no
       else (stub mode): record without confirming
  5. On yes: UPDATE entities SET canonical_id = <other_id>

The `--provider stub` CLI path calls this with `llm_confirm=False`
to produce a cost-estimation report (cosine pre-filter only, no LLM).

Public entry points:
  - `run_pass(db, *, llm_confirm=True, log_decisions=False) -> dict`
    Used by `sage-memory dedup --sync` AND `worker._do_dedup`.

Returns `{pairs_considered, pairs_confirmed, pairs_merged,
cost_estimate_usd}` summary dict.

Failure handling: LLM exceptions propagate to caller. Caller decides
whether to mark task failed (worker) or exit nonzero (CLI sync).
"""

from __future__ import annotations

import logging
import math
import sys
import time

from . import embedder as _embedder
from . import llm as _llm


logger = logging.getLogger("sage_memory.dedup")


# ADR-003 §Dedup: cosine pre-filter threshold; tunable via spec.
_DEFAULT_COSINE_THRESHOLD = 0.9
# gpt-4o-mini approx cost per dedup confirm (one short prompt + ~5 tokens).
_LLM_COST_PER_PAIR_USD = 0.0002


def run_pass(
    db,
    *,
    llm_confirm: bool = True,
    log_decisions: bool = False,
    cosine_threshold: float = _DEFAULT_COSINE_THRESHOLD,
) -> dict:
    """Execute one dedup pass.

    Args:
        db: open sqlite3 connection with row_factory set.
        llm_confirm: when False, runs the cosine pre-filter only
            (produces candidate pairs report; no UPDATEs, no LLM).
        log_decisions: when True, prints per-pair decisions to stdout
            (used by `--sync` CLI for operator feedback).
        cosine_threshold: pairs with cosine ≥ this are LLM-confirmed.

    Returns:
        {
            "pairs_considered": int (cosine pre-filter hits),
            "pairs_confirmed": int (LLM-confirmed merges, or all
                                    candidates when llm_confirm=False),
            "pairs_merged": int (actual UPDATE entities count),
            "cost_estimate_usd": float (pairs_considered × per-pair cost),
        }

    Raises: any exception from the LLM path propagates to caller.
    """
    candidates = _candidate_entities(db)
    by_type: dict[str, list[dict]] = {}
    for e in candidates:
        by_type.setdefault(e["type"], []).append(e)

    embedder = _embedder.get_embedder()
    pairs_considered = 0
    pairs_confirmed = 0
    pairs_merged = 0

    for etype, group in by_type.items():
        if len(group) < 2:
            continue
        # Embed all entity names in this group once.
        embeddings: list[list[float]] = [
            embedder.embed(e["name"]) for e in group
        ]
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                cos = _cosine(embeddings[i], embeddings[j])
                if cos < cosine_threshold:
                    continue
                pairs_considered += 1
                a, b = group[i], group[j]
                if log_decisions:
                    print(
                        f"  pair (cos={cos:.3f}): "
                        f"{a['name']!r} <> {b['name']!r}",
                    )
                if not llm_confirm:
                    # Stub mode: count as confirmed-but-not-merged.
                    pairs_confirmed += 1
                    continue
                # LLM confirm
                same = _llm_confirm_same(a, b)
                if same:
                    pairs_confirmed += 1
                    if _merge_pair(db, kept=a, merged=b):
                        pairs_merged += 1
                        if log_decisions:
                            print(
                                f"    merged: {b['name']!r} "
                                f"→ canonical={a['name']!r}",
                            )
                else:
                    if log_decisions:
                        print(
                            f"    skipped: LLM said not the same"
                        )

    cost = pairs_considered * _LLM_COST_PER_PAIR_USD
    return {
        "pairs_considered": pairs_considered,
        "pairs_confirmed": pairs_confirmed,
        "pairs_merged": pairs_merged,
        "cost_estimate_usd": cost,
    }


# ─── Helpers ──────────────────────────────────────────────────────


def _candidate_entities(db) -> list[dict]:
    return [
        dict(r) for r in db.execute(
            "SELECT id, name, type FROM entities "
            "WHERE canonical_id IS NULL AND mention_count >= 2 "
            "ORDER BY type, name"
        )
    ]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _llm_confirm_same(a: dict, b: dict) -> bool:
    """Ask the LLM if two entities are the same. Uses the shared
    `_call_llm` helper from llm.py."""
    prompt = (
        "You are a strict entity-dedup judge. Decide if the two "
        f"{a['type']} names refer to the SAME real-world entity. "
        "Return JSON only: {\"same\": true} or {\"same\": false}. "
        "If unsure, return false."
    )
    user_content = (
        f"<entity_a>{a['name']}</entity_a>\n"
        f"<entity_b>{b['name']}</entity_b>"
    )
    result = _llm._call_llm(
        system_prompt=prompt,
        user_content=user_content,
        max_tokens=64,
        timeout_s=_llm._HTTP_TIMEOUT,
    )
    if isinstance(result, dict):
        return bool(result.get("same", False))
    return False


def _merge_pair(db, *, kept: dict, merged: dict) -> bool:
    """UPDATE entities SET canonical_id = <kept.id> WHERE id = merged.id.
    Returns True iff a row was updated.

    Does NOT commit — caller controls transaction boundary (CLI --sync
    wraps in BEGIN IMMEDIATE; worker's _mark_done commits after dispatch).
    """
    cur = db.execute(
        "UPDATE entities SET canonical_id = ?, updated_at = ? "
        "WHERE id = ?",
        (kept["id"], time.time(), merged["id"]),
    )
    return cur.rowcount > 0
