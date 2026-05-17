"""T2 — sage_memory.extractor tests.

Covers spec A2 (prompt + JSON validation) and feeds A9 (entities
populate end-to-end). Mocks `llm.extract_entities` — no real LLM
or httpx calls.

Plan T2 done-when reference: 8 test gates including the pinned
controlled-vocab fixture (test_extractor_happy_path uses depends_on,
and a paired test rejects out-of-vocab integrates_with).
"""

from __future__ import annotations

import logging

import pytest


# ─── Fixtures ─────────────────────────────────────────────────────


def _patch_llm(monkeypatch, response):
    """Replace llm.extract_entities with a function returning `response`.

    `response` may be a dict (single call return) or a list of dicts
    (returned per-call).
    """
    from sage_memory import extractor
    calls = {"n": 0}

    def fake_llm(*args, **kwargs):
        calls["n"] += 1
        if isinstance(response, list):
            idx = min(calls["n"] - 1, len(response) - 1)
            value = response[idx]
        else:
            value = response
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(extractor.llm, "extract_entities", fake_llm)
    return calls


# ─── Happy path + controlled vocab ────────────────────────────────


def test_extractor_happy_path(monkeypatch):
    """Fixture content + canned LLM response with controlled vocab ->
    extractor returns 3 entities + 1 relation. PINNED mapping:
    "integrates_with" maps to depends_on (strict vocab); the mocked
    LLM emits depends_on directly."""
    canned = {
        "entities": [
            {"name": "Bob", "type": "PERSON", "surface_form": "Bob"},
            {"name": "Claude API", "type": "TECHNOLOGY",
             "surface_form": "Claude API"},
            {"name": "Anthropic SDK", "type": "TECHNOLOGY",
             "surface_form": "Anthropic SDK"},
        ],
        "relations": [
            {"source_name": "Claude API", "target_name": "Anthropic SDK",
             "type": "depends_on"},
        ],
    }
    _patch_llm(monkeypatch, canned)

    from sage_memory import extractor
    result = extractor.extract(
        "Bob mentioned Claude API integrates with Anthropic SDK"
    )
    assert len(result["entities"]) == 3
    assert {e["type"] for e in result["entities"]} == {
        "PERSON", "TECHNOLOGY"
    }
    assert len(result["relations"]) == 1
    assert result["relations"][0]["type"] == "depends_on"


def test_extractor_rejects_out_of_vocab_relation(monkeypatch):
    """Out-of-vocab relation type "integrates_with" is REJECTED
    (strict schema), not silently remapped. Other valid relations
    pass through."""
    canned = {
        "entities": [
            {"name": "Claude API", "type": "TECHNOLOGY",
             "surface_form": "Claude API"},
            {"name": "Anthropic SDK", "type": "TECHNOLOGY",
             "surface_form": "Anthropic SDK"},
            {"name": "Project Foo", "type": "PROJECT",
             "surface_form": "Project Foo"},
        ],
        "relations": [
            {"source_name": "Claude API", "target_name": "Anthropic SDK",
             "type": "integrates_with"},   # invalid → dropped
            {"source_name": "Claude API", "target_name": "Project Foo",
             "type": "relates_to"},        # valid → kept
        ],
    }
    _patch_llm(monkeypatch, canned)

    from sage_memory import extractor
    result = extractor.extract("...")
    assert len(result["relations"]) == 1
    assert result["relations"][0]["type"] == "relates_to"


# ─── Name normalization ───────────────────────────────────────────


def test_extractor_normalize_name():
    from sage_memory import extractor
    assert extractor.normalize_name("  Claude   API!  ") == "claude api"
    assert extractor.normalize_name("Bob's Project") == "bobs project"
    assert extractor.normalize_name("FastEmbed-v2") == "fastembed v2"


# ─── Retry on malformed JSON ──────────────────────────────────────


def test_extractor_malformed_json_retries_once(monkeypatch):
    """First call returns malformed JSON shape; second returns valid.
    extract() returns the valid result, 2 LLM calls made."""
    import json as json_mod
    bad = json_mod.JSONDecodeError("garbage", "x", 0)
    good = {"entities": [], "relations": []}

    calls = _patch_llm(monkeypatch, [bad, good])
    from sage_memory import extractor
    result = extractor.extract("...")
    assert calls["n"] == 2
    assert result == {"entities": [], "relations": []}


def test_extractor_double_failure_raises_extraction_failed(monkeypatch):
    """Both calls return malformed JSON → ExtractionFailedError."""
    import json as json_mod
    bad = json_mod.JSONDecodeError("garbage", "x", 0)
    _patch_llm(monkeypatch, [bad, bad])

    from sage_memory import extractor
    with pytest.raises(extractor.ExtractionFailedError):
        extractor.extract("...")


# ─── Caps + filtering ─────────────────────────────────────────────


def test_extractor_caps_entities(monkeypatch):
    """LLM returns 25 valid entities → result truncated to 10."""
    canned = {
        "entities": [
            {"name": f"Thing{i}", "type": "CONCEPT",
             "surface_form": f"Thing{i}"}
            for i in range(25)
        ],
        "relations": [],
    }
    _patch_llm(monkeypatch, canned)
    from sage_memory import extractor
    result = extractor.extract("...", max_entities=10)
    assert len(result["entities"]) == 10


def test_extractor_invalid_entity_type_filtered(monkeypatch):
    """type='INVALID' → entity dropped; valid entities kept."""
    canned = {
        "entities": [
            {"name": "Bob", "type": "PERSON", "surface_form": "Bob"},
            {"name": "Junk", "type": "INVALID", "surface_form": "Junk"},
            {"name": "Foo", "type": "PROJECT", "surface_form": "Foo"},
        ],
        "relations": [],
    }
    _patch_llm(monkeypatch, canned)
    from sage_memory import extractor
    result = extractor.extract("...")
    assert len(result["entities"]) == 2
    assert {e["name"] for e in result["entities"]} == {"Bob", "Foo"}


def test_extractor_long_name_truncated(monkeypatch, caplog):
    """300-char name → truncated to 200 with WARNING logged."""
    long_name = "x" * 300
    canned = {
        "entities": [
            {"name": long_name, "type": "CONCEPT",
             "surface_form": long_name},
        ],
        "relations": [],
    }
    _patch_llm(monkeypatch, canned)
    from sage_memory import extractor
    with caplog.at_level(logging.WARNING, logger="sage_memory.extractor"):
        result = extractor.extract("...")
    assert len(result["entities"]) == 1
    assert len(result["entities"][0]["name"]) == 200
    assert any("truncated" in rec.message.lower()
               for rec in caplog.records)
