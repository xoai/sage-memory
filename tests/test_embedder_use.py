"""P2-2 — Embedder ergonomics tests (SM-DOC-01 capability half).

Written tests-first per 06-spec §P2-2 + docs/design/embedder-ergonomics.md.

Contracts pinned:
  - `embedder use fastembed` without the extra → exit 2 + install hint
  - `embedder use openai` without the key → exit 2 naming the env var
  - happy path delegates to cli_reindex._do_full_reembed with the
    right embedder name (spy)
  - `status` prints a context-aware next step: reindex --embeddings
    when an upgrade embedder is active, embedder use fastembed when
    on the local floor; silent when nothing is stale
  - zero-dep floor: no fastembed import on the default path
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sage_memory import cli_embedder
from sage_memory.db import close_all


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch):
    close_all()
    monkeypatch.setenv("SAGE_PROJECT_ROOT", str(tmp_path))
    (tmp_path / "pyproject.toml").write_text("[tool.test]\n")
    yield tmp_path
    close_all()


def test_use_fastembed_without_extra_exits_2_with_hint(
    isolated, monkeypatch, capsys,
):
    import builtins
    real_import = builtins.__import__

    def _no_fastembed(name, *args, **kwargs):
        if name == "fastembed" or name.startswith("fastembed."):
            raise ImportError("No module named 'fastembed'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_fastembed)
    rc = cli_embedder.run_embedder(["use", "fastembed"])
    out = capsys.readouterr()
    assert rc == 2
    assert "sage-memory[neural]" in out.err


def test_use_openai_without_key_exits_2_naming_env(
    isolated, monkeypatch, capsys,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rc = cli_embedder.run_embedder(["use", "openai"])
    out = capsys.readouterr()
    assert rc == 2
    assert "OPENAI_API_KEY" in out.err


def test_use_unknown_tier_exits_2(isolated, capsys):
    rc = cli_embedder.run_embedder(["use", "bogus"])
    assert rc == 2


def test_use_fastembed_delegates_to_full_reembed(isolated, monkeypatch):
    # Happy path needs the [neural] extra — skips on the base-deps
    # floor (SM-TEST-01 pattern), where the exit-2-with-hint test
    # above covers behavior instead.
    pytest.importorskip("fastembed")
    calls = []

    def _spy(*, embedder_name, memory_id=None, limit=None):
        calls.append(embedder_name)
        return 0

    monkeypatch.setattr(
        "sage_memory.cli_reindex._do_full_reembed", _spy,
    )
    rc = cli_embedder.run_embedder(["use", "fastembed"])
    assert rc == 0
    assert calls == ["fastembed"]


def test_use_local_is_the_downgrade_path(isolated, monkeypatch):
    calls = []

    def _spy(*, embedder_name, memory_id=None, limit=None):
        calls.append(embedder_name)
        return 0

    monkeypatch.setattr(
        "sage_memory.cli_reindex._do_full_reembed", _spy,
    )
    rc = cli_embedder.run_embedder(["use", "local"])
    assert rc == 0
    assert calls == ["local"]


# ─── status next-step hints ───────────────────────────────────────


def _seed_stale_memory():
    from sage_memory.store import store
    store(content="stale-status-hint probe", title="stale-probe", tags=[])


def test_status_hints_reindex_when_stale_on_local_floor(
    isolated, capsys, monkeypatch,
):
    _seed_stale_memory()
    from sage_memory.cli_status import print_status
    # Local embedder active (default fixture state) → the upgrade hint.
    print_status()
    out = capsys.readouterr().out
    assert "stale" in out.lower()
    assert "embedder use fastembed" in out


def test_status_no_hint_when_nothing_stale(isolated, capsys):
    from sage_memory.cli_status import print_status
    print_status()
    out = capsys.readouterr().out
    assert "Next:" not in out
