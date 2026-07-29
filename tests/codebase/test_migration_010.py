"""P1-1 — Migration 010 (unresolved-relations partial index).

Pins the append-only migration contract: 010 applies cleanly on top
of a 009-era DB, user_version advances to 10, and the partial index
exists with its WHERE clause.
"""

from __future__ import annotations

import pytest

from sage_memory.db import _migrate


THROUGH_009 = (
    "001_initial.sql",
    "002_edges.sql",
    "003_memory_health.sql",
    "004_chunks.sql",
    "005_entities.sql",
    "006_embedding_meta.sql",
    "007_extraction_queue.sql",
    "008_worker_state.sql",
    "009_code_symbols.sql",
)
MIGRATION_010 = "010_unresolved_relations_index.sql"


@pytest.fixture
def migrated_009_db(fresh_db, tmp_migrations_dir, copy_production_migrations):
    copy_production_migrations(*THROUGH_009)
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    return fresh_db


def test_010_applies_on_009_db_and_advances_version(
    migrated_009_db, tmp_migrations_dir, copy_production_migrations,
):
    conn = migrated_009_db
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9

    copy_production_migrations(MIGRATION_010)
    _migrate(conn, migrations_dir=tmp_migrations_dir)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 10

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='idx_code_relations_target_name_unresolved'"
    ).fetchone()
    assert row is not None, "partial index missing after 010"
    assert "WHERE target_symbol_id IS NULL" in row[0]


def test_010_absent_before_migration(migrated_009_db):
    row = migrated_009_db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='idx_code_relations_target_name_unresolved'"
    ).fetchone()
    assert row is None


def test_010_idempotent(
    migrated_009_db, tmp_migrations_dir, copy_production_migrations,
):
    """Applying 010 twice is a no-op (CREATE INDEX IF NOT EXISTS)."""
    conn = migrated_009_db
    copy_production_migrations(MIGRATION_010)
    _migrate(conn, migrations_dir=tmp_migrations_dir)
    _migrate(conn, migrations_dir=tmp_migrations_dir)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
