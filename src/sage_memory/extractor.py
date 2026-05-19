"""Entity/relation extraction — prompt + schema validation.

Builds the extraction prompt per ADR-003, calls `llm.extract_entities`,
validates the response against a strict schema, and normalizes entity
names. JSON-parse / schema failures get one retry with a simplified
prompt; a second failure raises `ExtractionFailedError`.

DB writes (entities, mentions, relations) are NOT this module's job
— the worker (T3) handles them.
"""

from __future__ import annotations

import json
import logging
import re

from . import llm


logger = logging.getLogger("sage_memory.extractor")


# ─── Controlled vocabularies (ADR-003) ────────────────────────────

ENTITY_TYPES = frozenset({
    "PERSON", "CONCEPT", "TECHNOLOGY", "PROJECT", "EVENT", "OTHER",
})

RELATION_TYPES = frozenset({
    "mentions", "relates_to", "contains", "depends_on", "contradicts",
    "derived_from", "implements", "references", "supersedes",
    "alternative_to",
})

# Plan-introduced defensive cap on entity name length.
_MAX_NAME_LEN = 200
_MAX_RELATIONS = 15

# Agent-payload caps (0.9.0). Justification:
# - 50 entities ≈ _MAX_RELATIONS × 3.3 headroom; covers long stored
#   memories without inviting agent buffer abuse.
# - 100 relations ≈ entity² density cap; bounds a single store() to
#   ~150 row INSERTs/UPSERTs.
_AGENT_MAX_ENTITIES = 50
_AGENT_MAX_RELATIONS = 100


# ─── Errors ───────────────────────────────────────────────────────


class ExtractionFailedError(RuntimeError):
    """Raised when LLM extraction fails twice (initial + retry)."""


# ─── Public API ───────────────────────────────────────────────────


def extract(content: str, *, max_entities: int = 10) -> dict:
    """Extract entities + relations from content via the LLM.

    Returns a dict with keys 'entities' and 'relations'. Each entity
    has 'name', 'type', 'surface_form'. Each relation has
    'source_name', 'target_name', 'type'. Entries failing schema
    validation (unknown type, missing field) are dropped silently.

    Raises:
        ExtractionFailedError: both initial + retry calls failed
            (malformed JSON, schema-empty, transport error).
    """
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            raw = llm.extract_entities(
                content, max_entities=max_entities,
            )
            return _validate_and_clean(raw, max_entities=max_entities)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            logger.warning(
                "extractor: validation failure on attempt %d/2: %s",
                attempt + 1, exc,
            )

    raise ExtractionFailedError(
        f"extraction failed after 2 attempts: {last_error}"
    )


def validate_agent_payload(
    entities, relations,
) -> tuple[list[dict], list[dict], list[str]]:
    """Validate the agent-provided entities + relations payload.

    Renames JSON wire fields (`from`/`to`/`rel`) to the worker-shape
    (`source_name`/`target_name`/`type`) consumed by
    `extraction_write.write_extraction`. Returns three lists:

      (cleaned_entities, cleaned_relations, error_messages)

    `None` or `[]` inputs are valid → empty lists, no errors.
    Each invalid item is dropped from the cleaned output and a
    descriptive error is appended to the error list. The caller
    decides whether non-empty errors should reject the whole call.
    """
    errors: list[str] = []
    cleaned_entities: list[dict] = []
    cleaned_relations: list[dict] = []

    if entities is None:
        entities = []
    if relations is None:
        relations = []

    if len(entities) > _AGENT_MAX_ENTITIES:
        errors.append(
            f"entities: {len(entities)} exceeds cap of "
            f"{_AGENT_MAX_ENTITIES}"
        )
        return [], [], errors

    if len(relations) > _AGENT_MAX_RELATIONS:
        errors.append(
            f"relations: {len(relations)} exceeds cap of "
            f"{_AGENT_MAX_RELATIONS}"
        )
        return [], [], errors

    for i, ent in enumerate(entities):
        if not isinstance(ent, dict):
            errors.append(f"entities[{i}]: must be a dict, got {type(ent).__name__}")
            continue
        name = ent.get("name")
        etype = ent.get("type")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"entities[{i}].name: must be a non-empty string")
            continue
        if len(name) > _MAX_NAME_LEN:
            errors.append(
                f"entities[{i}].name: {len(name)} chars exceeds "
                f"cap of {_MAX_NAME_LEN}"
            )
            continue
        if not isinstance(etype, str) or etype not in ENTITY_TYPES:
            errors.append(
                f"entities[{i}].type: {etype!r} not in vocab "
                f"({sorted(ENTITY_TYPES)})"
            )
            continue
        cleaned = {"name": name, "type": etype}
        surface = ent.get("surface_form")
        if isinstance(surface, str) and surface:
            cleaned["surface_form"] = surface
        cleaned_entities.append(cleaned)

    for i, rel in enumerate(relations):
        if not isinstance(rel, dict):
            errors.append(
                f"relations[{i}]: must be a dict, got {type(rel).__name__}"
            )
            continue
        src = rel.get("from")
        tgt = rel.get("to")
        rtype = rel.get("rel")
        if not isinstance(src, str) or not src.strip():
            errors.append(f"relations[{i}].from: must be a non-empty string")
            continue
        if not isinstance(tgt, str) or not tgt.strip():
            errors.append(f"relations[{i}].to: must be a non-empty string")
            continue
        if not isinstance(rtype, str) or rtype not in RELATION_TYPES:
            errors.append(
                f"relations[{i}].rel: {rtype!r} not in vocab "
                f"({sorted(RELATION_TYPES)})"
            )
            continue
        cleaned_relations.append({
            "source_name": src,
            "target_name": tgt,
            "type": rtype,
        })

    return cleaned_entities, cleaned_relations, errors


def normalize_name(name: str) -> str:
    """Normalize an entity name for in-DB dedup:
    lower + punctuation stripped + collapsed whitespace.

    Matches UNIQUE(name_normalized, type) in migration 004.
    """
    text = name.lower().strip()
    # Strip apostrophes to empty FIRST so "Bob's" and "Bobs" dedup
    # to the same key. Other punctuation becomes whitespace.
    text = re.sub(r"['’]", "", text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ─── Validation ───────────────────────────────────────────────────


def _validate_and_clean(raw: dict, *, max_entities: int) -> dict:
    """Filter the LLM response to schema-valid entries.

    - Drops entities with unknown type or missing fields
    - Drops relations with unknown type or missing fields
    - Truncates entities to max_entities, relations to _MAX_RELATIONS
    - Truncates entity names to _MAX_NAME_LEN with a WARNING log
    - Raises ValueError if the top-level shape is wrong
      (triggers the one retry in `extract()`)
    """
    if not isinstance(raw, dict):
        raise ValueError(
            f"expected dict at top level, got {type(raw).__name__}"
        )
    entities_in = raw.get("entities", [])
    relations_in = raw.get("relations", [])
    if not isinstance(entities_in, list) or not isinstance(
        relations_in, list
    ):
        raise ValueError(
            "entities and relations must both be lists"
        )

    entities: list[dict] = []
    for ent in entities_in:
        cleaned = _clean_entity(ent)
        if cleaned is not None:
            entities.append(cleaned)
        if len(entities) >= max_entities:
            break

    relations = []
    for rel in relations_in:
        cleaned = _clean_relation(rel)
        if cleaned is not None:
            relations.append(cleaned)
        if len(relations) >= _MAX_RELATIONS:
            break

    return {"entities": entities, "relations": relations}


def _clean_entity(ent) -> dict | None:
    if not isinstance(ent, dict):
        return None
    name = ent.get("name")
    etype = ent.get("type")
    surface = ent.get("surface_form", name)
    if not isinstance(name, str) or not isinstance(etype, str):
        return None
    if etype not in ENTITY_TYPES:
        return None
    if len(name) > _MAX_NAME_LEN:
        logger.warning(
            "extractor: entity name truncated (was %d chars, "
            "cap=%d)", len(name), _MAX_NAME_LEN,
        )
        name = name[:_MAX_NAME_LEN]
    if not isinstance(surface, str):
        surface = name
    return {"name": name, "type": etype, "surface_form": surface}


def _clean_relation(rel) -> dict | None:
    if not isinstance(rel, dict):
        return None
    src = rel.get("source_name")
    tgt = rel.get("target_name")
    rtype = rel.get("type")
    if not isinstance(src, str) or not isinstance(tgt, str):
        return None
    if not isinstance(rtype, str) or rtype not in RELATION_TYPES:
        return None
    return {"source_name": src, "target_name": tgt, "type": rtype}
