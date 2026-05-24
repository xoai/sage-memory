"""M3.3 — hub.store.store_to_project routed write.

4 tests per plan.md M3.3 done-when:
  1. writable project → success (envelope shape matches store.store)
  2. non-writable project → clear error
  3. non-existent project → error
  4. routed write actually lands in the target project's DB
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sage_memory.db import (
    _open, close_all, get_project_db_path, override_project_root,
)
from sage_memory.embedder import (
    EMBEDDING_DIM, LocalEmbedder, set_embedder,
)
from sage_memory.hub import config as hub_config
from sage_memory.hub import ownership as hub_ownership
from sage_memory.hub.store import store_to_project


class _TestEmbedder:
    name = "test-m33"
    version = "v1"
    dim = EMBEDDING_DIM
    quality = 0.9
    max_input_chars = 8192

    def embed(self, text):
        h = abs(hash(text)) % 100
        return [(h + i) / 100.0 for i in range(EMBEDDING_DIM)]


@pytest.fixture
def hub_with_writable_and_readonly(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    set_embedder(_TestEmbedder())
    hub_ownership.reset_module_state_for_tests()

    ops = tmp_path / "ops"
    ops.mkdir()
    (ops / ".git").mkdir()
    (ops / ".sage-memory").mkdir()
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / ".git").mkdir()
    (backend / ".sage-memory").mkdir()

    hub_path = tmp_path / ".sage-hub.yaml"
    cfg = hub_config.init(hub_path)
    cfg = hub_config.add_project(cfg, "ops", ops, writable=True)
    cfg = hub_config.add_project(cfg, "backend", backend, writable=False)
    hub_config.save(cfg, hub_path)

    yield hub_path, ops, backend

    close_all()
    set_embedder(LocalEmbedder())
    hub_ownership.reset_module_state_for_tests()


def test_routed_write_to_writable_project_succeeds(
    hub_with_writable_and_readonly,
):
    hub_path, ops, _backend = hub_with_writable_and_readonly
    res = store_to_project(
        "ops",
        content="Runbook: how to restart the auth service safely",
        title="Auth restart runbook",
        tags=["ops", "runbook"],
        hub_path=hub_path,
    )
    assert res.get("success") is True, f"routed write should succeed: {res!r}"
    assert isinstance(res.get("id"), str) and res["id"], (
        f"envelope must include id: {res!r}"
    )


def test_routed_write_to_non_writable_project_rejected(
    hub_with_writable_and_readonly,
):
    hub_path, _ops, _backend = hub_with_writable_and_readonly
    res = store_to_project(
        "backend",
        content="Trying to write to a non-writable project",
        title="should fail",
        hub_path=hub_path,
    )
    assert res.get("success") is False
    assert "writable" in res["message"].lower()


def test_routed_write_to_unknown_project_rejected(
    hub_with_writable_and_readonly,
):
    hub_path, _ops, _backend = hub_with_writable_and_readonly
    res = store_to_project(
        "no-such-project",
        content="anything",
        title="anything",
        hub_path=hub_path,
    )
    assert res.get("success") is False
    assert "no-such-project" in res["message"]


def test_routed_write_lands_in_target_project_db(
    hub_with_writable_and_readonly,
):
    """Open the target project's DB directly (read-only URI) and
    confirm the new memory id is there. Catches the "routed call
    lands in the caller's active project" regression."""
    hub_path, ops, _backend = hub_with_writable_and_readonly

    sentinel = "M3.3 routed-write sentinel"
    res = store_to_project(
        "ops",
        content=sentinel,
        title="routed sentinel",
        hub_path=hub_path,
    )
    assert res.get("success") is True, f"{res!r}"
    memory_id = res["id"]

    # Open ops DB read-only and verify the row landed.
    db_path = get_project_db_path(ops)
    assert db_path.exists()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT id, content FROM memories WHERE id = ?", (memory_id,),
        ).fetchone()
        assert row is not None, (
            f"new memory not found in ops DB; expected id={memory_id}"
        )
        assert sentinel in row[1]
    finally:
        conn.close()
