"""M4 T2 — sage_memory.rerank tests.

Covers spec ACs:
  A4: LLM call with top-K candidates → llm_score augmented on each.
  A5: failure_visibility = warn | silent | error (3 modes + import-
      time fail-fast on unknown value).
  A6: per-candidate truncation + defensive over-budget RRF fallback.
  A9 sub-case: empty/single-candidate list short-circuits.
  A14: ID validation sub-cases (i) hallucinated → WARN+drop,
       (ii) missing → silent llm_score=None, (iii) duplicate → keep
       first, drop subsequent silently.
  Major #6: non-list LLM response, string IDs (coerce), uncoercible IDs.
  Major #5: SAGE_RERANK_TOP_K env override with importlib.reload.
  Plus: prompt-injection delimiter wrapping (Minor-substantive #8),
        free-path floor (silent when LLM unconfigured).

Mocks `sage_memory.llm.rerank_candidates` (or `llm._call_llm` for
prompt-wrap tests).
"""

from __future__ import annotations

import importlib
import logging

import httpx
import pytest


# ─── Helpers ──────────────────────────────────────────────────────


def _make_candidates(n: int, content_len: int = 100) -> list[dict]:
    """Build n candidates with stable IDs 1..n and consistent rrf_scores."""
    return [
        {
            "id": i,
            "memory_id": f"mem-{i}",
            "content": "x" * content_len,
            "rrf_score": 1.0 / i,
        }
        for i in range(1, n + 1)
    ]


# ─── A4: basic LLM call ───────────────────────────────────────────


def test_rerank_calls_llm_with_top_k_candidates(monkeypatch):
    """A4: LLM call → per-candidate `llm_score` attached."""
    from sage_memory import rerank

    captured = {}

    def _fake(query, candidates, *, top_k, **_):
        captured["query"] = query
        captured["candidates"] = candidates
        captured["top_k"] = top_k
        return [
            {"id": 1, "score": 0.9},
            {"id": 2, "score": 0.4},
            {"id": 3, "score": 0.7},
        ]

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    cs = _make_candidates(3)
    out = rerank.rerank("query", cs)

    assert len(out) == 3
    scores = {c["id"]: c["llm_score"] for c in out}
    assert scores == {1: 0.9, 2: 0.4, 3: 0.7}


# ─── A9 sub-case: short skips ─────────────────────────────────────


def test_rerank_empty_list_skips_silently(monkeypatch):
    """A9: empty candidates → input returned unchanged, no LLM call."""
    from sage_memory import rerank

    called = []
    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates",
        lambda *a, **kw: (called.append(1), [])[1],
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    out = rerank.rerank("q", [])
    assert out == []
    assert called == []


def test_rerank_single_candidate_skips_silently(monkeypatch):
    """A9: len==1 → input returned unchanged, no LLM call."""
    from sage_memory import rerank

    called = []
    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates",
        lambda *a, **kw: (called.append(1), [])[1],
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    cs = _make_candidates(1)
    out = rerank.rerank("q", cs)
    assert out == cs
    assert called == []


# ─── A6: truncation + over-budget fallback ────────────────────────


def test_rerank_truncates_long_content(monkeypatch):
    """A6: per-candidate content truncated to _MAX_TOKENS//top_k*4 chars."""
    from sage_memory import rerank

    captured = {}

    def _fake(query, candidates, *, top_k, **_):
        # Capture the candidates SENT to the LLM (post-truncation).
        captured["sent"] = [
            (c["id"], len(c["content"])) for c in candidates
        ]
        return [{"id": c["id"], "score": 0.5} for c in candidates]

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    # 3 candidates, each with 100k chars. With default top_k=15,
    # max chars per candidate = 8192 // 15 * 4 = 2184.
    cs = _make_candidates(3, content_len=100_000)
    original_lengths = [len(c["content"]) for c in cs]
    out = rerank.rerank("q", cs)

    # Caller's input must not be mutated.
    assert [len(c["content"]) for c in cs] == original_lengths

    # Every truncated content fits under the per-candidate cap.
    cap = rerank._MAX_TOKENS // 15 * 4
    for _, sent_len in captured["sent"]:
        assert sent_len <= cap, (
            f"content sent to LLM ({sent_len}) exceeds cap ({cap})"
        )

    # llm_score was attached to original-length candidates.
    assert all(c["llm_score"] == 0.5 for c in out)


def test_rerank_defensive_over_budget_falls_back_to_rrf(
    monkeypatch, caplog,
):
    """A6: total prompt over budget → RRF fallback with WARN."""
    from sage_memory import rerank

    def _fake(query, candidates, *, top_k, **_):
        # Should NOT be called — defensive cap drops before send.
        raise AssertionError("LLM should not be called on over-budget")

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    # Construct a pathological input: 20 candidates each at the per-
    # candidate cap, plus an enormous query. Even after per-candidate
    # truncation, the total exceeds _MAX_TOKENS * 4.
    huge_query = "x" * (rerank._MAX_TOKENS * 5)
    cs = _make_candidates(20, content_len=rerank._MAX_TOKENS * 2)

    with caplog.at_level(logging.WARNING, logger="sage_memory.rerank"):
        out = rerank.rerank(huge_query, cs)

    assert len(out) == 20
    assert all(c["llm_score"] is None for c in out)
    assert any(
        "over-budget" in r.message.lower() or "RRF" in r.message
        for r in caplog.records
    )


# ─── A5: failure_visibility 3 modes ───────────────────────────────


def test_rerank_failure_visibility_warn(monkeypatch, caplog):
    """A5 default: LLM raises → all llm_score=None + WARN."""
    from sage_memory import rerank

    def _fake(*a, **kw):
        raise httpx.TimeoutException("simulated")

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    cs = _make_candidates(3)
    with caplog.at_level(logging.WARNING, logger="sage_memory.rerank"):
        out = rerank.rerank("q", cs)

    assert all(c["llm_score"] is None for c in out)
    assert any(
        r.levelno >= logging.WARNING and "TimeoutException" in r.message
        for r in caplog.records
    )


def test_rerank_failure_visibility_silent(monkeypatch, caplog):
    """A5 silent: LLM raises → all llm_score=None, no log."""
    monkeypatch.setenv("SAGE_RERANK_FAILURE_VISIBILITY", "silent")
    from sage_memory import rerank
    importlib.reload(rerank)

    def _fake(*a, **kw):
        raise httpx.TimeoutException("simulated")

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    cs = _make_candidates(3)
    with caplog.at_level(logging.DEBUG, logger="sage_memory.rerank"):
        out = rerank.rerank("q", cs)

    assert all(c["llm_score"] is None for c in out)
    assert not [
        r for r in caplog.records
        if r.name == "sage_memory.rerank"
        and r.levelno >= logging.WARNING
    ]

    monkeypatch.delenv("SAGE_RERANK_FAILURE_VISIBILITY", raising=False)
    importlib.reload(rerank)


def test_rerank_failure_visibility_error_reraises(monkeypatch):
    """A5 error: LLM raises → re-raised to caller."""
    monkeypatch.setenv("SAGE_RERANK_FAILURE_VISIBILITY", "error")
    from sage_memory import rerank
    importlib.reload(rerank)

    def _fake(*a, **kw):
        raise httpx.TimeoutException("simulated")

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    cs = _make_candidates(3)
    with pytest.raises(httpx.TimeoutException):
        rerank.rerank("q", cs)

    monkeypatch.delenv("SAGE_RERANK_FAILURE_VISIBILITY", raising=False)
    importlib.reload(rerank)


def test_rerank_unknown_failure_visibility_raises_at_import(monkeypatch):
    """A5 defensive: invalid SAGE_RERANK_FAILURE_VISIBILITY → ValueError."""
    monkeypatch.setenv("SAGE_RERANK_FAILURE_VISIBILITY", "panic")
    import sage_memory.rerank as rerank_mod
    with pytest.raises(ValueError, match="FAILURE_VISIBILITY"):
        importlib.reload(rerank_mod)

    monkeypatch.delenv("SAGE_RERANK_FAILURE_VISIBILITY", raising=False)
    importlib.reload(rerank_mod)


# ─── A14: ID validation sub-cases ─────────────────────────────────


def test_rerank_drops_hallucinated_ids_with_warn(monkeypatch, caplog):
    """A14 (i): extra IDs not in input → WARN + drop."""
    from sage_memory import rerank

    def _fake(query, candidates, **_):
        return [
            {"id": 1, "score": 0.9},
            {"id": 2, "score": 0.5},
            {"id": 99, "score": 0.99},  # spurious
            {"id": 3, "score": 0.3},
        ]

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    cs = _make_candidates(3)
    with caplog.at_level(logging.WARNING, logger="sage_memory.rerank"):
        out = rerank.rerank("q", cs)

    # Only the 3 real IDs come back; 99 dropped.
    out_ids = {c["id"] for c in out}
    assert out_ids == {1, 2, 3}
    scores = {c["id"]: c["llm_score"] for c in out}
    assert scores == {1: 0.9, 2: 0.5, 3: 0.3}
    # WARN names the spurious ID.
    assert any(
        "99" in r.message and r.levelno >= logging.WARNING
        for r in caplog.records
    )


def test_rerank_missing_ids_stay_none_silently(monkeypatch, caplog):
    """A14 (ii): LLM omits some IDs → those retain llm_score=None, no WARN."""
    from sage_memory import rerank

    def _fake(query, candidates, **_):
        # Only IDs 1 and 2; ID 3 omitted.
        return [
            {"id": 1, "score": 0.9},
            {"id": 2, "score": 0.5},
        ]

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    cs = _make_candidates(3)
    with caplog.at_level(logging.DEBUG, logger="sage_memory.rerank"):
        out = rerank.rerank("q", cs)

    scores = {c["id"]: c["llm_score"] for c in out}
    assert scores == {1: 0.9, 2: 0.5, 3: None}
    # No WARN — normal rank-cutoff behavior.
    assert not [
        r for r in caplog.records
        if r.name == "sage_memory.rerank"
        and r.levelno >= logging.WARNING
    ]


def test_rerank_duplicate_ids_keep_first_silently(monkeypatch, caplog):
    """A14 (iii): duplicate IDs → keep first occurrence, drop rest, no WARN."""
    from sage_memory import rerank

    def _fake(query, candidates, **_):
        return [
            {"id": 1, "score": 0.9},  # kept
            {"id": 1, "score": 0.3},  # dropped silently
            {"id": 2, "score": 0.5},
        ]

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    cs = _make_candidates(2)
    with caplog.at_level(logging.DEBUG, logger="sage_memory.rerank"):
        out = rerank.rerank("q", cs)

    scores = {c["id"]: c["llm_score"] for c in out}
    assert scores == {1: 0.9, 2: 0.5}
    # No WARN.
    assert not [
        r for r in caplog.records
        if r.name == "sage_memory.rerank"
        and r.levelno >= logging.WARNING
    ]


# ─── Major #6: response-shape validation ──────────────────────────


def test_rerank_non_list_response_falls_back(monkeypatch, caplog):
    """Major #6: LLM returns non-list → failure path → all None + WARN."""
    from sage_memory import rerank

    def _fake(*a, **kw):
        # Object instead of list (LLM contract violation).
        return {"error": "something broke"}

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    cs = _make_candidates(3)
    with caplog.at_level(logging.WARNING, logger="sage_memory.rerank"):
        out = rerank.rerank("q", cs)

    assert all(c["llm_score"] is None for c in out)
    assert any(
        r.levelno >= logging.WARNING for r in caplog.records
    )


def test_rerank_string_id_coerced_silently(monkeypatch, caplog):
    """Major #6: LLM returns "1" instead of 1 → coerce to int, no WARN."""
    from sage_memory import rerank

    def _fake(*a, **kw):
        return [
            {"id": "1", "score": 0.9},
            {"id": "2", "score": 0.5},
        ]

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    cs = _make_candidates(2)
    with caplog.at_level(logging.DEBUG, logger="sage_memory.rerank"):
        out = rerank.rerank("q", cs)

    scores = {c["id"]: c["llm_score"] for c in out}
    assert scores == {1: 0.9, 2: 0.5}
    assert not [
        r for r in caplog.records
        if r.name == "sage_memory.rerank"
        and r.levelno >= logging.WARNING
    ]


def test_rerank_uncoercible_id_dropped_silently(monkeypatch, caplog):
    """Major #6: id "not-an-int" → drop that entry, others handled."""
    from sage_memory import rerank

    def _fake(*a, **kw):
        return [
            {"id": "not-an-int", "score": 0.9},
            {"id": 2, "score": 0.5},
        ]

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    cs = _make_candidates(2)
    with caplog.at_level(logging.DEBUG, logger="sage_memory.rerank"):
        out = rerank.rerank("q", cs)

    # Candidate id=1 was not scored by LLM; ID 2 was.
    scores = {c["id"]: c["llm_score"] for c in out}
    assert scores == {1: None, 2: 0.5}
    # No WARN on coerce-failure-drop (silent defensive parse).
    assert not [
        r for r in caplog.records
        if r.name == "sage_memory.rerank"
        and r.levelno >= logging.WARNING
    ]


# ─── Prompt-injection delimiter wrapping (Minor-substantive #8) ────


def test_rerank_prompt_wraps_query_in_delimiters(monkeypatch):
    """Confirms llm.rerank_candidates wraps query + content in delimiters.

    Tests the end-to-end contract: rerank.rerank() → llm.rerank_candidates
    → user_content passed to _call_llm contains <query>...</query> and
    <candidate id="...">...</candidate> tags.
    """
    from sage_memory import rerank, llm

    captured = {}

    def _fake_call(*, system_prompt, user_content, max_tokens,
                   timeout_s):
        captured["user_content"] = user_content
        captured["system_prompt"] = system_prompt
        return {"rankings": [{"id": 1, "score": 0.5}]}

    monkeypatch.setattr(llm, "_call_llm", _fake_call)
    monkeypatch.setattr(llm, "is_configured", lambda: True)

    cs = [
        {"id": 1, "memory_id": "m1",
         "content": "candidate one body", "rrf_score": 0.5},
        {"id": 2, "memory_id": "m2",
         "content": "candidate two body", "rrf_score": 0.4},
    ]
    rerank.rerank("user query text", cs)

    uc = captured["user_content"]
    assert "<query>user query text</query>" in uc
    assert '<candidate id="1">candidate one body</candidate>' in uc
    assert '<candidate id="2">candidate two body</candidate>' in uc
    # System prompt mentions the data-vs-instructions framing.
    sp = captured["system_prompt"]
    assert "delimiters" in sp.lower() or "wrapped" in sp.lower()


# ─── Free-path floor ──────────────────────────────────────────────


def test_rerank_free_path_floor_silent_when_no_key(monkeypatch, caplog):
    """No LLM key → all llm_score=None silently. No WARN."""
    from sage_memory import rerank

    def _explode(*a, **kw):
        raise AssertionError("LLM must not be called when unconfigured")

    monkeypatch.setattr(
        "sage_memory.llm.rerank_candidates", _explode
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: False
    )

    cs = _make_candidates(3)
    with caplog.at_level(logging.DEBUG, logger="sage_memory.rerank"):
        out = rerank.rerank("q", cs)

    assert all(c["llm_score"] is None for c in out)
    assert not [
        r for r in caplog.records
        if r.name == "sage_memory.rerank"
        and r.levelno >= logging.WARNING
    ]


# ─── Major #5: env-reload contract uniformity ─────────────────────


def test_rerank_top_k_env_override_with_reload(monkeypatch):
    """Major #5: SAGE_RERANK_TOP_K=5 + reload → _TOP_K_DEFAULT == 5."""
    monkeypatch.setenv("SAGE_RERANK_TOP_K", "5")
    from sage_memory import rerank
    importlib.reload(rerank)

    assert rerank._TOP_K_DEFAULT == 5

    monkeypatch.delenv("SAGE_RERANK_TOP_K", raising=False)
    importlib.reload(rerank)
    assert rerank._TOP_K_DEFAULT == 15
