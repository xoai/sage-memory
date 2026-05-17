"""M5 T3 — `sage-memory dedup` CLI + worker dedup task tests.

Spec ACs:
  A7 — Default worker-async enqueue + LLM-key gate + at-most-one
       concurrency contract.
  A8 — --sync in-process algorithm + nonzero exit on LLM failure.
  A9 — --provider stub independence from LLM key + argparse
       rejection of --provider stub without --sync.
  A10 — Worker dedup task type processes via run_pass; failure
       marks task failed; default `dedup.interval` is disabled.

Total: 10 tests.
"""

from __future__ import annotations

import logging
import time
import uuid

import httpx
import pytest

from sage_memory import cli_dedup, dedup as dedup_mod
from sage_memory.db import close_all, get_project_db, override_project_root
from sage_memory.worker import Worker


# ─── Fixture: project DB with seeded entities ────────────────────


@pytest.fixture
def entity_db(tmp_path):
    """Project DB with 4 entities: 2 same-type/cosine~0.95, plus 2
    different-type pairs that should never be considered."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".sage").mkdir()
    override_project_root(project_root)
    db = get_project_db()
    now = time.time()
    # Pair 1: PERSON / "Quintarius Ozymandias" vs "Quintarius Ozy."
    # Pair 2: TECHNOLOGY / "Python" vs "python"
    # Plus 2 OTHER entities (no pairs of same type with mention_count≥2)
    ents = [
        ("e1", "Quintarius Ozymandias", "quintarius ozymandias", "PERSON", 3),
        ("e2", "Quintarius Ozy.", "quintarius ozy.", "PERSON", 2),
        ("e3", "Python", "python", "TECHNOLOGY", 5),
        ("e4", "python", "python", "TECHNOLOGY", 2),
    ]
    for eid, name, norm, etype, mc in ents:
        # Use unique normalized to avoid UNIQUE collisions
        db.execute(
            "INSERT INTO entities (id, name, name_normalized, type, "
            "mention_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (eid, name, f"{norm}_{eid}", etype, mc, now, now),
        )
    db.commit()
    yield db
    close_all()
    override_project_root(None)


def _stub_embed_high_sim(self, text):
    """Deterministic embedding stub: identical-type pairs (paired ids)
    return near-identical vectors → cosine ≈ 1.0."""
    # All "quintarius*" entities return same vector
    if "quintarius" in text.lower():
        return [1.0, 0.0, 0.0]
    if "python" in text.lower():
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


# ─── A7: default enqueue + LLM-key gate + at-most-one ─────────────


def test_dedup_default_enqueues_worker_task(entity_db, monkeypatch):
    """A7: with LLM key, enqueues 1 dedup task."""
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )
    rc = cli_dedup.run_dedup([])
    assert rc == 0

    rows = entity_db.execute(
        "SELECT id, task_type, status, memory_id FROM extraction_queue "
        "WHERE task_type = 'dedup'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["memory_id"] is None


def test_dedup_default_refuses_without_llm_key(
    entity_db, monkeypatch, capsys,
):
    """A7: no LLM key → exit code 1 + stderr names env vars."""
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: False
    )
    rc = cli_dedup.run_dedup([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err
    assert "OPENAI_API_KEY" in err


def test_dedup_default_at_most_one_pending(entity_db, monkeypatch):
    """A10 concurrency: second CLI invocation returns existing task
    id, does NOT insert a new row."""
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )
    rc = cli_dedup.run_dedup([])
    assert rc == 0
    rc = cli_dedup.run_dedup([])
    assert rc == 0  # still success, but no insert

    n = entity_db.execute(
        "SELECT COUNT(*) AS n FROM extraction_queue "
        "WHERE task_type = 'dedup'"
    ).fetchone()["n"]
    assert n == 1


# ─── A8: --sync ───────────────────────────────────────────────────


def test_dedup_sync_runs_in_process(entity_db, monkeypatch, capsys):
    """A8: --sync calls run_pass; mocked LLM yes/yes → 2 UPDATE
    entities calls observed via canonical_id population."""
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )
    monkeypatch.setattr(
        "sage_memory.embedder.LocalEmbedder.embed", _stub_embed_high_sim,
    )
    monkeypatch.setattr(
        dedup_mod, "_llm_confirm_same", lambda a, b: True,
    )
    rc = cli_dedup.run_dedup(["--sync"])
    assert rc == 0

    merged = entity_db.execute(
        "SELECT COUNT(*) AS n FROM entities WHERE canonical_id IS NOT NULL"
    ).fetchone()["n"]
    assert merged == 2, (
        f"expected 2 entities merged (1 per pair); got {merged}"
    )
    out = capsys.readouterr().out
    assert "merged" in out.lower()


def test_dedup_sync_nonzero_exit_on_llm_failure(
    entity_db, monkeypatch,
):
    """A8: LLM raises → exit nonzero, no entity merges happen."""
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )
    monkeypatch.setattr(
        "sage_memory.embedder.LocalEmbedder.embed", _stub_embed_high_sim,
    )

    def _raise(a, b):
        raise httpx.TimeoutException("simulated")

    monkeypatch.setattr(dedup_mod, "_llm_confirm_same", _raise)
    rc = cli_dedup.run_dedup(["--sync"])
    assert rc == 1

    # No merges committed (transaction rolled back).
    merged = entity_db.execute(
        "SELECT COUNT(*) AS n FROM entities WHERE canonical_id IS NOT NULL"
    ).fetchone()["n"]
    assert merged == 0


# ─── A9: --provider stub ──────────────────────────────────────────


def test_dedup_provider_stub_no_llm_call(
    entity_db, monkeypatch, capsys,
):
    """A9: --provider stub never calls LLM. Independent of key state."""
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: False
    )
    monkeypatch.setattr(
        "sage_memory.embedder.LocalEmbedder.embed", _stub_embed_high_sim,
    )

    def _explode(a, b):
        raise AssertionError("LLM must not be called in stub mode")

    monkeypatch.setattr(dedup_mod, "_llm_confirm_same", _explode)
    rc = cli_dedup.run_dedup(["--sync", "--provider", "stub"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "2 candidate pair" in out or "2 candidate" in out
    # Est cost = 2 * $0.0002 = $0.0004
    assert "$0.0004" in out


def test_dedup_provider_stub_without_sync_argparse_rejects(
    entity_db, monkeypatch, capsys,
):
    """A9: argparse rejects `--provider stub` without `--sync` to
    prevent silently-no-op enqueued tasks."""
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )
    rc = cli_dedup.run_dedup(["--provider", "stub"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--sync" in err


# ─── A10: worker dedup task ───────────────────────────────────────


def test_worker_dedup_task_processes_via_run_pass(
    entity_db, monkeypatch, tmp_path,
):
    """A10: worker pops a dedup task → calls run_pass → marks done."""
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )
    monkeypatch.setattr(
        "sage_memory.embedder.LocalEmbedder.embed", _stub_embed_high_sim,
    )
    monkeypatch.setattr(
        dedup_mod, "_llm_confirm_same", lambda a, b: True,
    )

    # Enqueue a dedup task
    tid = uuid.uuid4().hex
    entity_db.execute(
        "INSERT INTO extraction_queue "
        "(id, memory_id, task_type, status, attempts, created_at) "
        "VALUES (?, NULL, 'dedup', 'pending', 0, unixepoch())",
        (tid,),
    )
    entity_db.commit()

    # Worker drain.
    db_path = next(tmp_path.glob("**/memory.db"))
    worker = Worker(str(db_path))
    processed = worker.drain_once(max_iterations=2, timeout_s=10.0)
    assert processed >= 1

    row = entity_db.execute(
        "SELECT status FROM extraction_queue WHERE id = ?", (tid,),
    ).fetchone()
    assert row["status"] == "done"


def test_worker_dedup_task_marks_failed_on_llm_failure(
    entity_db, monkeypatch, tmp_path,
):
    """A10: LLM raises inside dedup task → task marked failed; worker
    loop does NOT crash."""
    monkeypatch.setattr(
        "sage_memory.llm.is_configured", lambda: True
    )
    monkeypatch.setattr(
        "sage_memory.embedder.LocalEmbedder.embed", _stub_embed_high_sim,
    )

    def _raise(a, b):
        raise httpx.TimeoutException("simulated")

    monkeypatch.setattr(dedup_mod, "_llm_confirm_same", _raise)

    tid = uuid.uuid4().hex
    entity_db.execute(
        "INSERT INTO extraction_queue "
        "(id, memory_id, task_type, status, attempts, created_at) "
        "VALUES (?, NULL, 'dedup', 'pending', 0, unixepoch())",
        (tid,),
    )
    entity_db.commit()

    db_path = next(tmp_path.glob("**/memory.db"))
    worker = Worker(str(db_path))
    worker.drain_once(max_iterations=2, timeout_s=10.0)

    row = entity_db.execute(
        "SELECT status, last_error FROM extraction_queue WHERE id = ?",
        (tid,),
    ).fetchone()
    assert row["status"] == "failed"
    assert row["last_error"] is not None


def test_dedup_interval_disabled_default(entity_db):
    """A10: default `dedup.interval` is null/disabled. Worker's poll
    loop does NOT auto-enqueue without config."""
    from sage_memory import config
    val = config.get("dedup.interval")
    assert val is None
