"""Embedder cascade tests — T7-T10 of M1.

Covers:
- T7: HostedEmbedder ABC + 3 subclasses + extended Protocol
- T8: mean-pool adapter for inputs > max_input_chars
- T9: 6-scenario resolver per ADR-005 §Resolver rule
- T10: dim-strategy refuse-write
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch, MagicMock

import pytest

from sage_memory.embedder import (
    LocalEmbedder,
    FastEmbedder,
    HostedEmbedder,
    OpenAIEmbedder,
    VoyageEmbedder,
    CohereEmbedder,
    resolve,
    EMBEDDING_DIM,
)


# ═══════════════════════════════════════════════════════════════════
# T7 — Protocol surface
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "cls,expected_name,expected_dim,min_quality",
    [
        (LocalEmbedder, "local", 384, 0.4),
        (OpenAIEmbedder, "openai", 1536, 0.85),
        (VoyageEmbedder, "voyage", 512, 0.85),
        (CohereEmbedder, "cohere", 1024, 0.85),
    ],
)
def test_protocol_surface_lightweight(cls, expected_name, expected_dim, min_quality):
    """Each non-FastEmbedder class exposes name/version/dim/quality/max_input_chars
    as class-level properties (no network/model required to instantiate)."""
    if cls is LocalEmbedder:
        e = cls()
    else:
        # Hosted embedders — construct without API key (constructor must not
        # eagerly call the API; instantiation is lazy).
        e = cls(api_key="test-key-for-protocol-check")

    assert e.name == expected_name
    assert isinstance(e.version, str) and e.version
    assert e.dim == expected_dim
    assert e.quality >= min_quality
    assert isinstance(e.max_input_chars, int) and e.max_input_chars > 0


def test_local_embedder_version_is_pinned():
    """LocalEmbedder.version must be 'tfidf-v1' (spec decision — affects
    staleness queries in memory_embedding_meta)."""
    assert LocalEmbedder().version == "tfidf-v1"


# FastEmbedder requires loading the model — slow test, mark as integration.
@pytest.mark.parametrize("expected_prefix", ["BAAI/"])
def test_fastembed_version_is_model_checkpoint(expected_prefix):
    """FastEmbedder.version must be the underlying model checkpoint id
    (per spec — NOT the fastembed package version)."""
    fe = FastEmbedder()  # downloads model if not cached
    assert fe.version.startswith(expected_prefix), (
        f"expected version to be model checkpoint, got {fe.version!r}"
    )
    assert fe.name == "fastembed"
    assert fe.dim == 384


# ═══════════════════════════════════════════════════════════════════
# T7 — HostedEmbedder ABC + 3 subclasses
# ═══════════════════════════════════════════════════════════════════


def test_hosted_embedder_is_abstract():
    """HostedEmbedder is abstract — instantiating it directly raises."""
    with pytest.raises(TypeError):
        HostedEmbedder(api_key="x")  # type: ignore[abstract]


def test_openai_auth_headers():
    """OpenAIEmbedder uses Bearer token auth."""
    e = OpenAIEmbedder(api_key="sk-test")
    headers = e._auth_headers()
    assert headers["Authorization"] == "Bearer sk-test"


def test_voyage_auth_headers():
    e = VoyageEmbedder(api_key="vy-test")
    headers = e._auth_headers()
    assert headers["Authorization"] == "Bearer vy-test"


def test_cohere_auth_headers():
    e = CohereEmbedder(api_key="co-test")
    headers = e._auth_headers()
    assert headers["Authorization"] == "Bearer co-test"


def test_openai_payload_shape():
    """OpenAI's payload format: {'input': [...], 'model': '...'}."""
    e = OpenAIEmbedder(api_key="sk-test")
    payload = e._build_payload(["hello", "world"])
    assert payload["input"] == ["hello", "world"]
    assert payload["model"] == e.version  # version IS the model id


def test_voyage_payload_shape():
    e = VoyageEmbedder(api_key="vy-test")
    payload = e._build_payload(["hello"])
    assert payload["input"] == ["hello"]
    assert payload["model"] == e.version


def test_cohere_payload_shape():
    e = CohereEmbedder(api_key="co-test")
    payload = e._build_payload(["hello"])
    assert payload["texts"] == ["hello"]  # Cohere uses "texts", not "input"
    assert payload["model"] == e.version


def test_openai_response_parsing():
    """OpenAI returns {'data': [{'embedding': [...]}], ...}."""
    e = OpenAIEmbedder(api_key="sk-test")
    body = {
        "data": [
            {"embedding": [0.1, 0.2, 0.3]},
            {"embedding": [0.4, 0.5, 0.6]},
        ],
        "model": e.version,
    }
    vectors = e._parse_response(body)
    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]


def test_voyage_response_parsing():
    e = VoyageEmbedder(api_key="vy-test")
    body = {"data": [{"embedding": [0.1, 0.2]}]}
    assert e._parse_response(body) == [[0.1, 0.2]]


def test_cohere_response_parsing():
    e = CohereEmbedder(api_key="co-test")
    body = {"embeddings": [[0.1, 0.2], [0.3, 0.4]]}
    assert e._parse_response(body) == [[0.1, 0.2], [0.3, 0.4]]


# Retry-with-backoff: mock httpx.post to fail twice then succeed.
def test_hosted_embedder_retry_succeeds_after_transient_failure(monkeypatch):
    """Retry logic: 3 attempts max, exponential backoff. Recovers from
    two transient failures."""
    import httpx
    e = OpenAIEmbedder(api_key="sk-test")

    call_count = {"n": 0}
    success_body = {"data": [{"embedding": [0.1] * 1536}]}

    def fake_post(url, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise httpx.ConnectError("transient failure")
        resp = MagicMock()
        resp.json.return_value = success_body
        resp.raise_for_status.return_value = None
        return resp

    monkeypatch.setattr(httpx, "post", fake_post)
    # Patch sleep to make test fast
    monkeypatch.setattr("time.sleep", lambda _: None)

    vectors = e.embed_batch(["hello"])
    assert call_count["n"] == 3
    assert len(vectors) == 1
    assert len(vectors[0]) == 1536


def test_hosted_embedder_retry_exhausts(monkeypatch):
    """After 3 failed attempts, the original exception propagates."""
    import httpx
    e = OpenAIEmbedder(api_key="sk-test")

    def always_fail(url, *args, **kwargs):
        raise httpx.ConnectError("persistent failure")

    monkeypatch.setattr(httpx, "post", always_fail)
    monkeypatch.setattr("time.sleep", lambda _: None)

    with pytest.raises(httpx.ConnectError):
        e.embed_batch(["hello"])


def test_hosted_embedder_embed_dispatches_to_batch(monkeypatch):
    """embed(text) is a thin shim over embed_batch([text]) for short inputs."""
    import httpx
    e = OpenAIEmbedder(api_key="sk-test")

    captured: dict = {}
    def fake_post(url, *args, **kwargs):
        captured["payload"] = kwargs.get("json")
        resp = MagicMock()
        resp.json.return_value = {"data": [{"embedding": [0.5] * 1536}]}
        resp.raise_for_status.return_value = None
        return resp

    monkeypatch.setattr(httpx, "post", fake_post)
    vec = e.embed("short text")
    assert len(vec) == 1536
    assert captured["payload"]["input"] == ["short text"]


# ═══════════════════════════════════════════════════════════════════
# T8 — Mean-pool adapter
# ═══════════════════════════════════════════════════════════════════


def test_mean_pool_for_long_input(monkeypatch):
    """A text longer than max_input_chars gets split into N segments,
    batch-embedded in one call returning N vectors, then mean-pooled
    + L2-normalized."""
    import httpx
    e = OpenAIEmbedder(api_key="sk-test")
    # Force a small max_input_chars to trigger pooling on a manageable test input.
    e._max_input_chars_override = 100

    captured_inputs: list[list[str]] = []
    def fake_post(url, *args, **kwargs):
        payload = kwargs.get("json", {})
        inputs = payload.get("input", [])
        captured_inputs.append(inputs)
        # Return one different-valued embedding per input segment
        body = {"data": [
            {"embedding": [float(i + 1)] * 1536} for i in range(len(inputs))
        ]}
        resp = MagicMock()
        resp.json.return_value = body
        resp.raise_for_status.return_value = None
        return resp

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("time.sleep", lambda _: None)

    long_text = "word " * 100  # 500 chars; with max=100, expect 5 segments
    vec = e.embed(long_text)

    # Single batched HTTP call (one POST returning N vectors).
    assert len(captured_inputs) == 1
    # The batched input contained multiple segments (mean-pool engaged).
    assert len(captured_inputs[0]) >= 2, (
        f"expected >=2 segments in batched embed_batch call, got {len(captured_inputs[0])}"
    )

    # L2 normalized
    import math
    norm = math.sqrt(sum(x * x for x in vec))
    assert abs(norm - 1.0) < 1e-4, f"vector not L2-normalized: norm={norm}"


def test_mean_pool_segment_cap(monkeypatch):
    """When segments exceed max_segments (32 default), excess is truncated
    with a warning logged."""
    import httpx, logging
    e = OpenAIEmbedder(api_key="sk-test")
    # Force tiny max so we generate >32 segments
    e._max_input_chars_override = 10

    def fake_post(url, *args, **kwargs):
        body = {"data": [{"embedding": [0.1] * 1536}]}
        resp = MagicMock()
        resp.json.return_value = body
        resp.raise_for_status.return_value = None
        return resp

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("time.sleep", lambda _: None)

    long_text = "x" * 10_000  # would generate 1000+ segments at max=10
    with patch("sage_memory.embedder.logger.warning") as warn:
        e.embed(long_text)
    assert warn.called, "expected truncation warning"


# ═══════════════════════════════════════════════════════════════════
# T9 — 6-scenario resolver test (per ADR-005 §Resolver rule)
# ═══════════════════════════════════════════════════════════════════


# Each scenario: (corpus_dim, available API keys, fastembed available?, expected outcome)
# Per ADR-005 §Resolver rule worked-scenarios table. Note: scenarios 2,3,5,6
# explicitly list only "T1 + T3" (no T2) — fastembed_available must be False
# for those to faithfully test the documented case.
RESOLVER_SCENARIOS = [
    pytest.param(
        384, [], True, "fastembed",
        id="scenario1_corpus384_T2_fastembed_T3_local",
    ),
    pytest.param(
        384, ["OPENAI_API_KEY"], False, "local",  # no T2; OpenAI is 1536d so doesn't match
        id="scenario2_corpus384_T1OpenAI_T3Local_picks_local",
    ),
    pytest.param(
        384, ["VOYAGE_API_KEY"], False, "local",  # no T2; Voyage is 512d
        id="scenario3_corpus384_T1Voyage_T3Local_picks_local",
    ),
    pytest.param(
        1536, [], True, "REFUSE",
        id="scenario4_corpus1536_T2_T3_only_refuses",
    ),
    pytest.param(
        1536, ["OPENAI_API_KEY"], False, "openai",
        id="scenario5_corpus1536_T1OpenAI_picks_openai",
    ),
    pytest.param(
        512, ["VOYAGE_API_KEY"], False, "voyage",
        id="scenario6_corpus512_T1Voyage_picks_voyage",
    ),
]


@pytest.mark.parametrize(
    "corpus_dim,env_keys,fastembed_available,expected", RESOLVER_SCENARIOS
)
def test_resolve_scenarios(monkeypatch, corpus_dim, env_keys, fastembed_available, expected):
    """Each row of ADR-005's worked-scenarios table."""
    # Scrub all hosted env vars first
    for k in ("OPENAI_API_KEY", "VOYAGE_API_KEY", "COHERE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    # Set requested ones
    for k in env_keys:
        monkeypatch.setenv(k, "test-key")

    # Toggle FastEmbedder availability via the resolver's discovery hook
    if not fastembed_available:
        monkeypatch.setattr(
            "sage_memory.embedder._fastembed_available", lambda: False
        )

    if expected == "REFUSE":
        with pytest.raises(Exception) as exc_info:
            resolve(corpus_dim=corpus_dim)
        # The error message must mention reindex (per ADR-005 §Failure mode)
        assert "reindex" in str(exc_info.value).lower(), (
            f"refuse error must mention reindex; got: {exc_info.value}"
        )
    else:
        embedder = resolve(corpus_dim=corpus_dim)
        assert embedder.name == expected, (
            f"expected {expected}, got {embedder.name}"
        )
        # And the chosen embedder's dim must match corpus_dim
        assert embedder.dim == corpus_dim


# ═══════════════════════════════════════════════════════════════════
# T10 — Dim-strategy refuse-write
# ═══════════════════════════════════════════════════════════════════


def test_dim_mismatch_refused_on_resolve():
    """A corpus locked at dim=1536 with only LocalEmbedder (384d) available
    refuses to start — covered by scenario 4 above. Verify here that the
    error is clear and actionable."""
    import os
    # Scrub all hosted env vars
    saved = {}
    for k in ("OPENAI_API_KEY", "VOYAGE_API_KEY", "COHERE_API_KEY"):
        if k in os.environ:
            saved[k] = os.environ.pop(k)
    try:
        with pytest.raises(Exception) as exc_info:
            resolve(corpus_dim=1536)
        msg = str(exc_info.value).lower()
        assert "1536" in msg or "dim" in msg
        assert "reindex" in msg
    finally:
        for k, v in saved.items():
            os.environ[k] = v
