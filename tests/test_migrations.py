"""Migration runner tests — Phase 1 (T1) and Phase 2 (T2-T6) of M1.

These tests exercise the refactored `_migrate(conn, migrations_dir=...)`
runner with the PHASE A (virtual tables outside txn) / PHASE B
(BEGIN/COMMIT for non-virtual DDL + PRAGMA user_version) split.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sage_memory.db import _migrate


# ═══════════════════════════════════════════════════════════════════
# T1 — Migration runner: PHASE A/B + rollback semantics
# ═══════════════════════════════════════════════════════════════════


FORCE_FAIL_SQL = """\
-- Test fixture: PHASE B failure to verify rollback.
-- The CREATE TABLE statement is valid; the INSERT references a
-- nonexistent table, raising sqlite3.OperationalError at execute time.
CREATE TABLE valid_table_in_phase_b (x INTEGER);
INSERT INTO nonexistent VALUES (1);
"""


def test_phase_b_rollback(fresh_db, tmp_migrations_dir, copy_production_migrations):
    """A failure inside PHASE B's BEGIN/COMMIT must roll back the entire
    file's DDL and leave user_version untouched."""
    copy_production_migrations("001_initial.sql", "002_edges.sql")
    (tmp_migrations_dir / "999_force_fail.sql").write_text(FORCE_FAIL_SQL)

    # 001 + 002 should apply fine; 999 should raise and roll back.
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        _migrate(fresh_db, migrations_dir=tmp_migrations_dir)

    # user_version stays at 2 — 999's PHASE B rolled back atomically.
    user_version = fresh_db.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == 2, (
        f"expected user_version=2 after rollback, got {user_version}"
    )

    # The valid table from 999's PHASE B must NOT exist (rolled back).
    tables = {row[0] for row in fresh_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "valid_table_in_phase_b" not in tables, (
        "PHASE B rollback failed: valid_table_in_phase_b persists"
    )

    # 001's tables DID land (they applied successfully before 999 failed).
    assert "memories" in tables


def test_multi_file_partial_failure(
    fresh_db, tmp_migrations_dir, copy_production_migrations
):
    """If 001 + 002 + a third migration fail sequentially, the third's
    failure must roll back only itself — not retroactively undo 001 or 002.
    user_version stays at 2 (the last successful migration)."""
    copy_production_migrations("001_initial.sql", "002_edges.sql")
    # Use 003_force_fail.sql in the tmp dir (does not collide with the
    # real production 003 because we point _migrate at the tmp dir).
    (tmp_migrations_dir / "003_force_fail.sql").write_text(FORCE_FAIL_SQL)

    with pytest.raises(sqlite3.OperationalError):
        _migrate(fresh_db, migrations_dir=tmp_migrations_dir)

    # user_version is 2 (001 + 002 applied; 003 rolled back).
    user_version = fresh_db.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == 2

    # 002's edges table exists.
    tables = {row[0] for row in fresh_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "edges" in tables
    assert "valid_table_in_phase_b" not in tables


def test_migrate_default_migrations_dir(fresh_db):
    """Back-compat: calling _migrate(conn) with no migrations_dir
    defaults to the production migrations directory and applies all
    current migrations."""
    _migrate(fresh_db)

    # After running production migrations, user_version should be at
    # whatever the highest existing migration number is. Pre-M1: 2.
    # During M1 build: jumps to 6 as 003-006 land.
    user_version = fresh_db.execute("PRAGMA user_version").fetchone()[0]
    assert user_version >= 2, "production migrations 001/002 must apply"

    tables = {row[0] for row in fresh_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "memories" in tables
    assert "edges" in tables


def test_phase_a_virtual_table_survives_phase_b_failure(
    fresh_db, tmp_migrations_dir, copy_production_migrations
):
    """A virtual table created in PHASE A persists harmlessly if PHASE B
    of the same migration rolls back. IF NOT EXISTS guarantees a retry
    is safe."""
    copy_production_migrations("001_initial.sql")

    # Synthesize a migration with: virtual-table CREATE (PHASE A) +
    # failing non-virtual DDL (PHASE B).
    mixed = """\
CREATE VIRTUAL TABLE IF NOT EXISTS test_fts_in_phase_a USING fts5(
    content
);
INSERT INTO nonexistent VALUES (1);
"""
    (tmp_migrations_dir / "002_mixed.sql").write_text(mixed)

    with pytest.raises(sqlite3.OperationalError):
        _migrate(fresh_db, migrations_dir=tmp_migrations_dir)

    # user_version stays at 1 (002's PHASE B rolled back).
    user_version = fresh_db.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == 1

    # But the virtual table from PHASE A persists (no transaction
    # protected it). This is acceptable because IF NOT EXISTS makes
    # a retry safe.
    tables = {row[0] for row in fresh_db.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual')"
    )}
    # FTS5 virtual tables show as 'table' in sqlite_master
    assert "test_fts_in_phase_a" in tables, (
        "PHASE A virtual table should persist across PHASE B rollback"
    )


# ═══════════════════════════════════════════════════════════════════
# T2 — Migration 003_chunks.sql
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def migrated_db_through_003(fresh_db, tmp_migrations_dir, copy_production_migrations):
    """fresh_db with migrations 001 + 002 + 003 applied."""
    copy_production_migrations("001_initial.sql", "002_edges.sql", "003_chunks.sql")
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    return fresh_db


def test_003_applies_clean(migrated_db_through_003):
    db = migrated_db_through_003
    assert db.execute("PRAGMA user_version").fetchone()[0] == 3

    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "chunks" in tables
    assert "chunks_fts" in tables  # FTS5 shows as table
    assert "chunks_vec" in tables  # vec0 shows as table


def test_003_idempotency(
    fresh_db, tmp_migrations_dir, copy_production_migrations
):
    copy_production_migrations("001_initial.sql", "002_edges.sql", "003_chunks.sql")
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    # Reset user_version to pretend the migration hadn't been applied,
    # then re-run. IF NOT EXISTS clauses must make this a no-op.
    fresh_db.execute("PRAGMA user_version = 2")
    fresh_db.commit()
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    # Should reach user_version=3 again without error
    assert fresh_db.execute("PRAGMA user_version").fetchone()[0] == 3


def test_003_cascade_delete(migrated_db_through_003):
    db = migrated_db_through_003
    db.execute(
        "INSERT INTO memories(id,title,content,content_hash,created_at,updated_at,accessed_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("m1", "t", "c", "h1", 1.0, 1.0, 1.0),
    )
    db.execute(
        "INSERT INTO chunks(id,memory_id,chunk_index,content,byte_start,byte_end,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("c1", "m1", 0, "chunk content", 0, 13, 1.0),
    )
    db.commit()
    # Delete the memory; chunk should cascade.
    db.execute("DELETE FROM memories WHERE id = ?", ("m1",))
    db.commit()
    remaining = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert remaining == 0


def test_003_fts_trigger_sync(migrated_db_through_003):
    db = migrated_db_through_003
    # Need a memory first (chunks FK references it)
    db.execute(
        "INSERT INTO memories(id,title,content,content_hash,created_at,updated_at,accessed_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("m1", "t", "c", "h1", 1.0, 1.0, 1.0),
    )
    db.execute(
        "INSERT INTO chunks(id,memory_id,chunk_index,content,byte_start,byte_end,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("c1", "m1", 0, "the quick brown fox", 0, 19, 1.0),
    )
    db.commit()
    # FTS5 search should find the chunk via the sync trigger.
    rows = db.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'quick'"
    ).fetchall()
    assert len(rows) == 1


def test_003_vec_table_dim(migrated_db_through_003):
    """The vec0 virtual table is created with float[384] embedding."""
    db = migrated_db_through_003
    # vec0 doesn't expose schema easily — check the create statement.
    sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE name='chunks_vec'"
    ).fetchone()[0]
    assert "float[384]" in sql


# ═══════════════════════════════════════════════════════════════════
# T3 — Migration 004_entities.sql
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def migrated_db_through_004(fresh_db, tmp_migrations_dir, copy_production_migrations):
    copy_production_migrations(
        "001_initial.sql", "002_edges.sql", "003_chunks.sql", "004_entities.sql",
    )
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    return fresh_db


def test_004_applies_clean(migrated_db_through_004):
    db = migrated_db_through_004
    assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    for t in ("entities", "mentions", "relations"):
        assert t in tables


def test_004_cascade_on_memory_delete(migrated_db_through_004):
    db = migrated_db_through_004
    # Insert memory + entity + mention + relation
    db.execute(
        "INSERT INTO memories(id,title,content,content_hash,created_at,updated_at,accessed_at) "
        "VALUES ('m1','t','c','h1',1,1,1)"
    )
    db.execute(
        "INSERT INTO entities(id,name,name_normalized,type,created_at,updated_at) "
        "VALUES ('e1','API','api','CONCEPT',1,1)"
    )
    db.execute(
        "INSERT INTO mentions(memory_id,entity_id,surface_form,confidence,created_at) "
        "VALUES ('m1','e1','API',1.0,1)"
    )
    db.execute(
        "INSERT INTO relations(id,source_entity_id,target_entity_id,relation_type,source_memory_id,confidence,created_at) "
        "VALUES ('r1','e1','e1','mentions','m1',1.0,1)"
    )
    db.commit()
    # Delete memory: mentions cascade gone, relation.source_memory_id SET NULL
    db.execute("DELETE FROM memories WHERE id='m1'")
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM mentions").fetchone()[0] == 0
    rel_src_mem = db.execute("SELECT source_memory_id FROM relations").fetchone()[0]
    assert rel_src_mem is None  # SET NULL


def test_004_cascade_on_entity_delete(migrated_db_through_004):
    db = migrated_db_through_004
    db.execute(
        "INSERT INTO memories(id,title,content,content_hash,created_at,updated_at,accessed_at) "
        "VALUES ('m1','t','c','h1',1,1,1)"
    )
    db.execute(
        "INSERT INTO entities(id,name,name_normalized,type,created_at,updated_at) "
        "VALUES ('e1','A','a','CONCEPT',1,1),('e2','B','b','CONCEPT',1,1)"
    )
    db.execute(
        "INSERT INTO mentions(memory_id,entity_id,surface_form,confidence,created_at) "
        "VALUES ('m1','e1','A',1.0,1),('m1','e2','B',1.0,1)"
    )
    db.execute(
        "INSERT INTO relations(id,source_entity_id,target_entity_id,relation_type,confidence,created_at) "
        "VALUES ('r1','e1','e2','relates_to',1.0,1)"
    )
    db.commit()
    db.execute("DELETE FROM entities WHERE id='e1'")
    db.commit()
    # Both mention referencing e1 and relation referencing e1 should cascade away.
    assert db.execute("SELECT COUNT(*) FROM mentions WHERE entity_id='e1'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM relations WHERE source_entity_id='e1'").fetchone()[0] == 0


def test_004_canonical_id_self_ref_set_null(migrated_db_through_004):
    db = migrated_db_through_004
    db.execute(
        "INSERT INTO entities(id,name,name_normalized,type,created_at,updated_at) "
        "VALUES ('e1','A','a','CONCEPT',1,1)"
    )
    db.execute(
        "INSERT INTO entities(id,name,name_normalized,type,canonical_id,created_at,updated_at) "
        "VALUES ('e2','A2','a2','CONCEPT','e1',1,1)"
    )
    db.commit()
    db.execute("DELETE FROM entities WHERE id='e1'")
    db.commit()
    cano = db.execute("SELECT canonical_id FROM entities WHERE id='e2'").fetchone()[0]
    assert cano is None  # ON DELETE SET NULL


def test_004_unique_constraint(migrated_db_through_004):
    db = migrated_db_through_004
    db.execute(
        "INSERT INTO entities(id,name,name_normalized,type,created_at,updated_at) "
        "VALUES ('e1','A','a','CONCEPT',1,1)"
    )
    db.commit()
    # Duplicate (name_normalized, type) → IntegrityError
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO entities(id,name,name_normalized,type,created_at,updated_at) "
            "VALUES ('e2','A','a','CONCEPT',1,1)"
        )


# ═══════════════════════════════════════════════════════════════════
# T4 — Migration 005_embedding_meta.sql
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def migrated_db_through_005(fresh_db, tmp_migrations_dir, copy_production_migrations):
    copy_production_migrations(
        "001_initial.sql", "002_edges.sql", "003_chunks.sql",
        "004_entities.sql", "005_embedding_meta.sql",
    )
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    return fresh_db


def test_005_applies_clean(migrated_db_through_005):
    db = migrated_db_through_005
    assert db.execute("PRAGMA user_version").fetchone()[0] == 5
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    for t in ("memory_embedding_meta", "chunk_embedding_meta", "corpus_meta"):
        assert t in tables


def test_005_corpus_meta_default(migrated_db_through_005):
    db = migrated_db_through_005
    vec_dim = db.execute(
        "SELECT value FROM corpus_meta WHERE key='vec_dim'"
    ).fetchone()[0]
    assert vec_dim == "384"


def test_005_backfill_embedded_rows(
    fresh_db, tmp_migrations_dir, copy_production_migrations
):
    """Insert embedded=1 memories BEFORE migration 005 runs, then verify
    the backfill INSERT created one meta row per embedded memory with
    ('legacy', '0', 384)."""
    copy_production_migrations(
        "001_initial.sql", "002_edges.sql", "003_chunks.sql", "004_entities.sql",
    )
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    # Insert 3 embedded + 2 unembedded
    for i in range(3):
        fresh_db.execute(
            "INSERT INTO memories(id,title,content,content_hash,embedded,created_at,updated_at,accessed_at) "
            "VALUES (?,?,?,?,1,?,?,?)",
            (f"em{i}", f"t{i}", f"c{i}", f"h{i}", 100.0+i, 100.0+i, 100.0+i),
        )
    for i in range(2):
        fresh_db.execute(
            "INSERT INTO memories(id,title,content,content_hash,embedded,created_at,updated_at,accessed_at) "
            "VALUES (?,?,?,?,0,?,?,?)",
            (f"un{i}", f"t{i}", f"c{i}", f"unh{i}", 200.0+i, 200.0+i, 200.0+i),
        )
    fresh_db.commit()

    # Now apply 005 — the backfill runs.
    copy_production_migrations("005_embedding_meta.sql")
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)

    # Exactly 3 meta rows; all embedded=1 covered, no rows for embedded=0.
    rows = fresh_db.execute(
        "SELECT memory_id, model_name, model_version, dim FROM memory_embedding_meta ORDER BY memory_id"
    ).fetchall()
    assert len(rows) == 3
    for row in rows:
        assert row[0].startswith("em"), f"unexpected backfill row: {dict(row)}"
        assert row[1] == "legacy"
        assert row[2] == "0"
        assert row[3] == 384


def test_005_backfill_skips_unembedded(migrated_db_through_005):
    """embedded=0 memories get NO meta row — they stay stale (spec A4)."""
    db = migrated_db_through_005
    db.execute(
        "INSERT INTO memories(id,title,content,content_hash,embedded,created_at,updated_at,accessed_at) "
        "VALUES ('u1','t','c','h1',0,1,1,1)"
    )
    db.commit()
    # The backfill INSERT only fires for embedded=1 rows at migration time.
    # A new embedded=0 row inserted post-migration also has no meta.
    cnt = db.execute(
        "SELECT COUNT(*) FROM memory_embedding_meta WHERE memory_id='u1'"
    ).fetchone()[0]
    assert cnt == 0


def test_005_cascade_memory_delete(migrated_db_through_005):
    db = migrated_db_through_005
    db.execute(
        "INSERT INTO memories(id,title,content,content_hash,created_at,updated_at,accessed_at) "
        "VALUES ('m1','t','c','h1',1,1,1)"
    )
    db.execute(
        "INSERT INTO memory_embedding_meta(memory_id,model_name,model_version,dim,created_at) "
        "VALUES ('m1','local','tfidf-v1',384,1)"
    )
    db.commit()
    db.execute("DELETE FROM memories WHERE id='m1'")
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM memory_embedding_meta").fetchone()[0] == 0


def test_005_cascade_chunk_delete(migrated_db_through_005):
    db = migrated_db_through_005
    db.execute(
        "INSERT INTO memories(id,title,content,content_hash,created_at,updated_at,accessed_at) "
        "VALUES ('m1','t','c','h1',1,1,1)"
    )
    db.execute(
        "INSERT INTO chunks(id,memory_id,chunk_index,content,byte_start,byte_end,created_at) "
        "VALUES ('c1','m1',0,'x',0,1,1)"
    )
    db.execute(
        "INSERT INTO chunk_embedding_meta(chunk_id,model_name,model_version,dim,created_at) "
        "VALUES ('c1','local','tfidf-v1',384,1)"
    )
    db.commit()
    db.execute("DELETE FROM chunks WHERE id='c1'")
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM chunk_embedding_meta").fetchone()[0] == 0


# ═══════════════════════════════════════════════════════════════════
# T5 — Migration 006_extraction_queue.sql
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def migrated_db_full(fresh_db, tmp_migrations_dir, copy_production_migrations):
    """All M1 migrations applied (001-006)."""
    copy_production_migrations(
        "001_initial.sql", "002_edges.sql", "003_chunks.sql",
        "004_entities.sql", "005_embedding_meta.sql", "006_extraction_queue.sql",
    )
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    return fresh_db


def test_006_applies_clean(migrated_db_full):
    db = migrated_db_full
    assert db.execute("PRAGMA user_version").fetchone()[0] == 6
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "extraction_queue" in tables


def test_006_started_at_nullable(migrated_db_full):
    db = migrated_db_full
    db.execute(
        "INSERT INTO memories(id,title,content,content_hash,created_at,updated_at,accessed_at) "
        "VALUES ('m1','t','c','h1',1,1,1)"
    )
    db.execute(
        "INSERT INTO extraction_queue(id,memory_id,task_type,status,created_at) "
        "VALUES ('q1','m1','extract','pending',1)"
    )
    db.commit()
    started_at = db.execute(
        "SELECT started_at FROM extraction_queue WHERE id='q1'"
    ).fetchone()[0]
    assert started_at is None


def test_006_cascade_memory_delete(migrated_db_full):
    db = migrated_db_full
    db.execute(
        "INSERT INTO memories(id,title,content,content_hash,created_at,updated_at,accessed_at) "
        "VALUES ('m1','t','c','h1',1,1,1)"
    )
    db.execute(
        "INSERT INTO extraction_queue(id,memory_id,task_type,status,created_at) "
        "VALUES ('q1','m1','extract','pending',1)"
    )
    db.commit()
    db.execute("DELETE FROM memories WHERE id='m1'")
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM extraction_queue").fetchone()[0] == 0


def test_006_partial_index_exists(migrated_db_full):
    db = migrated_db_full
    # The partial index has a WHERE clause; check sqlite_master.
    sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE name='idx_queue_pending'"
    ).fetchone()[0]
    assert "WHERE" in sql.upper()
    assert "'pending'" in sql or '"pending"' in sql or "pending" in sql.lower()


# ═══════════════════════════════════════════════════════════════════
# T6 — Migration-order invariant test
# ═══════════════════════════════════════════════════════════════════


def test_migration_order_invariant(
    fresh_db, tmp_migrations_dir, copy_production_migrations
):
    """The documented migration order (003 chunks → 005 embedding_meta)
    matters because chunk_embedding_meta.chunk_id REFERENCES chunks(id).

    SQLite does NOT validate FK existence at CREATE TABLE time (FKs are
    checked at row-level operations). So the order invariant is provable
    at the practical failure point: an INSERT into chunk_embedding_meta
    fails with FK error when the parent chunks table doesn't exist.
    This is the actual production failure mode if the migrations were
    ever reordered. The ADR's documented order prevents this.
    """
    import shutil
    copy_production_migrations("001_initial.sql", "002_edges.sql")
    # Copy 005's body into a slot at version 3, WITHOUT including 003.
    src = (Path(__file__).parent.parent
           / "src" / "sage_memory" / "migrations" / "005_embedding_meta.sql")
    shutil.copy2(src, tmp_migrations_dir / "003_premature_chunk_meta.sql")

    # The migration applies successfully (CREATE TABLE doesn't validate
    # FK references). PRAGMA user_version → 3.
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    assert fresh_db.execute("PRAGMA user_version").fetchone()[0] == 3

    # But any INSERT into chunk_embedding_meta will fail at runtime
    # because chunks(id) doesn't exist (foreign_keys=ON is set in fresh_db).
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        fresh_db.execute(
            "INSERT INTO chunk_embedding_meta(chunk_id,model_name,model_version,dim,created_at) "
            "VALUES ('c1','local','tfidf-v1',384,1)"
        )
