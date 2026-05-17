"""M4 T3 — position-blend math (search._blended_score).

Covers spec A7:
  - 3 bucket weights: pos 1-3 → 0.75, pos 4-10 → 0.6, pos 11+ → 0.4
  - llm_score=None branch: returns rrf_score unchanged (NOT coerce-to-0)
  - position < 1 → ValueError
  - SAGE_RERANK_BLEND_CURVE env override (Major #5 reload contract)
  - Bucket-boundary tests (3→4 switch, 10→11 switch — Minor #9)
  - rrf=0 degenerate input (Minor #9)
"""

from __future__ import annotations

import importlib

import pytest


def test_blended_score_position_1_3_uses_first_weight():
    """A7: positions 1, 2, 3 → w_rrf = 0.75 (first bucket)."""
    from sage_memory import search
    for p in (1, 2, 3):
        out = search._blended_score(
            rrf_score=1.0, llm_score=0.0, position=p,
        )
        assert out == pytest.approx(0.75), f"position={p}"


def test_blended_score_position_4_10_uses_second_weight():
    """A7: positions 4, 7, 10 → w_rrf = 0.6 (second bucket)."""
    from sage_memory import search
    for p in (4, 7, 10):
        out = search._blended_score(
            rrf_score=1.0, llm_score=0.0, position=p,
        )
        assert out == pytest.approx(0.6), f"position={p}"


def test_blended_score_position_11_plus_uses_third_weight():
    """A7: positions 11+ → w_rrf = 0.4 (third bucket)."""
    from sage_memory import search
    for p in (11, 50, 1000):
        out = search._blended_score(
            rrf_score=1.0, llm_score=0.0, position=p,
        )
        assert out == pytest.approx(0.4), f"position={p}"


def test_blended_score_bucket_boundary_3_to_4():
    """Minor #9: pos=3 and pos=4 produce different scores (0.75 vs 0.6)."""
    from sage_memory import search
    s3 = search._blended_score(
        rrf_score=1.0, llm_score=0.0, position=3,
    )
    s4 = search._blended_score(
        rrf_score=1.0, llm_score=0.0, position=4,
    )
    assert s3 == pytest.approx(0.75)
    assert s4 == pytest.approx(0.6)
    assert s3 != s4


def test_blended_score_bucket_boundary_10_to_11():
    """Minor #9: pos=10 and pos=11 produce different scores (0.6 vs 0.4)."""
    from sage_memory import search
    s10 = search._blended_score(
        rrf_score=1.0, llm_score=0.0, position=10,
    )
    s11 = search._blended_score(
        rrf_score=1.0, llm_score=0.0, position=11,
    )
    assert s10 == pytest.approx(0.6)
    assert s11 == pytest.approx(0.4)
    assert s10 != s11


def test_blended_score_none_llm_returns_rrf_unchanged():
    """A7 None-branch: llm_score=None → returns rrf_score, NOT coerce-to-0."""
    from sage_memory import search
    for p in (1, 5, 11, 100):
        out = search._blended_score(
            rrf_score=0.42, llm_score=None, position=p,
        )
        assert out == pytest.approx(0.42), f"position={p}"


def test_blended_score_degenerate_rrf_zero():
    """Minor #9: rrf=0, llm=0.8, pos=1 → 0.25 * 0.8 = 0.2 (no crash)."""
    from sage_memory import search
    out = search._blended_score(
        rrf_score=0.0, llm_score=0.8, position=1,
    )
    assert out == pytest.approx(0.2)


def test_blended_score_position_below_1_raises():
    """Defensive guard: position < 1 → ValueError."""
    from sage_memory import search
    with pytest.raises(ValueError):
        search._blended_score(
            rrf_score=1.0, llm_score=0.5, position=0,
        )


def test_blended_curve_env_var_overrides_default(monkeypatch):
    """Major #5: SAGE_RERANK_BLEND_CURVE=0.9,0.5,0.1 + reload → tuple."""
    monkeypatch.setenv("SAGE_RERANK_BLEND_CURVE", "0.9,0.5,0.1")
    from sage_memory import search
    importlib.reload(search)

    assert search._BLEND_CURVE == (0.9, 0.5, 0.1)

    monkeypatch.delenv("SAGE_RERANK_BLEND_CURVE", raising=False)
    importlib.reload(search)
    assert search._BLEND_CURVE == (0.75, 0.6, 0.4)
