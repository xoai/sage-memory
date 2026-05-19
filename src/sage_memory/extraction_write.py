"""Shared entity/mention/relation write helper.

Used by both `worker._do_extract` (background path, LLM-driven) and
`store.store()` / `store.update()` (agent-driven path, 0.9.0+).
Refactored from `worker.py` private methods so both paths produce
equivalent rows for the same input.

The caller is responsible for transaction management — this helper
issues INSERTs/UPSERTs against the provided connection but does NOT
commit. Callers commit when their broader operation is done.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from typing import Sequence

from . import extractor as _extractor


logger = logging.getLogger("sage_memory.extraction_write")


def write_extraction(
    conn: sqlite3.Connection,
    memory_id: str,
    content: str,
    entities: Sequence[dict],
    relations: Sequence[dict],
    now: float,
) -> None:
    """Upsert entities, insert mentions, resolve and insert relations.

    Args:
        conn: live sqlite connection (caller manages tx + commit).
        memory_id: the memory these entities/relations attach to.
        content: source text — used to locate surface-form offsets
            for mentions.
        entities: list of `{"name": str, "type": str, "surface_form": str?}`.
        relations: list of `{"source_name": str, "target_name": str,
            "type": str}` (worker-shape; agent payloads must be
            renamed via `extractor.validate_agent_payload` before
            calling this).
        now: current epoch seconds; used for `created_at`/`updated_at`.
    """
    name_to_id: dict[tuple[str, str], str] = {}
    for ent in entities:
        normalized = _extractor.normalize_name(ent["name"])
        etype = ent["type"]
        entity_id = _upsert_entity(
            conn, ent["name"], normalized, etype, now,
        )
        name_to_id[(normalized, etype)] = entity_id
        _insert_mention(
            conn, memory_id, entity_id,
            ent.get("surface_form", ent["name"]), content, now,
        )

    for rel in relations:
        src_norm = _extractor.normalize_name(rel["source_name"])
        tgt_norm = _extractor.normalize_name(rel["target_name"])
        src_id = _resolve_entity_id(conn, src_norm, name_to_id)
        tgt_id = _resolve_entity_id(conn, tgt_norm, name_to_id)
        if src_id is None or tgt_id is None:
            logger.debug(
                "dropping relation: unresolvable endpoints "
                "(source=%r, target=%r)",
                rel["source_name"], rel["target_name"],
            )
            continue
        _insert_relation(
            conn, src_id, tgt_id, rel["type"], memory_id, now,
        )


# ─── Private helpers (moved verbatim from worker.py) ──────────────


def _upsert_entity(conn, name, normalized, etype, now) -> str:
    new_id = uuid.uuid4().hex
    cur = conn.execute(
        "INSERT INTO entities "
        "(id, name, name_normalized, type, mention_count, "
        " created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 1, ?, ?) "
        "ON CONFLICT(name_normalized, type) DO UPDATE SET "
        "  mention_count = mention_count + 1, "
        "  updated_at = excluded.updated_at "
        "RETURNING id",
        (new_id, name, normalized, etype, now, now),
    )
    row = cur.fetchone()
    return row["id"]


def _insert_mention(
    conn, memory_id, entity_id, surface_form, content, now,
) -> None:
    start = content.find(surface_form)
    if start == -1:
        context_start = None
        context_end = None
    else:
        context_start = start
        context_end = start + len(surface_form)
    try:
        conn.execute(
            "INSERT INTO mentions "
            "(memory_id, entity_id, surface_form, "
            " context_start, context_end, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1.0, ?)",
            (memory_id, entity_id, surface_form,
             context_start, context_end, now),
        )
    except sqlite3.IntegrityError:
        # PRIMARY KEY (memory_id, entity_id, surface_form) hit on
        # idempotent replay — silent skip per spec.
        pass


def _resolve_entity_id(conn, normalized: str, name_to_id: dict) -> str | None:
    for (nn, _t), eid in name_to_id.items():
        if nn == normalized:
            return eid
    row = conn.execute(
        "SELECT id FROM entities WHERE name_normalized = ? "
        "ORDER BY mention_count DESC LIMIT 1",
        (normalized,),
    ).fetchone()
    return row["id"] if row else None


def _insert_relation(
    conn, src_id, tgt_id, rtype, source_memory_id, now,
) -> None:
    try:
        conn.execute(
            "INSERT INTO relations "
            "(id, source_entity_id, target_entity_id, relation_type, "
            " source_memory_id, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1.0, ?)",
            (uuid.uuid4().hex, src_id, tgt_id, rtype,
             source_memory_id, now),
        )
    except sqlite3.IntegrityError:
        # UNIQUE(source_entity_id, target_entity_id, relation_type,
        # source_memory_id) — replay-safe skip.
        pass
