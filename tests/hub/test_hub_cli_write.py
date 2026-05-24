"""M3.5 — `sage-memory hub` write subcommands (store / import / release).

6 tests per plan.md M3.5 done-when:
  1. store happy path
  2. store non-writable error
  3. import happy path
  4. import non-existent source error
  5. release happy path
  6. release on non-owned project → no-op
"""

from __future__ import annotations

import json
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


class _TestEmbedder:
    name = "test-m35"
    version = "v1"
    dim = EMBEDDING_DIM
    quality = 0.9
    max_input_chars = 8192

    def embed(self, text):
        h = abs(hash(text)) % 100
        return [(h + i) / 100.0 for i in range(EMBEDDING_DIM)]


@pytest.fixture
def hub_with_writable(tmp_path, monkeypatch):
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


def test_cli_store_happy(hub_with_writable, capsys):
    from sage_memory.cli_hub import run_hub

    hub_path, _ops, _backend = hub_with_writable
    rc = run_hub([
        "store",
        "--to", "ops",
        "--content", "Routed CLI store: deploy procedure for the auth service",
        "--title", "Deploy runbook",
        "--tags", "ops",
        "--config-path", str(hub_path),
    ])
    out = capsys.readouterr().out
    assert rc == 0, f"store failed: out={out!r}"
    assert "stored memory" in out


def test_cli_store_non_writable_errors(hub_with_writable, capsys):
    from sage_memory.cli_hub import run_hub

    hub_path, _, _ = hub_with_writable
    rc = run_hub([
        "store",
        "--to", "backend",
        "--content", "this should be rejected",
        "--config-path", str(hub_path),
    ])
    err = capsys.readouterr().err
    assert rc == 2
    assert "writable" in err.lower()


def test_cli_import_happy(hub_with_writable, tmp_path, capsys):
    """Build a small source DB, import it into ops, assert the CLI
    reports a successful copy count."""
    from sage_memory.cli_hub import run_hub

    hub_path, ops, _ = hub_with_writable

    # Build source via store path.
    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / ".git").mkdir()
    (src_root / ".sage-memory").mkdir()
    close_all()
    override_project_root(src_root)
    _open(get_project_db_path(src_root))
    from sage_memory.store import store as _store
    _store(content="CLI import sentinel content #1", title="src1")
    _store(content="CLI import sentinel content #2", title="src2")
    source_db = get_project_db_path(src_root)
    close_all()

    capsys.readouterr()  # discard any setup output

    rc = run_hub([
        "import",
        "--from", str(source_db),
        "--to", "ops",
        "--config-path", str(hub_path),
    ])
    out = capsys.readouterr().out
    assert rc == 0, f"import failed: out={out!r}"
    assert "imported" in out


def test_cli_import_missing_source_errors(hub_with_writable, capsys, tmp_path):
    from sage_memory.cli_hub import run_hub

    hub_path, _, _ = hub_with_writable
    rc = run_hub([
        "import",
        "--from", str(tmp_path / "no-such.db"),
        "--to", "ops",
        "--config-path", str(hub_path),
    ])
    err = capsys.readouterr().err
    assert rc == 2
    assert "source" in err.lower()


def test_cli_release_happy(hub_with_writable, capsys):
    """Acquire ownership in-process, then `hub release` should drop it."""
    from sage_memory.cli_hub import run_hub

    hub_path, ops, _ = hub_with_writable
    token = hub_ownership.acquire(ops)
    assert token is not None
    owner_file = ops / ".sage-memory" / ".hub-owner.json"
    assert owner_file.exists()

    rc = run_hub([
        "release", "ops",
        "--config-path", str(hub_path),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "released" in out
    assert not owner_file.exists(), "release should delete the owner file"


def test_cli_release_no_op_when_not_owned(hub_with_writable, capsys):
    """`hub release` on a project we don't own returns 0 with a
    "nothing to release" message — idempotent contract per ADR-009."""
    from sage_memory.cli_hub import run_hub

    hub_path, _, _ = hub_with_writable
    # Ensure registry is clean — we own nothing.
    hub_ownership.reset_module_state_for_tests()

    rc = run_hub([
        "release", "ops",
        "--config-path", str(hub_path),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not owned" in out.lower() or "nothing to release" in out.lower()
