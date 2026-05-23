"""T5 — atomic per-file scan orchestration tests.

``_scan_file()`` is the SOLE writer to ``codebase_scans`` (rev 3 C1).
This file verifies that contract under:

- First-scan path: empty DB → row appears in memories + code_symbols +
  codebase_scans, all FK-consistent.
- Unchanged fast path: identical bytes → returns ``("unchanged", id)``
  with no write.
- Content-change path: new bytes → old code_symbols deleted, new
  symbols inserted, codebase_scans hash updated.
- Force re-scan: same bytes but ``force=True`` → still rewrites.
- Rollback on extract error: ``with conn:`` rolls back the
  ``upsert_file_memory`` and the empty-symbol DELETE so the half-state
  "memory exists but no scan record" cannot persist.
- parent_id linkage survives sibling-shadow (the rev 2 reviewer
  concern that motivated adding the column).

The module skips wholesale when ``[codebase]`` is missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

tsp = pytest.importorskip("tree_sitter_language_pack")

from sage_memory.codebase import _scan_file
from sage_memory.codebase import _extract as extract_module
from sage_memory.db import _migrate


FIXTURE = Path(__file__).parent / "fixtures" / "sample.py"


ALL_MIGRATIONS = (
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


@pytest.fixture
def migrated_db(fresh_db, tmp_migrations_dir, copy_production_migrations):
    copy_production_migrations(*ALL_MIGRATIONS)
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    return fresh_db


@pytest.fixture
def parser():
    return tsp.get_parser("python")


@pytest.fixture
def scanned_db(migrated_db, parser, tmp_path):
    """Copy the fixture into tmp_path and run one scan over it. Returns
    (conn, abs_path, memory_id) for downstream tests.
    """
    abs_path = tmp_path / "sample.py"
    abs_path.write_bytes(FIXTURE.read_bytes())
    status, memory_id = _scan_file(
        migrated_db,
        abs_path=abs_path,
        rel_path="sample.py",
        language_tag="py",
        query_id="py",
        parser=parser,
        force=False,
    )
    assert status == "scanned"
    return migrated_db, abs_path, memory_id


# ---------------------------------------------------------------------------
# First-scan path
# ---------------------------------------------------------------------------


def test_first_scan_returns_scanned_status(scanned_db) -> None:
    conn, _abs_path, memory_id = scanned_db
    assert isinstance(memory_id, str) and memory_id


def test_first_scan_writes_memory_row(scanned_db) -> None:
    conn, _abs_path, memory_id = scanned_db
    row = conn.execute(
        "SELECT title FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    assert row is not None
    assert row["title"] == "[file:py] sample.py"


def test_first_scan_writes_code_symbols(scanned_db) -> None:
    conn, _abs_path, memory_id = scanned_db
    count = conn.execute(
        "SELECT COUNT(*) FROM code_symbols WHERE file_memory_id = ?",
        (memory_id,),
    ).fetchone()[0]
    # Same 7 symbols as test_extract_python.test_symbol_count.
    assert count == 7


def test_first_scan_writes_codebase_scans(scanned_db) -> None:
    conn, abs_path, memory_id = scanned_db
    row = conn.execute(
        "SELECT content_hash, file_memory_id, language FROM codebase_scans "
        "WHERE file_path = ?",
        (str(abs_path),),
    ).fetchone()
    assert row is not None
    assert row["file_memory_id"] == memory_id
    assert row["language"] == "py"
    # content_hash is unsalted sha256 of file bytes — verify by recompute.
    import hashlib
    expected = hashlib.sha256(abs_path.read_bytes()).hexdigest()
    assert row["content_hash"] == expected


def test_first_scan_parent_id_linkage_for_sibling_shadow(scanned_db) -> None:
    """The rev 2 reviewer concern: two ``outer.inner_helper`` symbols
    share qname/kind but must have distinct parent_ids that point to
    DIFFERENT ``outer`` rows.
    """
    conn, _abs_path, memory_id = scanned_db
    inners = conn.execute(
        "SELECT id, parent_id, line_start FROM code_symbols "
        "WHERE qualified_name = 'outer.inner_helper' AND file_memory_id = ? "
        "ORDER BY line_start",
        (memory_id,),
    ).fetchall()
    assert len(inners) == 2
    assert inners[0]["parent_id"] != inners[1]["parent_id"]
    # Each parent_id should point at a row with qname='outer'.
    for inner in inners:
        parent = conn.execute(
            "SELECT qualified_name, line_start FROM code_symbols WHERE id = ?",
            (inner["parent_id"],),
        ).fetchone()
        assert parent["qualified_name"] == "outer"
        # The parent must precede the child in source order.
        assert parent["line_start"] < inner["line_start"]


# ---------------------------------------------------------------------------
# Unchanged fast path
# ---------------------------------------------------------------------------


def test_unchanged_returns_unchanged_status(scanned_db, parser) -> None:
    conn, abs_path, memory_id = scanned_db
    status, mid2 = _scan_file(
        conn,
        abs_path=abs_path,
        rel_path="sample.py",
        language_tag="py",
        query_id="py",
        parser=parser,
        force=False,
    )
    assert status == "unchanged"
    assert mid2 == memory_id


def test_unchanged_does_not_change_symbol_set(scanned_db, parser) -> None:
    conn, abs_path, memory_id = scanned_db
    before = conn.execute(
        "SELECT id FROM code_symbols WHERE file_memory_id = ?", (memory_id,),
    ).fetchall()
    _scan_file(
        conn,
        abs_path=abs_path,
        rel_path="sample.py",
        language_tag="py",
        query_id="py",
        parser=parser,
        force=False,
    )
    after = conn.execute(
        "SELECT id FROM code_symbols WHERE file_memory_id = ?", (memory_id,),
    ).fetchall()
    assert {r["id"] for r in before} == {r["id"] for r in after}


# ---------------------------------------------------------------------------
# Force path
# ---------------------------------------------------------------------------


def test_force_rewrites_even_when_unchanged(scanned_db, parser) -> None:
    conn, abs_path, memory_id = scanned_db
    before_ids = {
        r["id"] for r in conn.execute(
            "SELECT id FROM code_symbols WHERE file_memory_id = ?",
            (memory_id,),
        )
    }
    status, _mid2 = _scan_file(
        conn,
        abs_path=abs_path,
        rel_path="sample.py",
        language_tag="py",
        query_id="py",
        parser=parser,
        force=True,
    )
    assert status == "scanned"
    after_ids = {
        r["id"] for r in conn.execute(
            "SELECT id FROM code_symbols WHERE file_memory_id = ?",
            (memory_id,),
        )
    }
    # Force re-extracts → fresh uuids assigned at extract time → no
    # overlap with the prior insert.
    assert before_ids.isdisjoint(after_ids)
    assert len(after_ids) == 7


# ---------------------------------------------------------------------------
# Content-change path
# ---------------------------------------------------------------------------


def test_content_change_replaces_symbols(scanned_db, parser) -> None:
    conn, abs_path, _memory_id = scanned_db
    abs_path.write_text("def newfn():\n    return 42\n")
    status, mid2 = _scan_file(
        conn,
        abs_path=abs_path,
        rel_path="sample.py",
        language_tag="py",
        query_id="py",
        parser=parser,
        force=False,
    )
    assert status == "scanned"
    # The new memory_id is fresh (T4 creates a new row on content change).
    symbols = conn.execute(
        "SELECT name FROM code_symbols WHERE file_memory_id = ?",
        (mid2,),
    ).fetchall()
    assert [r["name"] for r in symbols] == ["newfn"]


def test_content_change_updates_codebase_scans_hash(scanned_db, parser) -> None:
    conn, abs_path, _memory_id = scanned_db
    new_bytes = b"def newfn():\n    return 42\n"
    abs_path.write_bytes(new_bytes)
    _scan_file(
        conn,
        abs_path=abs_path,
        rel_path="sample.py",
        language_tag="py",
        query_id="py",
        parser=parser,
        force=False,
    )
    row = conn.execute(
        "SELECT content_hash FROM codebase_scans WHERE file_path = ?",
        (str(abs_path),),
    ).fetchone()
    import hashlib
    assert row["content_hash"] == hashlib.sha256(new_bytes).hexdigest()


def test_content_change_keeps_one_codebase_scans_row(scanned_db, parser) -> None:
    """The codebase_scans table is keyed by file_path — content change
    must REPLACE the row, not duplicate it.
    """
    conn, abs_path, _memory_id = scanned_db
    abs_path.write_text("def x(): pass\n")
    _scan_file(
        conn,
        abs_path=abs_path,
        rel_path="sample.py",
        language_tag="py",
        query_id="py",
        parser=parser,
        force=False,
    )
    count = conn.execute(
        "SELECT COUNT(*) FROM codebase_scans WHERE file_path = ?",
        (str(abs_path),),
    ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# Rollback on extract error — rev 3 C1 contract
# ---------------------------------------------------------------------------


def test_rollback_on_extract_failure_leaves_no_partial_state(
    migrated_db, parser, tmp_path, monkeypatch,
) -> None:
    """When ``extract`` raises after ``upsert_file_memory`` has written
    to memories, the surrounding ``with conn:`` block must roll BOTH
    writes back. Verifies the rev 3 C1 atomicity contract.
    """
    abs_path = tmp_path / "boom.py"
    abs_path.write_text("def f(): pass\n")

    def explode(*args, **kwargs):
        raise RuntimeError("simulated parser failure")

    monkeypatch.setattr(extract_module, "extract", explode)

    with pytest.raises(RuntimeError, match="simulated parser failure"):
        _scan_file(
            migrated_db,
            abs_path=abs_path,
            rel_path="boom.py",
            language_tag="py",
            query_id="py",
            parser=parser,
            force=False,
        )

    # Memory upsert must be rolled back along with the failed scan.
    assert migrated_db.execute(
        "SELECT COUNT(*) FROM memories WHERE tags LIKE '%codebase%'"
    ).fetchone()[0] == 0
    assert migrated_db.execute(
        "SELECT COUNT(*) FROM codebase_scans"
    ).fetchone()[0] == 0
    assert migrated_db.execute(
        "SELECT COUNT(*) FROM code_symbols"
    ).fetchone()[0] == 0


def test_rollback_on_extract_failure_after_prior_scan(
    scanned_db, parser, monkeypatch,
) -> None:
    """A prior successful scan exists; the second call (on the same
    file with changed bytes) fails mid-extract. The prior row in
    ``codebase_scans`` must remain intact — the failed scan does NOT
    corrupt the existing record.
    """
    conn, abs_path, memory_id = scanned_db
    before_scans = conn.execute(
        "SELECT file_path, content_hash, file_memory_id FROM codebase_scans"
    ).fetchall()
    before_symbols = conn.execute(
        "SELECT id FROM code_symbols WHERE file_memory_id = ?", (memory_id,),
    ).fetchall()

    abs_path.write_text("def changed(): pass\n")

    def explode(*args, **kwargs):
        raise RuntimeError("simulated failure on rescan")

    monkeypatch.setattr(extract_module, "extract", explode)

    with pytest.raises(RuntimeError, match="simulated failure on rescan"):
        _scan_file(
            conn,
            abs_path=abs_path,
            rel_path="sample.py",
            language_tag="py",
            query_id="py",
            parser=parser,
            force=False,
        )

    after_scans = conn.execute(
        "SELECT file_path, content_hash, file_memory_id FROM codebase_scans"
    ).fetchall()
    after_symbols = conn.execute(
        "SELECT id FROM code_symbols WHERE file_memory_id = ?", (memory_id,),
    ).fetchall()

    # codebase_scans untouched.
    assert [tuple(r) for r in before_scans] == [tuple(r) for r in after_scans]
    # code_symbols for the prior memory unchanged (the rollback restores
    # the rows DELETEd inside the `with conn:` block).
    assert {r["id"] for r in before_symbols} == {r["id"] for r in after_symbols}
