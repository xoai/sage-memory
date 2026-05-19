"""Task 1 — Unit tests for `extraction_write.py`.

Refactor extraction-write logic out of `worker._do_extract` into a
shared helper used by both the worker and the agent-driven store path
(Task 3). This file covers the helper directly with a real sqlite
connection; worker integration tests stay in `tests/test_worker.py`.
"""

from __future__ import annotations

import sqlite3
import time
import uuid

import pytest

from sage_memory import extraction_write
from sage_memory import db as _db


@pytest.fixture
def memory_conn(tmp_path, monkeypatch):
    """Fresh project DB with one memory row inserted; returns the conn
    and the inserted memory_id."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    _db.override_project_root(tmp_path)
    _db.close_all()
    conn = _db.get_db("project")

    memory_id = uuid.uuid4().hex
    now = time.time()
    content_hash = "deadbeef" * 8
    conn.execute(
        "INSERT INTO memories "
        "(id, content, title, tags, "
        " content_hash, created_at, updated_at, accessed_at, access_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (memory_id, "PaymentOrchestrator coordinates StripeGateway and LedgerService.",
         "Payment saga", "[]", content_hash, now, now, now),
    )
    conn.commit()
    yield conn, memory_id
    _db.close_all()
    _db.override_project_root(None)


# ───── Public API: write_extraction ─────

def test_write_extraction_creates_entities_mentions_relations(memory_conn):
    conn, memory_id = memory_conn
    entities = [
        {"name": "PaymentOrchestrator", "type": "CONCEPT"},
        {"name": "StripeGateway", "type": "CONCEPT"},
    ]
    relations = [
        {"source_name": "PaymentOrchestrator",
         "target_name": "StripeGateway", "type": "depends_on"},
    ]
    extraction_write.write_extraction(
        conn, memory_id,
        "PaymentOrchestrator coordinates StripeGateway and LedgerService.",
        entities, relations, time.time(),
    )

    ent_rows = conn.execute(
        "SELECT name, type, mention_count FROM entities ORDER BY name"
    ).fetchall()
    assert len(ent_rows) == 2
    assert ent_rows[0]["name"] == "PaymentOrchestrator"
    assert ent_rows[0]["type"] == "CONCEPT"
    assert ent_rows[0]["mention_count"] == 1

    mention_rows = conn.execute(
        "SELECT memory_id FROM mentions"
    ).fetchall()
    assert len(mention_rows) == 2
    assert all(r["memory_id"] == memory_id for r in mention_rows)

    rel_rows = conn.execute(
        "SELECT relation_type, source_memory_id FROM relations"
    ).fetchall()
    assert len(rel_rows) == 1
    assert rel_rows[0]["relation_type"] == "depends_on"
    assert rel_rows[0]["source_memory_id"] == memory_id


def test_write_extraction_upserts_existing_entity(memory_conn):
    """Second call with same (name_normalized, type) increments
    mention_count rather than inserting a duplicate."""
    conn, memory_id = memory_conn
    ent = [{"name": "Stripe", "type": "TECHNOLOGY"}]
    extraction_write.write_extraction(
        conn, memory_id, "Stripe is used here.", ent, [], time.time()
    )
    extraction_write.write_extraction(
        conn, memory_id, "Stripe is used here.", ent, [], time.time()
    )
    rows = conn.execute(
        "SELECT mention_count FROM entities WHERE name = 'Stripe'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["mention_count"] == 2


def test_write_extraction_mention_context_offsets(memory_conn):
    """Surface form found in content → context_start/end populated."""
    conn, memory_id = memory_conn
    content = "PaymentOrchestrator coordinates StripeGateway and LedgerService."
    extraction_write.write_extraction(
        conn, memory_id, content,
        [{"name": "StripeGateway", "type": "CONCEPT",
          "surface_form": "StripeGateway"}],
        [], time.time(),
    )
    row = conn.execute(
        "SELECT context_start, context_end FROM mentions"
    ).fetchone()
    expected_start = content.find("StripeGateway")
    assert row["context_start"] == expected_start
    assert row["context_end"] == expected_start + len("StripeGateway")


def test_write_extraction_mention_offset_null_when_surface_form_absent(memory_conn):
    """Surface form NOT in content → context offsets are NULL (mirrors
    worker behavior at worker.py:396-402)."""
    conn, memory_id = memory_conn
    extraction_write.write_extraction(
        conn, memory_id, "PaymentOrchestrator only.",
        [{"name": "StripeGateway", "type": "CONCEPT"}],
        [], time.time(),
    )
    row = conn.execute(
        "SELECT context_start, context_end FROM mentions"
    ).fetchone()
    assert row["context_start"] is None
    assert row["context_end"] is None


def test_write_extraction_drops_unresolvable_relations(memory_conn, caplog):
    """A relation whose source_name doesn't match any entity in this
    call OR in the DB is silently dropped. Matches worker's `if src_id
    is None or tgt_id is None: continue` (worker.py:364)."""
    conn, memory_id = memory_conn
    extraction_write.write_extraction(
        conn, memory_id, "PaymentOrchestrator only.",
        [{"name": "PaymentOrchestrator", "type": "CONCEPT"}],
        [{"source_name": "PaymentOrchestrator",
          "target_name": "NonExistent",
          "type": "depends_on"}],
        time.time(),
    )
    rel_count = conn.execute(
        "SELECT COUNT(*) AS n FROM relations"
    ).fetchone()["n"]
    assert rel_count == 0


def test_write_extraction_resolves_relation_against_existing_entity(memory_conn):
    """Relation source_name matches an entity already in the DB (from
    a prior write); we don't need to re-pass it in this call."""
    conn, memory_id = memory_conn
    # First call: store PaymentOrchestrator
    extraction_write.write_extraction(
        conn, memory_id, "PaymentOrchestrator only.",
        [{"name": "PaymentOrchestrator", "type": "CONCEPT"}],
        [], time.time(),
    )
    # Second call: only pass StripeGateway, but reference both in
    # a relation
    extraction_write.write_extraction(
        conn, memory_id, "StripeGateway in use.",
        [{"name": "StripeGateway", "type": "CONCEPT"}],
        [{"source_name": "PaymentOrchestrator",
          "target_name": "StripeGateway",
          "type": "depends_on"}],
        time.time(),
    )
    rel_count = conn.execute(
        "SELECT COUNT(*) AS n FROM relations"
    ).fetchone()["n"]
    assert rel_count == 1


def test_write_extraction_idempotent_mention(memory_conn):
    """Re-inserting the same (memory_id, entity_id, surface_form) is
    a silent no-op (PRIMARY KEY constraint)."""
    conn, memory_id = memory_conn
    ent = [{"name": "Stripe", "type": "TECHNOLOGY", "surface_form": "Stripe"}]
    extraction_write.write_extraction(
        conn, memory_id, "Stripe Stripe.", ent, [], time.time()
    )
    # Second write with same surface form — no IntegrityError raised
    extraction_write.write_extraction(
        conn, memory_id, "Stripe Stripe.", ent, [], time.time()
    )
    mention_count = conn.execute(
        "SELECT COUNT(*) AS n FROM mentions"
    ).fetchone()["n"]
    assert mention_count == 1


def test_write_extraction_idempotent_relation(memory_conn):
    """Re-inserting the same (source, target, relation_type,
    source_memory_id) is a silent no-op."""
    conn, memory_id = memory_conn
    ent = [
        {"name": "A", "type": "CONCEPT"},
        {"name": "B", "type": "CONCEPT"},
    ]
    rel = [{"source_name": "A", "target_name": "B", "type": "depends_on"}]
    extraction_write.write_extraction(
        conn, memory_id, "A depends on B.", ent, rel, time.time()
    )
    extraction_write.write_extraction(
        conn, memory_id, "A depends on B.", ent, rel, time.time()
    )
    rel_count = conn.execute(
        "SELECT COUNT(*) AS n FROM relations"
    ).fetchone()["n"]
    assert rel_count == 1


def test_write_extraction_empty_inputs(memory_conn):
    """Empty entities + relations → no-op, no exception."""
    conn, memory_id = memory_conn
    extraction_write.write_extraction(
        conn, memory_id, "content", [], [], time.time(),
    )
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM entities"
    ).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM mentions"
    ).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM relations"
    ).fetchone()["n"] == 0


def test_worker_path_uses_same_helper(memory_conn, monkeypatch):
    """Refactor invariant: worker._do_extract calls
    extraction_write.write_extraction with the same arguments it used
    to use internally. Verified by spying on the helper."""
    from sage_memory import worker as _worker_mod

    conn, memory_id = memory_conn
    captured = {}

    def _spy(conn_arg, mid, content, ents, rels, now):
        captured["called"] = True
        captured["memory_id"] = mid
        captured["entities"] = ents
        captured["relations"] = rels

    monkeypatch.setattr(
        extraction_write, "write_extraction", _spy,
    )

    # Stub extractor to return a known result without LLM call
    monkeypatch.setattr(
        _worker_mod._extractor, "extract",
        lambda _c, **_kw: {
            "entities": [{"name": "Test", "type": "CONCEPT"}],
            "relations": [],
        },
    )

    worker_instance = _worker_mod.Worker.__new__(_worker_mod.Worker)
    worker_instance._do_extract(conn, memory_id)

    assert captured.get("called") is True
    assert captured["memory_id"] == memory_id
    assert captured["entities"] == [{"name": "Test", "type": "CONCEPT"}]
    assert captured["relations"] == []
