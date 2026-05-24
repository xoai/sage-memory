"""T0 — Embedder normalization probes.

The 0.12.0 semantic-dedup design assumes embeddings are unit-length
so the L2-distance → cosine-similarity identity
`cos = 1 - (L2**2)/2` holds. These probes verify the assumption for
each embedder sage-memory actually uses under the test environment.

Test (e) (NaN-vector rejection via `find_near_duplicate`) is
skip-marked here and unskipped in T1 once `semantic_dedup` lands.
"""

from __future__ import annotations

import math
import os

import pytest

from sage_memory.embedder import LocalEmbedder


_NORM_TOL = 1e-3   # acceptable deviation from unit norm


def _l2_norm(vec: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vec))


# ─── (a) LocalEmbedder ────────────────────────────────────────────


def test_local_embedder_produces_unit_norm():
    """LocalEmbedder explicitly L2-normalizes (embedder.py:150-153)."""
    e = LocalEmbedder()
    v = e.embed("the payment service uses saga for distributed transactions")
    norm = _l2_norm(v)
    assert abs(norm - 1.0) < _NORM_TOL, (
        f"LocalEmbedder norm {norm} not in [1-{_NORM_TOL}, 1+{_NORM_TOL}]"
    )


# ─── (b) FastEmbedder ─────────────────────────────────────────────


def test_fastembedder_produces_unit_norm(embedder_fastembed):
    """bge-small-en-v1.5 outputs L2-normalized vectors by default.

    Verified here because our code does NOT explicitly renormalize
    after fastembed's `embed()` call (embedder.py:201-202); semantic
    dedup relies on the model's defaults holding.
    """
    v = embedder_fastembed.embed(
        "the payment service uses saga for distributed transactions"
    )
    norm = _l2_norm(v)
    assert abs(norm - 1.0) < _NORM_TOL, (
        f"FastEmbedder norm {norm} not in [1-{_NORM_TOL}, 1+{_NORM_TOL}]"
    )


# ─── (c) HostedEmbedder (OpenAI path) ─────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="requires OPENAI_API_KEY (skipped in CI by default)",
)
def test_hosted_openai_embed_batch_produces_unit_norm():
    """`HostedEmbedder.embed_batch` returns provider vectors unchanged
    (embedder.py:268 — no normalization step in the non-pooled path).
    OpenAI's text-embedding-3-small returns L2-normalized vectors by
    policy, but our code doesn't enforce it — this probe confirms.
    """
    from sage_memory.embedder import OpenAIEmbedder
    e = OpenAIEmbedder(api_key=os.environ["OPENAI_API_KEY"])
    vecs = e.embed_batch(["the payment service uses saga"])
    norm = _l2_norm(vecs[0])
    assert abs(norm - 1.0) < _NORM_TOL, (
        f"OpenAIEmbedder.embed_batch norm {norm} not unit "
        f"(provider may have changed default)"
    )


# ─── (d) LocalEmbedder whitespace-only fallback ───────────────────


def test_local_embedder_whitespace_fallback_is_not_all_zero():
    """`LocalEmbedder.embed("   ")` returns `[1e-6] * 384` (embedder.py:118-119).

    The fallback is not zero, so `find_near_duplicate`'s
    zero-norm short-circuit doesn't fire; instead, the defensive
    normalize path rescales to unit length. This test guards the
    fallback shape so a future change to the LocalEmbedder
    doesn't silently start returning all-zero vectors (which would
    then make `find_near_duplicate` silently skip dedup for any
    whitespace-only memory).
    """
    e = LocalEmbedder()
    v = e.embed("   ")
    assert len(v) == 384
    assert any(x != 0.0 for x in v), (
        "LocalEmbedder whitespace fallback is all-zero; would break "
        "semantic-dedup's zero-norm short-circuit assumption"
    )
    # And it has positive (non-zero) norm — defensive normalize can
    # rescale to unit.
    assert _l2_norm(v) > 0.0


# ─── (e) NaN-vector defensive rejection (skipped until T1) ────────


def test_find_near_duplicate_rejects_nan_vector(tmp_path):
    """When `embedding` contains NaN, L2 norm is NaN (non-finite).
    `find_near_duplicate`'s defensive check returns None rather
    than crash (T1 unskipped this; full coverage in test_semantic_dedup)."""
    import sqlite3
    from sage_memory.semantic_dedup import find_near_duplicate
    # Use a bare connection — find_near_duplicate must short-circuit
    # on the bad embedding BEFORE any sqlite call.
    db = sqlite3.connect(str(tmp_path / "bare.db"))
    db.row_factory = sqlite3.Row
    bad = [0.5] * 384
    bad[0] = float("nan")
    assert find_near_duplicate(db, embedding=bad) is None
    db.close()
