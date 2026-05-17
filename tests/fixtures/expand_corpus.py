"""Shared FTS5 fixture corpus for M4 expand.py + search.py tests.

Provides four scenarios exercising the spec A1 strong-signal short-
circuit decision boundary (ADR-004 §"Strong-signal short-circuit").
Each scenario builds its own in-memory sqlite DB so tests run in
isolation. Consumed by `tests/test_expand.py` (T1) and
`tests/test_search_expand_rerank.py` (T4).

Why per-scenario DBs instead of one shared corpus:
  BM25 scores depend on global corpus statistics (avgdl, doc count).
  Per-scenario isolation pins each test's expected normalized-score
  RANGE, not exact bm25() output (which drifts with SQLite version
  + tokenizer changes). Tests assert on the SHORT-CIRCUIT DECISION
  ("does it fire?") rather than literal numbers.

Tolerance contract:
  Raw bm25() output is NOT pinned. SQLite FTS5's bm25 magnitude is
  sensitive to SQLite version, corpus size, doc length distribution
  and tokenizer choice. What IS pinned per scenario is the BOOLEAN
  outcome of the strong-signal predicate at default thresholds
  (`SAGE_EXPAND_TOP1_NORM=0.4`, `SAGE_EXPAND_TOP1_RATIO=2.0`):
    - "strong"               → short-circuit FIRES (1 hit, ratio
                                gate auto-passes for single hit)
    - "high-top1-ambiguous"  → short-circuit FAILS (2 hits both
                                with title match, ratio < 2)
    - "ambiguous-all-weak"   → short-circuit FAILS (3 hits with
                                comparable bm25, ratio < 2)
    - "low-confidence"       → short-circuit FAILS (0 hits, empty
                                input)

  Observed bm25 ranges (SQLite 3.40+, our 50-doc filler corpus,
  May 2026, recorded by the smoke check in T0):
    strong               top1 ≈ -15.1   (norm ≈ 0.94)
    high-top1-ambiguous  top1 ≈ -13.0, top2 ≈ -13.0   (norms ≈ 0.93/0.93)
    ambiguous-all-weak   top1..3 ≈ -8.0..-8.6   (norms ≈ 0.89)
    low-confidence       empty (no matching docs)

Plan reference: .sage/work/20260516-retrieval-upgrade/M4/plan.md §T0
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import sqlite_vec


# Resolve production migrations dir; T0 only needs 001_initial.sql.
_PROD_MIGRATIONS_DIR = (
    Path(__file__).parent.parent.parent
    / "src" / "sage_memory" / "migrations"
)


@dataclass(frozen=True)
class Scenario:
    """A single A1 scenario: a small corpus + a query.

    A pool of unrelated `filler` docs is added to EVERY scenario to
    give FTS5 a meaningful corpus-level IDF signal. Without filler,
    bm25() on a 3-doc corpus produces values in [-0.001, -0.00001]
    range that never normalize above 0.001 — the strong-signal
    formula `abs(s)/(1+abs(s))` requires |s| >~ 0.67 to clear the
    0.4 threshold, which only happens at meaningful corpus sizes.
    """

    name: str
    query: str
    documents: tuple[tuple[str, str], ...]  # (title, content) per doc
    expected: str  # human-readable description for failing-test triage


# Filler corpus: ~50 unrelated docs added to every scenario so FTS5's
# IDF signal produces bm25 magnitudes consistent with ADR-004's
# worked-example regime (raw bm25 in [-30, -1] range).
_FILLER_DOCS: tuple[tuple[str, str], ...] = tuple(
    (
        f"filler topic {i}",
        f"this is unrelated filler content number {i} about everyday "
        f"subjects like weather, cooking, gardening, sports, music, "
        f"travel, and miscellaneous trivia. " + "lorem ipsum dolor " * 15,
    )
    for i in range(50)
)


# Four scenarios mirror spec A1's worked examples. Each scenario's
# corpus is hand-crafted to produce the documented normalized-score
# regime, NOT to hit a specific raw bm25 number.

SCENARIO_STRONG = Scenario(
    name="strong",
    query="quintarius ozymandias",
    documents=(
        # Doc 1: TITLE match + dense content match → strong bm25.
        # Docs 2 and 3 don't match at all → top2 absent (None branch
        # of strong-signal predicate: single hit is always strong).
        (
            "Quintarius Ozymandias dossier",
            "Quintarius Ozymandias is the unique subject of this "
            "record. Quintarius Ozymandias appears throughout.",
        ),
        (
            "Unrelated weather report",
            "Cloudy with a chance of rain. Wind from the west at "
            "fifteen knots. No subjects of interest mentioned.",
        ),
        (
            "Unrelated cooking recipe",
            "Combine flour and water. Knead until smooth. Bake at "
            "moderate temperature for thirty minutes.",
        ),
    ),
    expected=(
        "only doc 1 matches; top1 strong, top2 absent → ratio gate "
        "passes (single-hit branch) → short-circuit fires"
    ),
)

SCENARIO_HIGH_TOP1_AMBIGUOUS = Scenario(
    name="high-top1-ambiguous",
    query="quintarius ozymandias",
    documents=(
        # Doc 1 AND Doc 2 both have TITLE matches → both score strong.
        # Ratio gate fails.
        (
            "Quintarius Ozymandias profile",
            "Quintarius Ozymandias is the subject. Quintarius "
            "Ozymandias features. Quintarius Ozymandias.",
        ),
        (
            "Ozymandias and Quintarius notes",
            "Quintarius Ozymandias is discussed. Quintarius Ozymandias "
            "is referenced. Quintarius Ozymandias.",
        ),
        # Doc 3: no match (so we have only 2 matches; top1+top2 both
        # strong with similar bm25).
        (
            "Unrelated cooking recipe",
            "Combine flour and water. Knead until smooth. Bake at "
            "moderate temperature for thirty minutes.",
        ),
    ),
    expected=(
        "top1 and top2 both strong (both have title matches); "
        "top1 < 2 * top2 → ratio gate fails, expansion runs"
    ),
)

SCENARIO_AMBIGUOUS_ALL_WEAK = Scenario(
    name="ambiguous-all-weak",
    query="quintarius ozymandias",
    documents=(
        # Three docs, NO title matches, similar single-mention content.
        # Bm25 should rank them close (similar lengths + densities).
        (
            "Brief mention one",
            "A passing reference to quintarius ozymandias was made "
            "in passing. " + ("Filler content here. " * 25),
        ),
        (
            "Brief mention two",
            "quintarius ozymandias is mentioned in passing once. "
            + ("More filler text. " * 25),
        ),
        (
            "Brief mention three",
            "quintarius ozymandias appears once in this paragraph. "
            + ("Additional filler. " * 25),
        ),
    ),
    expected=(
        "all three documents have comparable bm25 (no title boost, "
        "similar content density); ratio gate fails → expansion runs"
    ),
)

SCENARIO_LOW_CONFIDENCE = Scenario(
    name="low-confidence",
    query="quintarius ozymandias",
    documents=(
        # The corpus has NO matches for the query — every "matching"
        # doc has been removed. The probe returns []; expand.py
        # treats empty results as "no top1 → top1 gate fails →
        # expansion runs". This mirrors the spec's `[-0.3]`
        # low-confidence case (single weak result) more cleanly than
        # trying to engineer a sub-0.67 bm25 against a 50-doc IDF
        # corpus (which proved impractical with SQLite FTS5).
        (
            "Generic content one",
            "Some unrelated content about gardening, "
            + ("more filler text. " * 25),
        ),
        (
            "Generic content two",
            "Notes on small home repair projects, "
            + ("additional filler. " * 25),
        ),
    ),
    expected=(
        "no doc matches the query → seed_bm25_results is empty → "
        "top1 gate fails (empty input) → expansion runs"
    ),
)


SCENARIOS: dict[str, Scenario] = {
    s.name: s for s in (
        SCENARIO_STRONG,
        SCENARIO_HIGH_TOP1_AMBIGUOUS,
        SCENARIO_AMBIGUOUS_ALL_WEAK,
        SCENARIO_LOW_CONFIDENCE,
    )
}


def _open_blank_db(path: Path) -> sqlite3.Connection:
    """Open a fresh sqlite connection with PRAGMA defaults matching prod."""
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _apply_initial_migration(conn: sqlite3.Connection) -> None:
    """Apply 001_initial.sql via the production migration runner.

    Reuses `sage_memory.db._migrate` so trigger bodies (which contain
    `;`) are split correctly. Restricts the migrations dir to a temp
    copy holding ONLY 001_initial.sql — we don't need chunks /
    entities / extraction_queue for the strong-signal probe.
    """
    import shutil
    import tempfile
    from sage_memory.db import _migrate

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        shutil.copy2(
            _PROD_MIGRATIONS_DIR / "001_initial.sql",
            tmp_path / "001_initial.sql",
        )
        _migrate(conn, migrations_dir=tmp_path)


def build_scenario_db(
    scenario_name: str, db_path: Path
) -> sqlite3.Connection:
    """Build a fresh DB with the named scenario's corpus loaded.

    Returns an open sqlite3 connection. Caller is responsible for
    closing it (the pytest fixture in conftest handles this).
    """
    scenario = SCENARIOS[scenario_name]
    conn = _open_blank_db(db_path)
    _apply_initial_migration(conn)
    now = time.time()
    # Scenario docs first, then filler corpus. Order doesn't matter
    # for bm25 — IDF is corpus-global.
    all_docs = list(scenario.documents) + list(_FILLER_DOCS)
    for title, content in all_docs:
        mid = uuid.uuid4().hex
        # FTS sync triggers fire on INSERT to memories
        conn.execute(
            "INSERT INTO memories (id, title, content, tags, "
            "content_hash, created_at, updated_at, accessed_at) "
            "VALUES (?, ?, ?, '[]', ?, ?, ?, ?)",
            (mid, title, content, mid, now, now, now),
        )
    conn.commit()
    return conn


def bm25_probe(
    conn: sqlite3.Connection, query: str, limit: int = 3
) -> list[tuple[str, float]]:
    """Run the seed bm25 probe used by `expand.expand_query`.

    Returns up to `limit` (memory_id, raw_bm25_score) pairs ordered by
    relevance (lowest raw_bm25_score first — bm25() output is negative-
    magnitude). Uses the same FTS5 weights as production search
    (`bm25(memories_fts, 10.0, 3.0, 1.0)` per search.py:_fts_search).

    Query tokenization mirrors prod: lowercase + simple split. We do
    NOT apply the term-frequency filtering or stopword scrub from
    search.py:_build_fts_query — the probe operates on the raw user
    query to feed the strong-signal decision. expand.py is free to
    re-run its own filter if needed.
    """
    # Build a minimal FTS5 OR query: each word becomes a prefix term.
    words = [w.lower() for w in query.split() if w]
    if not words:
        return []
    fts_q = " OR ".join(f"{w}*" for w in words)
    try:
        rows = conn.execute(
            "SELECT m.id AS id, "
            "bm25(memories_fts, 10.0, 3.0, 1.0) AS bm25_score "
            "FROM memories m "
            "JOIN memories_fts fts ON m.rowid = fts.rowid "
            "WHERE memories_fts MATCH ? "
            "ORDER BY bm25_score LIMIT ?",
            (fts_q, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(r["id"], float(r["bm25_score"])) for r in rows]
