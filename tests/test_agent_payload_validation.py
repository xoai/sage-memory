"""Task 2 — `extractor.validate_agent_payload` unit tests.

Agent-driven extraction (0.9.0) takes user-supplied entities + relations
via the MCP wire format and must (a) validate vocab + size caps,
(b) rename JSON fields (`from`/`to`/`rel`) to the worker-shape
(`source_name`/`target_name`/`type`) so `extraction_write.write_extraction`
consumes them unchanged.
"""

from __future__ import annotations

import pytest

from sage_memory.extractor import validate_agent_payload


# ───── Happy path ─────

def test_valid_entities_and_relations_no_errors():
    entities = [
        {"name": "Stripe", "type": "TECHNOLOGY"},
        {"name": "PaymentOrchestrator", "type": "CONCEPT"},
    ]
    relations = [
        {"from": "PaymentOrchestrator", "to": "Stripe", "rel": "depends_on"},
    ]
    cleaned_ents, cleaned_rels, errors = validate_agent_payload(
        entities, relations,
    )
    assert errors == []
    assert cleaned_ents == [
        {"name": "Stripe", "type": "TECHNOLOGY"},
        {"name": "PaymentOrchestrator", "type": "CONCEPT"},
    ]
    # The critical rename: from/to/rel → source_name/target_name/type
    assert cleaned_rels == [
        {"source_name": "PaymentOrchestrator",
         "target_name": "Stripe",
         "type": "depends_on"},
    ]


def test_field_rename_explicit():
    """Critical correctness test: JSON wire shape `{"from","to","rel"}`
    is renamed to worker shape `{"source_name","target_name","type"}`
    so `extraction_write.write_extraction` consumes the result without
    further translation."""
    _, rels, _ = validate_agent_payload(
        [],
        [{"from": "A", "to": "B", "rel": "depends_on"}],
    )
    assert len(rels) == 1
    assert set(rels[0].keys()) == {"source_name", "target_name", "type"}
    assert rels[0]["source_name"] == "A"
    assert rels[0]["target_name"] == "B"
    assert rels[0]["type"] == "depends_on"


def test_surface_form_passes_through():
    entities = [
        {"name": "Stripe", "type": "TECHNOLOGY", "surface_form": "stripe"},
    ]
    cleaned, _, errors = validate_agent_payload(entities, [])
    assert errors == []
    assert cleaned[0]["surface_form"] == "stripe"


def test_surface_form_optional():
    entities = [{"name": "Stripe", "type": "TECHNOLOGY"}]
    cleaned, _, errors = validate_agent_payload(entities, [])
    assert errors == []
    assert "surface_form" not in cleaned[0] or cleaned[0]["surface_form"] == "Stripe"


# ───── Empty / None inputs ─────

def test_none_inputs():
    cleaned_ents, cleaned_rels, errors = validate_agent_payload(None, None)
    assert cleaned_ents == []
    assert cleaned_rels == []
    assert errors == []


def test_empty_list_inputs():
    cleaned_ents, cleaned_rels, errors = validate_agent_payload([], [])
    assert cleaned_ents == []
    assert cleaned_rels == []
    assert errors == []


def test_only_entities_no_relations():
    cleaned_ents, cleaned_rels, errors = validate_agent_payload(
        [{"name": "X", "type": "CONCEPT"}], None,
    )
    assert errors == []
    assert len(cleaned_ents) == 1
    assert cleaned_rels == []


# ───── Vocab violations ─────

def test_unknown_entity_type_reported():
    _, _, errors = validate_agent_payload(
        [{"name": "X", "type": "INVALID_TYPE"}], [],
    )
    assert len(errors) == 1
    assert "INVALID_TYPE" in errors[0]
    assert "entities[0]" in errors[0]


def test_unknown_relation_type_reported():
    _, _, errors = validate_agent_payload(
        [{"name": "A", "type": "CONCEPT"}, {"name": "B", "type": "CONCEPT"}],
        [{"from": "A", "to": "B", "rel": "INVALID_REL"}],
    )
    assert len(errors) == 1
    assert "INVALID_REL" in errors[0]
    assert "relations[0]" in errors[0]


# ───── Size caps ─────

def test_entities_oversize_capped_at_50():
    too_many = [
        {"name": f"E{i}", "type": "CONCEPT"} for i in range(51)
    ]
    _, _, errors = validate_agent_payload(too_many, [])
    assert any("50" in e or "entities" in e for e in errors)


def test_entities_exactly_50_ok():
    fifty = [
        {"name": f"E{i}", "type": "CONCEPT"} for i in range(50)
    ]
    cleaned, _, errors = validate_agent_payload(fifty, [])
    assert errors == []
    assert len(cleaned) == 50


def test_relations_oversize_capped_at_100():
    entities = [
        {"name": "A", "type": "CONCEPT"},
        {"name": "B", "type": "CONCEPT"},
    ]
    too_many = [
        {"from": "A", "to": "B", "rel": "relates_to"} for _ in range(101)
    ]
    _, _, errors = validate_agent_payload(entities, too_many)
    assert any("100" in e or "relations" in e for e in errors)


def test_relations_exactly_100_ok():
    entities = [
        {"name": "A", "type": "CONCEPT"},
        {"name": "B", "type": "CONCEPT"},
    ]
    hundred = [
        {"from": "A", "to": "B", "rel": "relates_to"} for _ in range(100)
    ]
    _, cleaned_rels, errors = validate_agent_payload(entities, hundred)
    assert errors == []
    assert len(cleaned_rels) == 100


# ───── Field-shape violations ─────

def test_entity_name_too_long():
    long_name = "X" * 201
    _, _, errors = validate_agent_payload(
        [{"name": long_name, "type": "CONCEPT"}], [],
    )
    assert any("name" in e and "200" in e for e in errors)


def test_entity_missing_name():
    _, _, errors = validate_agent_payload(
        [{"type": "CONCEPT"}], [],
    )
    assert any("name" in e for e in errors)


def test_entity_missing_type():
    _, _, errors = validate_agent_payload(
        [{"name": "X"}], [],
    )
    assert any("type" in e for e in errors)


def test_entity_non_string_name():
    _, _, errors = validate_agent_payload(
        [{"name": 123, "type": "CONCEPT"}], [],
    )
    assert any("name" in e for e in errors)


def test_relation_missing_field():
    """Missing `from`, `to`, or `rel` → error."""
    _, _, errors = validate_agent_payload(
        [{"name": "A", "type": "CONCEPT"}],
        [{"from": "A", "rel": "relates_to"}],  # missing 'to'
    )
    assert any("to" in e for e in errors)


def test_from_reserved_word_accessed_safely():
    """`from` is a Python soft keyword in some contexts but valid as a
    dict key. Validator must access via subscript, not attribute."""
    payload = {"from": "A", "to": "B", "rel": "depends_on"}
    _, rels, errors = validate_agent_payload(
        [{"name": "A", "type": "CONCEPT"},
         {"name": "B", "type": "CONCEPT"}],
        [payload],
    )
    assert errors == []
    assert rels[0]["source_name"] == "A"


# ───── Mixed valid + invalid ─────

def test_partial_validation_returns_all_errors():
    """Multiple violations across the payload all surface in errors;
    cleaned outputs contain only the valid items."""
    entities = [
        {"name": "Valid", "type": "CONCEPT"},
        {"name": "X", "type": "INVALID"},
        {"name": "Y", "type": "PERSON"},
    ]
    cleaned, _, errors = validate_agent_payload(entities, [])
    assert len(errors) == 1
    # Valid entities pass through
    names = {e["name"] for e in cleaned}
    assert "Valid" in names
    assert "Y" in names
    assert "X" not in names


# ───── All ENTITY_TYPES / RELATION_TYPES are accepted ─────

@pytest.mark.parametrize("etype", [
    "PERSON", "CONCEPT", "TECHNOLOGY", "PROJECT", "EVENT", "OTHER",
])
def test_all_entity_types_accepted(etype):
    _, _, errors = validate_agent_payload(
        [{"name": "X", "type": etype}], [],
    )
    assert errors == []


@pytest.mark.parametrize("rtype", [
    "mentions", "relates_to", "contains", "depends_on", "contradicts",
    "derived_from", "implements", "references", "supersedes",
    "alternative_to",
])
def test_all_relation_types_accepted(rtype):
    _, _, errors = validate_agent_payload(
        [{"name": "A", "type": "CONCEPT"},
         {"name": "B", "type": "CONCEPT"}],
        [{"from": "A", "to": "B", "rel": rtype}],
    )
    assert errors == []
