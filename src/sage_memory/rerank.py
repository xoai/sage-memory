"""M4 — LLM rerank stage.

Public entry point: `rerank(query, candidates, *, top_k=None)`.

Calls the LLM to score the top-K candidates against the query.
Returns the input candidate list augmented with `llm_score: float |
None` on each entry. Position-blend math (combining `llm_score` with
the existing `rrf_score`) lives in `search.py` — this module ONLY
handles the LLM call + score parse + per-candidate validation.

Failure handling per SAGE_RERANK_FAILURE_VISIBILITY env var:
  warn   (default): WARN log + all candidates llm_score=None
  silent           : no log + all candidates llm_score=None
  error            : re-raise the underlying exception

Per-candidate truncation: content is capped at _MAX_TOKENS//top_k*4
chars before being sent to the LLM. Defensive: if the constructed
prompt still exceeds _MAX_TOKENS*4 chars after truncation, drop to
RRF-only fallback with WARN.

ID validation against the input candidate set:
  (i)   LLM emits ID not in input → WARN naming spurious IDs + drop
  (ii)  LLM omits real ID → entry retains llm_score=None silently
  (iii) LLM emits duplicate ID → keep FIRST occurrence, drop rest

Major #6: response shape validation
  - LLM returns non-list (e.g., {"error": ...}) → failure path
  - LLM returns ID as string → coerce with int(), drop on failure

Env-var overrides (read at import; reload required to change):
  SAGE_RERANK_TOP_K               default 15
  SAGE_RERANK_FAILURE_VISIBILITY  default "warn" (warn|silent|error)

Free-path floor: when llm.is_configured() is False, all entries get
llm_score=None silently. Preserves M3b byte-identity on no-key path.

Prompt-injection defense: query + candidate content are wrapped in
delimiters in `llm.rerank_candidates()` (`<query>...</query>`,
`<candidate id="...">...</candidate>`). System prompt instructs the
model to treat wrapped content as DATA.

Plan: .sage/work/20260516-retrieval-upgrade/M4/plan.md §T2
Spec:  .sage/work/20260516-retrieval-upgrade/M4/spec.md A4-A6, A14
"""

from __future__ import annotations

import json
import logging
import os

import httpx

from . import llm


logger = logging.getLogger("sage_memory.rerank")


# ─── Tunables ─────────────────────────────────────────────────────

_TOP_K_DEFAULT = int(os.environ.get("SAGE_RERANK_TOP_K", "15"))

_FAILURE_VISIBILITY = os.environ.get(
    "SAGE_RERANK_FAILURE_VISIBILITY", "warn"
)
_VALID_VISIBILITY = {"warn", "silent", "error"}
if _FAILURE_VISIBILITY not in _VALID_VISIBILITY:
    raise ValueError(
        f"SAGE_RERANK_FAILURE_VISIBILITY={_FAILURE_VISIBILITY!r} is "
        f"invalid. Must be one of {sorted(_VALID_VISIBILITY)}."
    )

# Conservative prompt cap for both Haiku and gpt-4o-mini (both ≥8k
# in JSON mode). Chars × 0.25 ≈ tokens for ASCII prose (A6 spec).
_MAX_TOKENS = 8192


# ─── Public API ───────────────────────────────────────────────────


def rerank(
    query: str,
    candidates: list[dict],
    *,
    top_k: int | None = None,
) -> list[dict]:
    """Score `candidates` against `query` and attach `llm_score`.

    Args:
        query: the user search query (will be wrapped in delimiters
            by llm.rerank_candidates).
        candidates: list of dicts. Each MUST have an int `id` and a
            str `content` field. Other fields pass through untouched.
        top_k: max number of candidates sent to the LLM. Defaults to
            _TOP_K_DEFAULT. The caller is expected to pre-trim, but
            if more are passed we send up to top_k.

    Returns: the input list (same length, same order) with each entry
    augmented with `llm_score: float | None`. Never raises unless
    SAGE_RERANK_FAILURE_VISIBILITY=error AND the LLM call raises.
    """
    if top_k is None:
        top_k = _TOP_K_DEFAULT

    # A9 sub-case: trivially-small inputs short-circuit.
    if len(candidates) < 2:
        return candidates

    # Free-path floor: no key → silent skip.
    if not llm.is_configured():
        return [{**c, "llm_score": None} for c in candidates]

    # Slice to top_k for the LLM call. Candidates beyond top_k get
    # llm_score=None.
    head = candidates[:top_k]
    tail = candidates[top_k:]

    # A6: per-candidate truncation. Build truncated COPIES — do not
    # mutate caller's input.
    per_cap = max(1, _MAX_TOKENS // max(1, top_k) * 4)
    truncated = [
        {
            "id": c["id"],
            "content": str(c.get("content", ""))[:per_cap],
        }
        for c in head
    ]

    # A6 defensive over-budget guard. Should be rare given the per-
    # candidate cap, but a long query + many candidates can still
    # blow the prompt budget.
    total_chars = (
        len(query) + sum(len(c["content"]) for c in truncated)
        + len(head) * 40  # scaffolding (delimiters, etc.)
    )
    if total_chars > _MAX_TOKENS * 4:
        logger.warning(
            "rerank: prompt over-budget after truncation "
            "(%d > %d chars); falling back to RRF order",
            total_chars, _MAX_TOKENS * 4,
        )
        return [{**c, "llm_score": None} for c in candidates]

    try:
        raw_scores = llm.rerank_candidates(
            query, truncated, top_k=top_k,
        )
    except (
        llm.LlmNotConfiguredError,
        httpx.TimeoutException,
        httpx.HTTPError,
        json.JSONDecodeError,
        ValueError,
    ) as e:
        return _handle_llm_failure(e, candidates)

    return _apply_scores(head, tail, raw_scores)


# ─── Failure / response handling ──────────────────────────────────


def _handle_llm_failure(
    exc: Exception, candidates: list[dict],
) -> list[dict]:
    if _FAILURE_VISIBILITY == "error":
        raise exc
    if _FAILURE_VISIBILITY == "warn":
        logger.warning(
            "rerank: LLM call failed (%s: %s); falling back to RRF "
            "order with llm_score=None",
            type(exc).__name__, exc,
        )
    return [{**c, "llm_score": None} for c in candidates]


def _apply_scores(
    head: list[dict],
    tail: list[dict],
    raw_scores,
) -> list[dict]:
    """Map LLM-returned scores onto head candidates; tail keeps None.

    Implements A14 sub-cases (i/ii/iii) + Major #6 (non-list, string IDs).
    """
    head_ids = {c["id"] for c in head}

    # Major #6 non-list: failure path.
    if not isinstance(raw_scores, list):
        logger.warning(
            "rerank: LLM returned non-list (type=%s); falling back to "
            "RRF order with llm_score=None", type(raw_scores).__name__,
        )
        return [
            {**c, "llm_score": None} for c in (head + tail)
        ]

    score_by_id: dict[int, float] = {}
    spurious_ids: list = []

    for entry in raw_scores:
        if not isinstance(entry, dict):
            # Defensive: skip malformed entries silently.
            continue
        raw_id = entry.get("id")
        raw_score = entry.get("score")

        # Major #6: id coercion. Accept int directly, else try int().
        # Drop silently on coerce failure (defensive parse hygiene).
        if isinstance(raw_id, int) and not isinstance(raw_id, bool):
            cid = raw_id
        else:
            try:
                cid = int(raw_id)  # handles "1" → 1
            except (TypeError, ValueError):
                continue

        # A14 (i): hallucinated ID.
        if cid not in head_ids:
            spurious_ids.append(cid)
            continue

        # A14 (iii): duplicate ID → keep first.
        if cid in score_by_id:
            continue

        # Score validation: clamp to [0, 1].
        try:
            sc = float(raw_score)
        except (TypeError, ValueError):
            continue
        score_by_id[cid] = max(0.0, min(1.0, sc))

    if spurious_ids:
        logger.warning(
            "rerank: LLM emitted IDs not in input (dropped): %s",
            spurious_ids,
        )

    # A14 (ii): missing IDs retain llm_score=None silently.
    out_head = [
        {**c, "llm_score": score_by_id.get(c["id"])}
        for c in head
    ]
    out_tail = [{**c, "llm_score": None} for c in tail]
    return out_head + out_tail
