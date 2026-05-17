"""M4 T1 — sage_memory.expand tests.

Covers spec ACs:
  A1: strong-signal short-circuit on real FTS5 scores (4 scenarios
      from T0's expand_corpus_db fixture).
  A2: validated variant schema (happy + malformed + empty backfill).
  A3: LLM failure fallback with WARNING.
  Plus: env-var override with importlib.reload, free-path floor.

Mocks `sage_memory.llm.expand_query_variants` for the LLM-call path.
"""

from __future__ import annotations

import importlib
import logging

import httpx
import pytest


# Test fixtures from T0 (conftest.py) — expand_corpus_db is parametrized
# via indirect=True; bm25_probe is the helper function.


# ─── A1: strong-signal short-circuit (4 scenarios) ────────────────


@pytest.mark.parametrize(
    "expand_corpus_db", ["strong"], indirect=True,
)
def test_expand_strong_signal_short_circuits_no_llm_call(
    monkeypatch, expand_corpus_db, bm25_probe,
):
    """A1 positive: strong-signal scenario fires → no LLM call."""
    from sage_memory import expand

    seed = bm25_probe(expand_corpus_db, "quintarius ozymandias")

    # Mock the LLM helper to detect that it was NOT called.
    called = []

    def _fake(query, **kwargs):
        called.append(query)
        return {"lex": [query], "vec": query, "hyde": None}

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    result = expand.expand_query("quintarius ozymandias", seed)
    assert called == [], "LLM should not be called on short-circuit"
    assert result == {
        "lex": ["quintarius ozymandias"],
        "vec": "quintarius ozymandias",
        "hyde": None,
    }


@pytest.mark.parametrize(
    "expand_corpus_db", ["high-top1-ambiguous"], indirect=True,
)
def test_expand_high_top1_ambiguous_ratio_runs_llm(
    monkeypatch, expand_corpus_db, bm25_probe,
):
    """A1 counter 1: two strong matches → ratio gate fails → LLM called."""
    from sage_memory import expand

    seed = bm25_probe(expand_corpus_db, "quintarius ozymandias")
    called = []

    def _fake(query, **kwargs):
        called.append(query)
        return {
            "lex": ["q", "o"], "vec": query, "hyde": "doc",
        }

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    result = expand.expand_query("quintarius ozymandias", seed)
    assert called == ["quintarius ozymandias"]
    assert result["lex"] == ["q", "o"]
    assert result["hyde"] == "doc"


@pytest.mark.parametrize(
    "expand_corpus_db", ["ambiguous-all-weak"], indirect=True,
)
def test_expand_ambiguous_all_weak_runs_llm(
    monkeypatch, expand_corpus_db, bm25_probe,
):
    """A1 counter 2: comparable weak matches → ratio fails → LLM called."""
    from sage_memory import expand

    seed = bm25_probe(expand_corpus_db, "quintarius ozymandias")
    called = []

    def _fake(query, **kwargs):
        called.append(query)
        return {"lex": [query], "vec": query, "hyde": None}

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    expand.expand_query("quintarius ozymandias", seed)
    assert called == ["quintarius ozymandias"]


@pytest.mark.parametrize(
    "expand_corpus_db", ["low-confidence"], indirect=True,
)
def test_expand_low_confidence_runs_llm(
    monkeypatch, expand_corpus_db, bm25_probe,
):
    """A1 counter 3: empty seed_results (no matches) → top1 gate fails → LLM."""
    from sage_memory import expand

    seed = bm25_probe(expand_corpus_db, "quintarius ozymandias")
    assert seed == [], (
        "low-confidence scenario should produce empty bm25 results"
    )
    called = []

    def _fake(query, **kwargs):
        called.append(query)
        return {"lex": [query], "vec": query, "hyde": None}

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    expand.expand_query("quintarius ozymandias", seed)
    assert called == ["quintarius ozymandias"]


# ─── Env-var override (Major #5 reload contract) ──────────────────


@pytest.mark.parametrize(
    "expand_corpus_db", ["strong"], indirect=True,
)
def test_expand_threshold_env_override_with_reload(
    monkeypatch, expand_corpus_db, bm25_probe,
):
    """Major #5: SAGE_EXPAND_TOP1_NORM override via setenv+reload.

    With threshold=0.99, the "strong" scenario (top1_norm ~0.94)
    fails the top1 gate → expansion runs (LLM is called) where it
    would have short-circuited at the default 0.4 threshold.
    """
    monkeypatch.setenv("SAGE_EXPAND_TOP1_NORM", "0.99")
    from sage_memory import expand
    importlib.reload(expand)

    seed = bm25_probe(expand_corpus_db, "quintarius ozymandias")
    called = []

    def _fake(query, **kwargs):
        called.append(query)
        return {"lex": [query], "vec": query, "hyde": None}

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    expand.expand_query("quintarius ozymandias", seed)
    assert called == ["quintarius ozymandias"], (
        "with TOP1_NORM=0.99, strong scenario should NO LONGER "
        "short-circuit and the LLM should be called"
    )

    # Restore default for downstream tests.
    monkeypatch.delenv("SAGE_EXPAND_TOP1_NORM", raising=False)
    importlib.reload(expand)


# ─── A2: variant schema validation ────────────────────────────────


def test_expand_validates_variant_schema_happy_path(monkeypatch):
    """A2 happy path: well-formed variants flow through unchanged."""
    from sage_memory import expand

    def _fake(query, **kwargs):
        return {
            "lex": ["v1", "v2"],
            "vec": "expanded vec form",
            "hyde": "hypothetical_doc...",
        }

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    # Empty seed_bm25_results → no short-circuit possible → LLM called.
    result = expand.expand_query("q", [])
    assert result == {
        "lex": ["v1", "v2"],
        "vec": "expanded vec form",
        "hyde": "hypothetical_doc...",
    }


def test_expand_malformed_variants_silently_fall_back(
    monkeypatch, caplog,
):
    """A2 malformed: empty strings filtered, empty vec falls back."""
    from sage_memory import expand

    def _fake(query, **kwargs):
        # Empty strings in lex; empty vec; empty hyde
        return {"lex": ["", "v1", "", "v2"], "vec": "", "hyde": ""}

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    with caplog.at_level(logging.WARNING, logger="sage_memory.expand"):
        result = expand.expand_query("orig query", [])

    assert result["lex"] == ["v1", "v2"]
    assert result["vec"] == "orig query"  # empty → fallback to query
    assert result["hyde"] is None  # empty → None
    # Malformed-variants fall under "graceful degradation" per spec
    # A2 — no WARN expected
    assert "expand" not in caplog.text or "WARNING" not in caplog.text


def test_expand_empty_lex_backfilled_with_query(monkeypatch):
    """A2: lex=[] after cleanup gets backfilled with [query]."""
    from sage_memory import expand

    def _fake(query, **kwargs):
        return {"lex": [], "vec": "x", "hyde": None}

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    result = expand.expand_query("the original", [])
    assert result["lex"] == ["the original"]


# ─── A3: LLM failure fallback ─────────────────────────────────────


def test_expand_llm_failure_falls_back_with_warning(
    monkeypatch, caplog,
):
    """A3: LLM raises → no-expansion fallback + WARNING."""
    from sage_memory import expand

    def _fake(query, **kwargs):
        raise httpx.TimeoutException("simulated timeout")

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _fake
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )

    with caplog.at_level(logging.WARNING, logger="sage_memory.expand"):
        result = expand.expand_query("orig query", [])

    assert result == {
        "lex": ["orig query"], "vec": "orig query", "hyde": None,
    }
    assert any(
        "WARNING" in r.levelname and "expand" in r.name
        for r in caplog.records
    ), (
        "expected a WARNING from sage_memory.expand naming the failure"
    )


# ─── Free-path floor ──────────────────────────────────────────────


def test_expand_free_path_floor_silent_when_no_key(
    monkeypatch, caplog,
):
    """No LLM key + no short-circuit → no-expansion fallback, silently."""
    from sage_memory import expand

    # No short-circuit (empty seed); no key → silent skip.
    def _explode(*a, **kw):
        raise AssertionError("LLM must not be called when unconfigured")

    monkeypatch.setattr(
        "sage_memory.llm.expand_query_variants", _explode
    )
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: False
    )

    with caplog.at_level(logging.WARNING, logger="sage_memory.expand"):
        result = expand.expand_query("q", [])

    assert result == {"lex": ["q"], "vec": "q", "hyde": None}
    # Free-path floor is SILENT — no WARN
    assert not [
        r for r in caplog.records
        if r.name == "sage_memory.expand"
        and r.levelno >= logging.WARNING
    ]
