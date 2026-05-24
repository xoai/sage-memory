"""M3.2 — store.py + graph.py hub-ownership write guard.

5 tests per plan.md M3.2 done-when:
  1. reads (search/list/graph) work against an owned project
  2. store/update/delete return read-only envelope with PID info
  3. link returns read-only envelope
  4. per-DB isolation — owned X doesn't block writes to unowned Y
  5. SAGE_HUB_IGNORE_OWNERSHIP=1 bypasses the check

Each test seeds a fresh ``.hub-owner.json`` for the active project so
the in-process store/update/delete handlers see it as owned by another
PID. Reset ownership module state per test.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from sage_memory.db import (
    _open, close_all, get_project_db_path, override_project_root,
)
from sage_memory.embedder import (
    EMBEDDING_DIM, LocalEmbedder, set_embedder,
)
from sage_memory.hub import ownership


class _TestEmbedder:
    name = "test-m32"
    version = "v1"
    dim = EMBEDDING_DIM
    quality = 0.9
    max_input_chars = 8192

    def embed(self, text):
        h = abs(hash(text)) % 100
        return [(h + i) / 100.0 for i in range(EMBEDDING_DIM)]


@pytest.fixture
def owned_project(tmp_path, monkeypatch):
    """Set up a project with a fresh .hub-owner.json claiming the
    DB is held by an external PID."""
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    set_embedder(_TestEmbedder())

    root = tmp_path / "myproject"
    root.mkdir()
    (root / ".git").mkdir()
    sm = root / ".sage-memory"
    sm.mkdir()

    close_all()
    override_project_root(root)
    _open(get_project_db_path(root))

    # Synthetic ownership file: a DIFFERENT pid (not ours) holds it.
    owner_file = sm / ".hub-owner.json"
    owner_file.write_text(json.dumps({
        "owner_pid": 99999,
        "owner_started": time.time(),
        "owner_heartbeat": time.time(),
    }))
    ownership.reset_module_state_for_tests()

    yield root

    close_all()
    set_embedder(LocalEmbedder())
    ownership.reset_module_state_for_tests()


@pytest.fixture
def unowned_project(tmp_path, monkeypatch):
    """Companion fixture: a second project root with NO ownership
    file. Used by the per-DB-isolation test."""
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    set_embedder(_TestEmbedder())
    root = tmp_path / "otherproject"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".sage-memory").mkdir()
    yield root
    close_all()
    set_embedder(LocalEmbedder())


# ─── 1. Reads work against owned project ──────────────────────────


def test_reads_still_work_against_owned_project(owned_project):
    """search / list / graph do NOT consult the ownership guard —
    only writes do. An owned project must still be queryable."""
    from sage_memory.search import search
    from sage_memory.store import list_memories
    from sage_memory.graph import graph

    # Reads must not error — they may return empty (no seeded data),
    # but they must NOT return a read-only envelope.
    search_res = search(query="anything", limit=5)
    assert "results" in search_res, f"search blocked unexpectedly: {search_res!r}"

    list_res = list_memories(limit=5)
    assert "memories" in list_res or "results" in list_res or "success" not in list_res, (
        f"list blocked unexpectedly: {list_res!r}"
    )


# ─── 2. store/update/delete return read-only envelope ─────────────


def test_store_update_delete_return_read_only_envelope(owned_project):
    from sage_memory.store import store, update, delete

    store_res = store(content="A new memory we wish to add", title="test")
    assert store_res.get("success") is False, (
        f"store should be blocked; got {store_res!r}"
    )
    assert "hub-managed" in store_res["message"], (
        f"message must explain why; got {store_res!r}"
    )
    assert "99999" in store_res["message"], (
        f"message must include the owner PID; got {store_res!r}"
    )

    update_res = update(id="any-id", content="updated content")
    assert update_res.get("success") is False
    assert "hub-managed" in update_res["message"]

    delete_res = delete(id="any-id")
    assert delete_res.get("success") is False
    assert delete_res.get("deleted") == 0
    assert "hub-managed" in delete_res["message"]


# ─── 3. link returns read-only envelope ───────────────────────────


def test_link_returns_read_only_envelope(owned_project):
    from sage_memory.graph import link

    res = link(source_id="a", target_id="b", relation="depends_on")
    assert res.get("success") is False, (
        f"link should be blocked; got {res!r}"
    )
    assert "hub-managed" in res["message"]


# ─── 4. Per-DB isolation — owned X doesn't affect unowned Y ──────


def test_per_db_state_isolation(tmp_path, monkeypatch):
    """ADR-009 rev 2 fix verified at the store.py layer: an owned
    project blocks writes for ITS DB only. A second project with no
    ownership file continues to accept writes."""
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    set_embedder(_TestEmbedder())

    project_x = tmp_path / "owned"
    project_y = tmp_path / "unowned"
    for p in (project_x, project_y):
        p.mkdir()
        (p / ".git").mkdir()
        (p / ".sage-memory").mkdir()

    # Mark X as owned by another process.
    (project_x / ".sage-memory" / ".hub-owner.json").write_text(json.dumps({
        "owner_pid": 99999,
        "owner_started": time.time(),
        "owner_heartbeat": time.time(),
    }))

    from sage_memory.store import store

    # Touch X
    close_all()
    ownership.reset_module_state_for_tests()
    override_project_root(project_x)
    _open(get_project_db_path(project_x))
    res_x = store(content="cannot write to X", title="X attempt")
    assert res_x.get("success") is False, (
        f"X should be blocked; got {res_x!r}"
    )

    # Touch Y (no ownership file)
    close_all()
    ownership.reset_module_state_for_tests()
    override_project_root(project_y)
    _open(get_project_db_path(project_y))
    res_y = store(content="Y should accept this write", title="Y attempt")
    assert res_y.get("success") is True, (
        f"Y should accept writes; got {res_y!r}"
    )

    close_all()
    set_embedder(LocalEmbedder())
    ownership.reset_module_state_for_tests()


# ─── 5. SAGE_HUB_IGNORE_OWNERSHIP=1 bypasses guard ────────────────


def test_disabled_cache_clears_when_owner_heartbeat_goes_stale(
    owned_project,
):
    """Regression for /review M3 MAJOR #1: ``is_disabled_for`` must
    re-validate freshness on each call. Pre-fix, a cached entry would
    report disabled even after the owner's heartbeat went stale —
    the "wait 60s for stale reclaim" escape hatch advertised in the
    error message didn't actually unblock the cached caller."""
    from sage_memory.store import store

    # First store call populates _disabled_writes cache.
    res1 = store(content="cached-disabled probe", title="probe")
    assert res1.get("success") is False, f"setup: {res1!r}"

    # Now rewrite the owner file with an OLD heartbeat (simulate the
    # owner crashing / sleeping past the stale window).
    owner_file = owned_project / ".sage-memory" / ".hub-owner.json"
    owner_file.write_text(json.dumps({
        "owner_pid": 99999,
        "owner_started": time.time() - 1000,
        "owner_heartbeat": time.time() - 1000,  # > STALE_AFTER_SECONDS
    }))

    # The next store call: is_disabled_for must re-validate, see the
    # cached entry's heartbeat is now stale, drop it, and let store
    # proceed.
    res2 = store(content="post-stale write should succeed now", title="post-stale")
    assert res2.get("success") is True, (
        f"is_disabled_for must drop stale cache entry; got {res2!r}"
    )


def test_env_var_bypass_allows_write_on_owned_project(
    owned_project, monkeypatch,
):
    monkeypatch.setenv("SAGE_HUB_IGNORE_OWNERSHIP", "1")
    ownership.reset_module_state_for_tests()

    from sage_memory.store import store
    res = store(content="bypass writes via the dev env var", title="bypass")
    assert res.get("success") is True, (
        f"env-var bypass should allow writes; got {res!r}"
    )
