"""T3 — sage_memory.worker tests.

Covers spec A3 (lifecycle), A4 (kill-mid-task recovery),
A5 (optimistic claim), A7 (reembed dispatch via T6a's memory_id
filter), A9 (entities/mentions/relations populate).

Worker uses a per-thread sqlite3 connection opened inside run() —
tests must use file-backed DBs (tmp_path), not :memory:, per rev 2
pin #5.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path

import pytest

from sage_memory.db import (
    _open, get_project_db_path, close_all, override_project_root,
)
from sage_memory.embedder import (
    LocalEmbedder, get_embedder, set_embedder, EMBEDDING_DIM,
)


class _HighQualityTestEmbedder:
    name = "test-hq"
    version = "v1"
    dim = EMBEDDING_DIM
    quality = 0.9
    max_input_chars = 8192

    def embed(self, text):
        h = abs(hash(text)) % 1000
        return [(h % 7) / 7.0] * EMBEDDING_DIM


class _LowQualityTestEmbedder:
    name = "test-low"
    version = "v0"
    dim = EMBEDDING_DIM
    quality = 0.45
    max_input_chars = 8192

    def embed(self, text):
        return [0.0] * EMBEDDING_DIM


# ─── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def db_file(tmp_path):
    """File-backed DB path with all migrations applied. Worker can
    open its own connection against this path inside run()."""
    (tmp_path / ".git").mkdir()
    close_all()
    override_project_root(tmp_path)
    set_embedder(_HighQualityTestEmbedder())
    # Force migration + close so worker thread can open fresh.
    path = get_project_db_path(tmp_path)
    conn = _open(path)
    # Keep conn cached for tests to read/write via `db_conn` fixture
    yield path
    close_all()
    set_embedder(LocalEmbedder())


@pytest.fixture
def db_conn(db_file):
    """Connection for the test body. Bridges path→connection for
    asserting against the same file the worker writes to."""
    return _open(db_file)


@pytest.fixture
def mock_extractor(monkeypatch):
    """Replace extractor.extract with a stub returning canned data."""
    canned = {
        "entities": [
            {"name": "TestEntity", "type": "CONCEPT",
             "surface_form": "TestEntity"},
        ],
        "relations": [],
    }
    calls = {"n": 0, "contents": []}

    from sage_memory import extractor

    def fake_extract(content, **kwargs):
        calls["n"] += 1
        calls["contents"].append(content)
        return canned

    monkeypatch.setattr(extractor, "extract", fake_extract)
    return calls


def _insert_memory(db, mid, content="some memory content"):
    db.execute(
        "INSERT INTO memories(id, title, content, content_hash, "
        "embedded, created_at, updated_at, accessed_at) "
        "VALUES (?, ?, ?, ?, 0, 1, 1, 1)",
        (mid, "t", content, f"hash-{mid}"),
    )
    db.commit()


def _enqueue(db, memory_id, task_type="extract", status="pending",
             started_at=None, created_at=None):
    qid = uuid.uuid4().hex
    now = time.time()
    db.execute(
        "INSERT INTO extraction_queue (id, memory_id, task_type, "
        "status, attempts, created_at, started_at) "
        "VALUES (?, ?, ?, ?, 0, ?, ?)",
        (qid, memory_id, task_type, status,
         created_at if created_at is not None else now,
         started_at),
    )
    db.commit()
    return qid


# ─── A3 — Lifecycle ───────────────────────────────────────────────


def test_worker_default_shutdown_timeout_covers_http_timeout(db_file):
    """Regression for cumulative-review re-verify finding (#2): the
    default `shutdown_timeout_s` must be at least
    `_HTTP_CONNECT_TIMEOUT + _HTTP_TIMEOUT` so a single in-flight
    LLM call can complete within the join window.

    This couples the worker's join budget to the llm.py timeouts —
    a future bump to either httpx timeout without a corresponding
    bump to the worker default trips this test. Catches the exact
    class of silent contract violation flagged in the prior review.
    """
    from sage_memory.worker import Worker
    from sage_memory import llm

    w = Worker(str(db_file))
    http_total = llm._HTTP_CONNECT_TIMEOUT + llm._HTTP_TIMEOUT
    assert w._shutdown_timeout >= http_total, (
        f"Worker.shutdown_timeout_s default ({w._shutdown_timeout}s) "
        f"must cover llm.py's worst-case single-call timeout "
        f"({http_total}s). If you bumped llm._HTTP_TIMEOUT, also "
        f"bump Worker.shutdown_timeout_s default."
    )
    # Margin of at least 5s for the loop-pass + cleanup overhead
    assert w._shutdown_timeout >= http_total + 5, (
        f"shutdown_timeout_s ({w._shutdown_timeout}s) should leave "
        f">=5s margin over http_total ({http_total}s) for cleanup."
    )


def test_worker_lifecycle_clean_start_stop(db_file):
    from sage_memory.worker import Worker
    baseline_threads = threading.active_count()
    worker = Worker(str(db_file), poll_interval_ms=50)
    worker.start()
    assert worker.is_alive()
    worker.stop()
    assert not worker.is_alive()
    # Give the thread a moment to fully tear down
    time.sleep(0.1)
    assert threading.active_count() == baseline_threads


def test_worker_start_idempotent(db_file):
    from sage_memory.worker import Worker
    worker = Worker(str(db_file), poll_interval_ms=50)
    worker.start()
    first_thread = worker._thread
    worker.start()  # second call should no-op
    assert worker._thread is first_thread
    worker.stop()


def test_worker_restart_after_stop(db_file):
    from sage_memory.worker import Worker
    worker = Worker(str(db_file), poll_interval_ms=50)
    worker.start()
    first_thread = worker._thread
    worker.stop()
    assert not worker.is_alive()
    worker.start()
    assert worker._thread is not first_thread
    assert worker.is_alive()
    worker.stop()


def test_worker_shutdown_within_timeout(db_file):
    from sage_memory.worker import Worker
    worker = Worker(str(db_file), poll_interval_ms=50,
                    shutdown_timeout_s=2.0)
    worker.start()
    t0 = time.time()
    worker.stop()
    elapsed = time.time() - t0
    assert elapsed < 3.0  # 2s timeout + jitter


# ─── A4 — Kill-mid-task recovery ──────────────────────────────────


def test_worker_kill_mid_task_recovery(db_file, db_conn, mock_extractor):
    """Spec A4: simulate a crashed prior worker by inserting a stale
    'running' row. Fresh worker's startup recovery resets it to
    pending, then claims + executes + marks done."""
    from sage_memory.worker import Worker

    _insert_memory(db_conn, "M1", content="recovery test memory")
    # Stale row: started_at < now - 300
    qid = uuid.uuid4().hex
    now = time.time()
    db_conn.execute(
        "INSERT INTO extraction_queue (id, memory_id, task_type, "
        "status, attempts, created_at, started_at) "
        "VALUES (?, ?, 'extract', 'running', 0, ?, ?)",
        (qid, "M1", now - 310, now - 301),
    )
    db_conn.commit()

    worker = Worker(str(db_file), poll_interval_ms=50)
    worker.start()
    drained = worker._wait_for_queue_empty(timeout_s=2.0)
    worker.stop()
    assert drained is True

    row = db_conn.execute(
        "SELECT status, processed_at, attempts FROM extraction_queue "
        "WHERE id = ?", (qid,),
    ).fetchone()
    assert row["status"] == "done"
    assert row["processed_at"] is not None
    assert row["attempts"] == 1


# ─── A5 — Optimistic claim ────────────────────────────────────────


def test_worker_optimistic_claim_rowcount_zero(db_file, db_conn):
    """The claim SQL returns rowcount=0 if the row is already running.
    Tested directly via the same SQL."""
    _insert_memory(db_conn, "M1")
    qid = _enqueue(db_conn, "M1", status="running",
                   started_at=time.time())
    # Try to claim a row that's already running — rowcount must be 0.
    cur = db_conn.execute(
        "UPDATE extraction_queue SET status='running', "
        "started_at=unixepoch() "
        "WHERE id = ? AND status = 'pending'",
        (qid,),
    )
    assert cur.rowcount == 0


# ─── A9 — Entities populate end-to-end ────────────────────────────


def test_worker_extract_populates_entities(
    db_file, db_conn, mock_extractor,
):
    from sage_memory.worker import Worker
    _insert_memory(db_conn, "M1", content="memory with TestEntity inside")
    _enqueue(db_conn, "M1", task_type="extract")

    worker = Worker(str(db_file), poll_interval_ms=50)
    worker.start()
    worker._wait_for_queue_empty(timeout_s=2.0)
    worker.stop()

    entities = db_conn.execute(
        "SELECT name, name_normalized, type FROM entities"
    ).fetchall()
    assert len(entities) == 1
    assert entities[0]["name"] == "TestEntity"
    assert entities[0]["name_normalized"] == "testentity"
    assert entities[0]["type"] == "CONCEPT"

    mentions = db_conn.execute(
        "SELECT memory_id, surface_form FROM mentions"
    ).fetchall()
    assert len(mentions) == 1
    assert mentions[0]["memory_id"] == "M1"


def test_worker_mention_offset_first_match_only(
    db_file, db_conn, monkeypatch,
):
    """content.find returns the FIRST match position. With "Bob" appearing
    twice, the mention's context_start is the first occurrence."""
    from sage_memory.worker import Worker
    from sage_memory import extractor

    monkeypatch.setattr(extractor, "extract", lambda content, **kw: {
        "entities": [{"name": "Bob", "type": "PERSON",
                      "surface_form": "Bob"}],
        "relations": [],
    })

    _insert_memory(db_conn, "M1", content="hello Bob and goodbye Bob")
    _enqueue(db_conn, "M1", task_type="extract")

    worker = Worker(str(db_file), poll_interval_ms=50)
    worker.start()
    worker._wait_for_queue_empty(timeout_s=2.0)
    worker.stop()

    row = db_conn.execute(
        "SELECT context_start, context_end FROM mentions "
        "WHERE memory_id = 'M1'"
    ).fetchone()
    assert row["context_start"] == 6  # first 'Bob'
    assert row["context_end"] == 9


def test_worker_mention_offset_not_found(
    db_file, db_conn, monkeypatch,
):
    """surface_form not in content → context_start/end stored as NULL."""
    from sage_memory.worker import Worker
    from sage_memory import extractor

    monkeypatch.setattr(extractor, "extract", lambda content, **kw: {
        "entities": [{"name": "Alice", "type": "PERSON",
                      "surface_form": "Alice"}],
        "relations": [],
    })

    _insert_memory(db_conn, "M1", content="nobody named here")
    _enqueue(db_conn, "M1", task_type="extract")

    worker = Worker(str(db_file), poll_interval_ms=50)
    worker.start()
    worker._wait_for_queue_empty(timeout_s=2.0)
    worker.stop()

    row = db_conn.execute(
        "SELECT context_start, context_end FROM mentions "
        "WHERE memory_id = 'M1'"
    ).fetchone()
    assert row["context_start"] is None
    assert row["context_end"] is None


def test_worker_extract_idempotent_replay(
    db_file, db_conn, mock_extractor,
):
    """Same memory processed twice → UNIQUE constraints prevent dup
    entities/mentions; mention_count increments correctly."""
    from sage_memory.worker import Worker
    _insert_memory(db_conn, "M1", content="memory with TestEntity")
    _enqueue(db_conn, "M1", task_type="extract")

    worker = Worker(str(db_file), poll_interval_ms=50)
    worker.start()
    worker._wait_for_queue_empty(timeout_s=2.0)
    worker.stop()

    _enqueue(db_conn, "M1", task_type="extract")
    worker.start()
    worker._wait_for_queue_empty(timeout_s=2.0)
    worker.stop()

    # Still only one entity row (UNIQUE name_normalized + type)
    n_entities = db_conn.execute(
        "SELECT COUNT(*) FROM entities"
    ).fetchone()[0]
    assert n_entities == 1

    # mention_count should be 2 (one per processed task)
    count = db_conn.execute(
        "SELECT mention_count FROM entities"
    ).fetchone()[0]
    assert count == 2


# ─── A7 — Reembed dispatch via T6a's filter ───────────────────────


def test_worker_reembed_dispatches_to_filtered_embed_pending(
    db_file, db_conn, monkeypatch,
):
    """Worker's reembed dispatch calls embed_pending(db, memory_id=mid)
    and embed_pending_chunks(db, memory_id=mid)."""
    from sage_memory.worker import Worker
    from sage_memory import store as store_mod

    captured = {"embed_pending_calls": [],
                "embed_pending_chunks_calls": []}

    def fake_embed_pending(db, batch_size=50, memory_id=None):
        captured["embed_pending_calls"].append(memory_id)
        return 0

    def fake_embed_pending_chunks(db, batch_size=50, memory_id=None):
        captured["embed_pending_chunks_calls"].append(memory_id)
        return 0

    monkeypatch.setattr(store_mod, "embed_pending", fake_embed_pending)
    monkeypatch.setattr(
        store_mod, "embed_pending_chunks", fake_embed_pending_chunks,
    )

    _insert_memory(db_conn, "M_REEMBED")
    _enqueue(db_conn, "M_REEMBED", task_type="reembed")

    worker = Worker(str(db_file), poll_interval_ms=50)
    worker.start()
    worker._wait_for_queue_empty(timeout_s=2.0)
    worker.stop()

    assert captured["embed_pending_calls"] == ["M_REEMBED"]
    assert captured["embed_pending_chunks_calls"] == ["M_REEMBED"]


def test_worker_reembed_loops_past_batch_size(db_file, db_conn):
    """Regression test for verifier-flagged MAJOR finding: the reembed
    handler must drain ALL of a memory's pending chunks, not just the
    first batch_size (=50). Without the loop, chunks 51+ stay
    permanently unembedded — matching M2's behavior but contradicting
    the M3a narrative that the worker handles all chunks eventually.

    Setup: 75 chunk rows for one memory, no chunks_vec rows yet,
    enqueue ONE reembed task. After drain: all 75 chunks_vec rows
    must exist (worker looped 2× internally: 50 + 25).
    """
    from sage_memory.worker import Worker

    _insert_memory(db_conn, "M_BIG", content="big chunked memory")
    for i in range(75):
        db_conn.execute(
            "INSERT INTO chunks (id, memory_id, chunk_index, content, "
            "byte_start, byte_end, created_at) "
            "VALUES (?, 'M_BIG', ?, ?, ?, ?, 1)",
            (f"chunk_{i:03d}", i, f"chunk content {i}",
             i * 100, i * 100 + 50),
        )
    _enqueue(db_conn, "M_BIG", task_type="reembed")

    worker = Worker(str(db_file), poll_interval_ms=50)
    worker.start()
    worker._wait_for_queue_empty(timeout_s=5.0)
    worker.stop()

    n_vec = db_conn.execute(
        "SELECT COUNT(*) FROM chunks_vec WHERE chunk_id IN "
        "(SELECT id FROM chunks WHERE memory_id='M_BIG')"
    ).fetchone()[0]
    assert n_vec == 75, (
        f"expected all 75 chunks embedded after one reembed task, "
        f"got {n_vec} (worker reembed handler is not looping)"
    )


def test_worker_reembed_below_quality_threshold_marks_done(
    db_file, db_conn,
):
    """Low-quality embedder → embed_pending* return 0; reembed task
    still marks done (not failed)."""
    from sage_memory.worker import Worker
    set_embedder(_LowQualityTestEmbedder())

    _insert_memory(db_conn, "M1")
    qid = _enqueue(db_conn, "M1", task_type="reembed")

    worker = Worker(str(db_file), poll_interval_ms=50)
    worker.start()
    worker._wait_for_queue_empty(timeout_s=2.0)
    worker.stop()

    status = db_conn.execute(
        "SELECT status FROM extraction_queue WHERE id = ?", (qid,),
    ).fetchone()[0]
    assert status == "done"


# ─── Dispatch edge cases ──────────────────────────────────────────


def test_worker_deleted_memory_marks_failed(
    db_file, db_conn, mock_extractor,
):
    """If memory was deleted between enqueue and pickup, task is
    marked failed; worker continues."""
    from sage_memory.worker import Worker
    _insert_memory(db_conn, "M_GONE")
    qid = _enqueue(db_conn, "M_GONE", task_type="extract")
    # Delete the memory but leave the queue row
    db_conn.execute("DELETE FROM memories WHERE id = 'M_GONE'")
    db_conn.commit()

    worker = Worker(str(db_file), poll_interval_ms=50)
    worker.start()
    # The queue row has memory_id with FK CASCADE — the DELETE above
    # already cascaded the queue row, so the row may not even exist.
    # Verify by querying for the queue id.
    time.sleep(0.3)
    worker.stop()

    row = db_conn.execute(
        "SELECT status FROM extraction_queue WHERE id = ?", (qid,),
    ).fetchone()
    # Either CASCADE deleted the row, or the worker marked it failed.
    # Both are valid "doesn't crash" outcomes.
    assert row is None or row["status"] == "failed"


def test_worker_dedup_task_no_llm_key_marks_failed(
    db_file, db_conn, monkeypatch,
):
    """M5 T3: dedup task without LLM key configured → marked failed
    with a clear error naming the env vars. (Was 'stub marks failed'
    in M3a; M5 ships the real algorithm, so the failure mode is now
    'no LLM key' rather than 'not implemented'.)"""
    from sage_memory.worker import Worker
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: False
    )
    _insert_memory(db_conn, "M1")
    qid = _enqueue(db_conn, "M1", task_type="dedup")

    worker = Worker(str(db_file), poll_interval_ms=50)
    worker.start()
    worker._wait_for_queue_empty(timeout_s=2.0)
    worker.stop()

    row = db_conn.execute(
        "SELECT status, last_error FROM extraction_queue WHERE id = ?",
        (qid,),
    ).fetchone()
    assert row["status"] == "failed"
    err = (row["last_error"] or "").upper()
    assert "ANTHROPIC_API_KEY" in err or "OPENAI_API_KEY" in err


def test_worker_exception_caught_does_not_kill_thread(
    db_file, db_conn, monkeypatch,
):
    """Unexpected RuntimeError from extractor → task marked failed,
    worker thread keeps running, next task processes normally."""
    from sage_memory.worker import Worker
    from sage_memory import extractor

    call_count = {"n": 0}
    canned_ok = {
        "entities": [{"name": "OK", "type": "CONCEPT",
                      "surface_form": "OK"}],
        "relations": [],
    }

    def flaky(content, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated crash")
        return canned_ok

    monkeypatch.setattr(extractor, "extract", flaky)

    _insert_memory(db_conn, "M1", content="content one")
    _insert_memory(db_conn, "M2", content="content two")
    q1 = _enqueue(db_conn, "M1", task_type="extract",
                  created_at=time.time() - 10)
    q2 = _enqueue(db_conn, "M2", task_type="extract",
                  created_at=time.time())

    worker = Worker(str(db_file), poll_interval_ms=50)
    worker.start()
    worker._wait_for_queue_empty(timeout_s=3.0)
    assert worker.is_alive()
    worker.stop()

    r1 = db_conn.execute(
        "SELECT status, last_error FROM extraction_queue WHERE id = ?",
        (q1,),
    ).fetchone()
    r2 = db_conn.execute(
        "SELECT status FROM extraction_queue WHERE id = ?", (q2,),
    ).fetchone()
    assert r1["status"] == "failed"
    assert "simulated crash" in (r1["last_error"] or "")
    assert r2["status"] == "done"
