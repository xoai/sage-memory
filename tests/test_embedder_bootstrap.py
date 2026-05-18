"""M5 follow-up — embedder resolver bootstrap tests.

ADR-005's resolver was never wired into production startup before
this fix. Users with API keys (OPENAI_API_KEY etc.) silently got
LocalEmbedder. server.py:run now calls resolve(corpus_dim) and
set_embedder() before serving.

Tests cover:
  - 384d corpus + no hosted key → LocalEmbedder (or FastEmbedder if installed)
  - 384d corpus + OPENAI_API_KEY set → still 384d-compatible (OpenAI is 1536d, not picked)
  - 1536d corpus + OPENAI_API_KEY → OpenAIEmbedder
  - 1536d corpus + no key → DimMismatchRefuseError
  - Resolver runs as part of `server.run()` (via direct unit test —
    the full async stdio flow is harder to mock).
"""

from __future__ import annotations

import os

import pytest

from sage_memory.embedder import (
    DimMismatchRefuseError, LocalEmbedder, OpenAIEmbedder, resolve,
    set_embedder, get_embedder,
)


def _scrub(monkeypatch):
    for var in (
        "OPENAI_API_KEY", "VOYAGE_API_KEY",
        "COHERE_API_KEY", "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _reset_singleton():
    """Force get_embedder() to re-create on next call."""
    import sage_memory.embedder as emb_mod
    emb_mod._instance = None


def test_bootstrap_384d_no_keys_picks_local_or_fastembed(monkeypatch):
    """384d corpus + no API keys → resolver picks LocalEmbedder (or
    FastEmbedder if fastembed package is installed)."""
    _scrub(monkeypatch)
    e = resolve(384)
    # FastEmbedder (quality 0.85, 384d) wins if available, else Local.
    assert e.dim == 384
    assert type(e).__name__ in ("LocalEmbedder", "FastEmbedder")


def test_bootstrap_384d_with_openai_key_keeps_384d_embedder(monkeypatch):
    """384d corpus + OPENAI_API_KEY set → OpenAI is 1536d (mismatch);
    resolver falls back to FastEmbedder/LocalEmbedder. Log a hint
    about reindex."""
    _scrub(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    e = resolve(384)
    assert e.dim == 384
    assert type(e).__name__ in ("LocalEmbedder", "FastEmbedder")


def test_bootstrap_1536d_with_openai_key_picks_openai(monkeypatch):
    """1536d corpus + OPENAI_API_KEY set → OpenAIEmbedder activated."""
    _scrub(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    e = resolve(1536)
    assert isinstance(e, OpenAIEmbedder)
    assert e.dim == 1536


def test_bootstrap_1536d_no_keys_refuses(monkeypatch):
    """1536d corpus + no API key → DimMismatchRefuseError (spec-mandated;
    no silent down-projection)."""
    _scrub(monkeypatch)
    with pytest.raises(DimMismatchRefuseError) as exc:
        resolve(1536)
    msg = str(exc.value)
    assert "1536" in msg
    assert "reindex" in msg.lower() or "configure" in msg.lower()


def test_bootstrap_set_embedder_makes_get_embedder_return_it(
    monkeypatch,
):
    """set_embedder() installs the singleton; get_embedder() returns it."""
    _scrub(monkeypatch)
    _reset_singleton()
    # By default, get_embedder() lazily creates LocalEmbedder.
    e0 = get_embedder()
    assert isinstance(e0, LocalEmbedder)

    # Install a different embedder
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    new = OpenAIEmbedder(api_key="sk-test")
    set_embedder(new)
    assert get_embedder() is new

    # Cleanup
    _reset_singleton()


def test_bootstrap_server_run_wires_resolver(monkeypatch):
    """Smoke test that server.run() calls resolve() + set_embedder()
    before serving. We patch stdio_server to short-circuit the actual
    serve loop so we just exercise the bootstrap code path."""
    _scrub(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-bootstrap")
    _reset_singleton()

    import asyncio
    from contextlib import asynccontextmanager

    # Track what set_embedder is called with
    seen = []
    import sage_memory.server as srv_mod
    real_set = srv_mod.set_embedder

    def _spy_set(embedder):
        seen.append(type(embedder).__name__)
        real_set(embedder)

    monkeypatch.setattr(srv_mod, "set_embedder", _spy_set)

    # Short-circuit the stdio_server context manager so run() exits
    # immediately after bootstrap.
    @asynccontextmanager
    async def _fake_stdio():
        raise SystemExit("test stub — exit before serve loop")
        yield  # never reached

    monkeypatch.setattr(srv_mod, "stdio_server", _fake_stdio)

    # Also stub create_server (we don't need a real Server instance)
    class _StubServer:
        def create_initialization_options(self):
            return None
        async def run(self, *a, **kw):
            return None

    monkeypatch.setattr(srv_mod, "create_server", lambda: _StubServer())

    # Run and expect SystemExit from our stubbed stdio
    with pytest.raises(SystemExit):
        asyncio.run(srv_mod.run())

    # Bootstrap should have called set_embedder. With 384d corpus
    # (the default for a fresh global DB), OpenAI 1536d doesn't
    # match → falls back to FastEmbedder/LocalEmbedder.
    assert seen, "set_embedder should have been called during run()"
    assert seen[0] in ("LocalEmbedder", "FastEmbedder")

    _reset_singleton()
