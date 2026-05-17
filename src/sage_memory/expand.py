"""M4 — query expansion with strong-signal short-circuit.

Public entry point: `expand_query(query, seed_bm25_results)`.

Two paths:
  1. Strong-signal short-circuit (ADR-004 §"Strong-signal short-
     circuit"): when FTS5 bm25 already returns a confident match,
     return the no-expansion fallback without calling the LLM.
  2. LLM-call path: invoke `llm.expand_query_variants()` to produce
     `{lex, vec, hyde}` variants. Validate and clean.

Free-path floor: when no LLM key is configured, both paths return
the no-expansion fallback silently. This preserves M3b byte-identity
on the no-key path (A10 invariant).

LLM-failure fallback (A3): any exception from the LLM-call path
returns the no-expansion fallback with a WARNING.

Env-var overrides (read at import; reload required to change):
  SAGE_EXPAND_TOP1_NORM  default 0.4   (top1 normalized threshold)
  SAGE_EXPAND_TOP1_RATIO default 2.0   (top1/top2 ratio threshold)

Plan: .sage/work/20260516-retrieval-upgrade/M4/plan.md §T1
Spec:  .sage/work/20260516-retrieval-upgrade/M4/spec.md A1-A3
"""

from __future__ import annotations

import json
import logging
import os

import httpx

from . import llm


logger = logging.getLogger("sage_memory.expand")


# ─── Tunables (env-var overrides; reload required) ────────────────

_TOP1_NORM_THRESHOLD = float(
    os.environ.get("SAGE_EXPAND_TOP1_NORM", "0.4")
)
_TOP1_RATIO_THRESHOLD = float(
    os.environ.get("SAGE_EXPAND_TOP1_RATIO", "2.0")
)


# ─── Public API ───────────────────────────────────────────────────


def expand_query(
    query: str,
    seed_bm25_results: list[tuple[str, float]],
) -> dict:
    """Produce {lex, vec, hyde} query variants for retrieval expansion.

    Args:
        query: the user's original search query.
        seed_bm25_results: pre-computed [(memory_id, raw_bm25_score)]
            from a small FTS5 probe against the same corpus that the
            downstream retrieval will hit. Used for the strong-signal
            decision. An empty list is acceptable (no short-circuit
            possible → LLM-call path if configured, else free-path
            floor).

    Returns:
        {"lex": list[str], "vec": str, "hyde": str | None}

    Never raises — all LLM failures degrade to the no-expansion
    fallback. Search continues unimpaired.
    """
    if _is_strong_signal(seed_bm25_results):
        top1_n, top2_n = _top_norms(seed_bm25_results)
        logger.debug(
            "expand: strong-signal short-circuit "
            "(top1_norm=%.4f, top2_norm=%.4f)", top1_n, top2_n,
        )
        return _no_expansion(query)

    if not llm.is_configured():
        # Free-path floor: silent skip (no WARN).
        return _no_expansion(query)

    try:
        raw = llm.expand_query_variants(query)
    except (
        llm.LlmNotConfiguredError,
        httpx.TimeoutException,
        httpx.HTTPError,
        json.JSONDecodeError,
        ValueError,
    ) as e:
        # A3: graceful degrade with explicit signal.
        logger.warning(
            "expand: LLM failure (%s); falling back to no-expansion "
            "for query=%r", type(e).__name__, query[:80],
        )
        return _no_expansion(query)

    return _validate_variants(raw, query)


# ─── Strong-signal predicate ──────────────────────────────────────


def _normalize_bm25(raw_score: float) -> float:
    """ADR-004 normalization: abs(s)/(1+abs(s)) ∈ [0, 1)."""
    s = abs(raw_score)
    return s / (1.0 + s)


def _top_norms(
    seed_results: list[tuple[str, float]],
) -> tuple[float, float]:
    """Return (top1_norm, top2_norm). top2 = 0.0 when no second hit."""
    if not seed_results:
        return 0.0, 0.0
    top1 = _normalize_bm25(seed_results[0][1])
    top2 = (
        _normalize_bm25(seed_results[1][1])
        if len(seed_results) > 1 else 0.0
    )
    return top1, top2


def _is_strong_signal(seed_results: list[tuple[str, float]]) -> bool:
    """ADR-004 strong-signal predicate.

    Fires when top1 is confident AND (single hit OR top1 is 2x top2).
    Both gates must clear at the env-tunable thresholds.
    """
    top1, top2 = _top_norms(seed_results)
    if top1 < _TOP1_NORM_THRESHOLD:
        return False
    if top2 == 0.0:
        # Single hit (or empty top2) — by definition strong if top1
        # cleared the absolute gate above.
        return True
    return top1 >= _TOP1_RATIO_THRESHOLD * top2


# ─── Variant validation ───────────────────────────────────────────


def _no_expansion(query: str) -> dict:
    return {"lex": [query], "vec": query, "hyde": None}


def _validate_variants(raw: dict, query: str) -> dict:
    """Coerce LLM output to the validated {lex, vec, hyde} schema.

    Per spec A2:
      - lex: drop empty strings; backfill with [query] if list empty.
      - vec: empty string → fall back to query.
      - hyde: missing or empty → None.

    Malformed responses fall under "graceful degradation" — no
    WARNING is logged (the LLM is honoring the contract, it just
    returned weak content). True LLM failures (exceptions) go through
    the A3 path and DO log WARNING.
    """
    if not isinstance(raw, dict):
        # LLM contract violation (returned a list, etc.). Treat as
        # no-expansion. No WARN — caller-facing behavior unchanged.
        return _no_expansion(query)

    lex_raw = raw.get("lex", [])
    if not isinstance(lex_raw, list):
        lex = [query]
    else:
        lex = [s for s in lex_raw
               if isinstance(s, str) and s.strip()]
        if not lex:
            lex = [query]

    vec_raw = raw.get("vec", "")
    if not isinstance(vec_raw, str) or not vec_raw.strip():
        vec = query
    else:
        vec = vec_raw

    hyde_raw = raw.get("hyde")
    if not isinstance(hyde_raw, str) or not hyde_raw.strip():
        hyde = None
    else:
        hyde = hyde_raw

    return {"lex": lex, "vec": vec, "hyde": hyde}
