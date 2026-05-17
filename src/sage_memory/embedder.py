"""Embedding backends with a clean Protocol interface.

Built-in tiers (cascade order: T0 explicit → T1 hosted-API → T2 FastEmbedder → T3 LocalEmbedder):
  LocalEmbedder    (T3) — zero-dep, char n-gram TF-IDF hashing (always available)
  FastEmbedder     (T2) — neural via fastembed (pip install sage-memory[neural])
  OpenAIEmbedder   (T1) — text-embedding-3-small, 1536d, via OPENAI_API_KEY
  VoyageEmbedder   (T1) — voyage-3-lite, 512d, via VOYAGE_API_KEY
  CohereEmbedder   (T1) — embed-english-v3.0, 1024d, via COHERE_API_KEY

The local embedder uses character n-grams (3-5 chars) which capture morphological
similarity: "authenticate" ↔ "authentication" ↔ "auth" ↔ "OAuth" all share
trigrams and produce closer vectors. Effective for LLM-authored content where
vocabulary is consistent between stored knowledge and queries.

Resolver: see `resolve(corpus_dim)` and ADR-005 §Resolver rule.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import struct
import time
from abc import ABC, abstractmethod
from typing import Protocol

import httpx

EMBEDDING_DIM = 384

logger = logging.getLogger("sage_memory.embedder")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Protocol
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class Embedder(Protocol):
    """Embedder protocol — extended in M1 with version + max_input_chars.

    `version` identifies the model checkpoint (not the package version).
    Used by memory_embedding_meta staleness queries: if the active
    embedder's (name, version, dim) doesn't match a row's meta, the row
    is stale.

    `max_input_chars` triggers mean-pool fall-through for hosted
    embedders. Local/Fast accept any length so it's advisory there.
    """
    @property
    def dim(self) -> int: ...
    @property
    def name(self) -> str: ...
    @property
    def version(self) -> str: ...
    @property
    def quality(self) -> float:
        """Signal quality in [0, 1]. Controls whether vec search is used.
        Hosted neural ≈ 0.85+, local TF-IDF ≈ 0.45."""
        ...
    @property
    def max_input_chars(self) -> int: ...

    def embed(self, text: str) -> list[float]: ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Local embedder (T3 — zero dependencies, always available)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_STOPWORDS = frozenset(
    "a an the is are was were be been being have has had do does did "
    "will would shall should may might can could to of in for on with "
    "at by from as into through during before after above below between "
    "out off over under again further then once here there when where "
    "why how all each every both few more most other some such no nor "
    "not only own same so than too very and but or if this that it its "
    "what which who whom whose need use add get set make "
    "def self cls return import class async await none true false".split()
)

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


class LocalEmbedder:
    """TF-IDF hashing with character n-grams. Fast (<0.5ms), zero deps."""

    @property
    def dim(self) -> int:
        return EMBEDDING_DIM

    @property
    def name(self) -> str:
        return "local"

    @property
    def version(self) -> str:
        # Hand-versioned tag: bump when the tokenizer or hashing scheme
        # changes in a way that affects vector content. memory_embedding_meta
        # staleness queries key on this.
        return "tfidf-v1"

    @property
    def quality(self) -> float:
        return 0.45

    @property
    def max_input_chars(self) -> int:
        # LocalEmbedder accepts any length; this is advisory only.
        return 8192

    def embed(self, text: str) -> list[float]:
        tokens = self._tokenize(text)
        if not tokens:
            return [1e-6] * EMBEDDING_DIM

        features: dict[str, float] = {}

        # Word unigrams + bigrams
        for t in tokens:
            features[f"w:{t}"] = features.get(f"w:{t}", 0) + 1.0
        for i in range(len(tokens) - 1):
            bg = f"b:{tokens[i]} {tokens[i + 1]}"
            features[bg] = features.get(bg, 0) + 1.5

        # Character n-grams (3, 4, 5) — morphological similarity
        full = " ".join(tokens)
        for n, w in ((3, 0.5), (4, 0.7), (5, 0.6)):
            for i in range(len(full) - n + 1):
                gram = f"c{n}:{full[i:i + n]}"
                features[gram] = features.get(gram, 0) + w

        # TF log-scaling
        for k in features:
            if features[k] > 1:
                features[k] = 1.0 + math.log(features[k])

        # Hash into vector
        vec = [0.0] * EMBEDDING_DIM
        for feat, weight in features.items():
            h = hashlib.md5(feat.encode(), usedforsecurity=False).digest()
            bucket = int.from_bytes(h[:4], "little") % EMBEDDING_DIM
            sign = 1.0 if h[4] & 1 else -1.0
            vec[bucket] += sign * weight

        # L2 normalize
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    def _tokenize(self, text: str) -> list[str]:
        lowered = _CAMEL_RE.sub(" ", text.lower())
        tokens = _TOKEN_RE.findall(lowered)
        result: list[str] = []
        for t in tokens:
            for part in t.split("_"):
                if len(part) >= 2 and part not in _STOPWORDS:
                    result.append(part)
        return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Neural embedder (T2 — optional: pip install sage-memory[neural])
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class FastEmbedder:
    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        from fastembed import TextEmbedding
        self._model_name = model
        self._model = TextEmbedding(model_name=model)
        self._dim = len(list(self._model.embed(["test"]))[0])

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def name(self) -> str:
        return "fastembed"

    @property
    def version(self) -> str:
        # Model checkpoint identifier — bumping the model bumps the version,
        # which forces memory_embedding_meta staleness checks to re-embed.
        return self._model_name

    @property
    def quality(self) -> float:
        return 0.85

    @property
    def max_input_chars(self) -> int:
        return 8192

    def embed(self, text: str) -> list[float]:
        return list(self._model.embed([text]))[0].tolist()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Hosted embedders (T1 — via API keys)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Tunables per ADR-005 §Pinned design decisions
_MAX_SEGMENTS_DEFAULT = 32       # mean-pool cap
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_BASE = 0.5        # seconds; doubles per attempt
_HTTP_TIMEOUT = 30.0             # seconds


class HostedEmbedder(ABC):
    """Base class for hosted-API embedders.

    Concrete subclasses (OpenAI/Voyage/Cohere) override only the four
    abstract hooks: _endpoint_url, _auth_headers, _build_payload,
    _parse_response. The base class owns httpx call, retry-with-backoff,
    batching, and mean-pool fall-through for long inputs.
    """

    # Subclasses set these in __init__ before calling super().__init__()
    _name: str = ""
    _version: str = ""
    _dim: int = 0
    _quality: float = 0.85
    _max_input_chars: int = 8192

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key
        self._max_input_chars_override: int | None = None  # test hook

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return self._version

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def quality(self) -> float:
        return self._quality

    @property
    def max_input_chars(self) -> int:
        return self._max_input_chars_override or self._max_input_chars

    # ─── concrete public methods ───

    def embed(self, text: str) -> list[float]:
        """Embed a single text. If longer than max_input_chars, splits
        into segments, embeds each, mean-pools + L2-normalizes."""
        if len(text) <= self.max_input_chars:
            vecs = self.embed_batch([text])
            return vecs[0]
        # Mean-pool fall-through for long inputs
        return self._embed_pooled(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts with retry-with-backoff. Returns
        list of vectors in the same order as inputs."""
        url = self._endpoint_url()
        headers = self._auth_headers()
        payload = self._build_payload(texts)

        last_exc: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                resp = httpx.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=_HTTP_TIMEOUT,
                )
                resp.raise_for_status()
                body = resp.json()
                return self._parse_response(body)
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                last_exc = e
                if attempt < _RETRY_ATTEMPTS - 1:
                    time.sleep(_RETRY_BACKOFF_BASE * (2 ** attempt))
                    continue
                raise
        # Unreachable, but keep the type checker happy
        raise last_exc  # type: ignore[misc]

    def _embed_pooled(self, text: str) -> list[float]:
        """Split long text into max-sized segments, embed each, mean-pool.
        Caps at _MAX_SEGMENTS_DEFAULT (32) with truncation warning."""
        chunk_size = self.max_input_chars
        segments: list[str] = []
        for i in range(0, len(text), chunk_size):
            segments.append(text[i:i + chunk_size])
            if len(segments) >= _MAX_SEGMENTS_DEFAULT:
                truncated_bytes = len(text) - i - chunk_size
                if truncated_bytes > 0:
                    logger.warning(
                        "embedder.mean_pool: truncating %d chars beyond "
                        "max_segments=%d × max_input_chars=%d (text len=%d). "
                        "Add '[truncated]' sentinel to last segment.",
                        truncated_bytes, _MAX_SEGMENTS_DEFAULT,
                        chunk_size, len(text),
                    )
                    # Append truncation sentinel to last segment
                    segments[-1] = segments[-1] + " [truncated]"
                break

        # Batch-embed all segments in one API call (most providers
        # support batches up to ~96 inputs; 32 is well within limits).
        vectors = self.embed_batch(segments)

        # Mean-pool element-wise
        if not vectors:
            return [0.0] * self._dim
        dim = len(vectors[0])
        pooled = [0.0] * dim
        for v in vectors:
            for i, x in enumerate(v):
                pooled[i] += x
        n = len(vectors)
        pooled = [x / n for x in pooled]

        # L2 normalize
        norm = math.sqrt(sum(x * x for x in pooled))
        if norm > 0:
            pooled = [x / norm for x in pooled]
        return pooled

    # ─── abstract hooks (subclasses MUST override) ───

    @abstractmethod
    def _endpoint_url(self) -> str: ...
    @abstractmethod
    def _auth_headers(self) -> dict[str, str]: ...
    @abstractmethod
    def _build_payload(self, texts: list[str]) -> dict: ...
    @abstractmethod
    def _parse_response(self, body: dict) -> list[list[float]]: ...


class OpenAIEmbedder(HostedEmbedder):
    """OpenAI text-embedding-3-small (1536d).

    Docs: https://platform.openai.com/docs/api-reference/embeddings
    """
    _name = "openai"
    _version = "text-embedding-3-small"
    _dim = 1536

    def _endpoint_url(self) -> str:
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        return f"{base}/embeddings"

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(self, texts: list[str]) -> dict:
        return {"input": texts, "model": self._version}

    def _parse_response(self, body: dict) -> list[list[float]]:
        return [item["embedding"] for item in body["data"]]


class VoyageEmbedder(HostedEmbedder):
    """Voyage voyage-3-lite (512d).

    Docs: https://docs.voyageai.com/reference/embeddings-api
    """
    _name = "voyage"
    _version = "voyage-3-lite"
    _dim = 512

    def _endpoint_url(self) -> str:
        base = os.environ.get("VOYAGE_BASE_URL", "https://api.voyageai.com/v1")
        return f"{base}/embeddings"

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(self, texts: list[str]) -> dict:
        return {"input": texts, "model": self._version}

    def _parse_response(self, body: dict) -> list[list[float]]:
        return [item["embedding"] for item in body["data"]]


class CohereEmbedder(HostedEmbedder):
    """Cohere embed-english-v3.0 (1024d).

    Docs: https://docs.cohere.com/reference/embed
    """
    _name = "cohere"
    _version = "embed-english-v3.0"
    _dim = 1024

    def _endpoint_url(self) -> str:
        base = os.environ.get("COHERE_BASE_URL", "https://api.cohere.com/v1")
        return f"{base}/embed"

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(self, texts: list[str]) -> dict:
        return {
            "texts": texts,
            "model": self._version,
            "input_type": "search_document",
        }

    def _parse_response(self, body: dict) -> list[list[float]]:
        return body["embeddings"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Resolver (ADR-005 §Resolver rule)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class DimMismatchRefuseError(RuntimeError):
    """Raised when corpus_meta.vec_dim doesn't match any available embedder
    tier — the user must reindex with --re-embed or configure a matching
    provider. Never silently down-projects (ADR-005 explicitly rejects this)."""


def _hosted_tier_candidates() -> list[type[HostedEmbedder]]:
    """Return hosted-embedder classes whose API key is present in env.
    Order matters: first-match wins within the hosted tier."""
    candidates: list[type[HostedEmbedder]] = []
    if os.environ.get("OPENAI_API_KEY"):
        candidates.append(OpenAIEmbedder)
    if os.environ.get("VOYAGE_API_KEY"):
        candidates.append(VoyageEmbedder)
    if os.environ.get("COHERE_API_KEY"):
        candidates.append(CohereEmbedder)
    return candidates


def _fastembed_available() -> bool:
    """Check whether `fastembed` is importable. Doesn't construct an
    instance (which would download the model)."""
    import importlib.util
    try:
        spec = importlib.util.find_spec("fastembed")
        return spec is not None
    except (ImportError, ValueError):
        return False


def resolve(corpus_dim: int) -> Embedder:
    """Pick the highest-tier embedder whose native dim == corpus_dim.

    Per ADR-005 §Resolver rule (six worked scenarios):
    1. Build candidates = embedders with native_dim == corpus_dim that
       are available (key configured or class always available).
    2. If candidates: pick highest tier (Tier 1 hosted > Tier 2
       FastEmbedder > Tier 3 LocalEmbedder). Log if a higher-tier
       embedder is available but its dim doesn't match (user should
       reindex).
    3. If no candidates: refuse with DimMismatchRefuseError (no silent
       down-projection).
    """
    # Tier 1 — hosted-API embedders, in priority order
    hosted_matching: list[type[HostedEmbedder]] = []
    hosted_dim_mismatched: list[type[HostedEmbedder]] = []
    for cls in _hosted_tier_candidates():
        if cls._dim == corpus_dim:
            hosted_matching.append(cls)
        else:
            hosted_dim_mismatched.append(cls)

    if hosted_matching:
        cls = hosted_matching[0]
        key = os.environ[_api_key_env_for(cls)]
        return cls(api_key=key)

    # Tier 2 — FastEmbedder (384d)
    fastembed_matches = _fastembed_available() and corpus_dim == 384

    if fastembed_matches:
        # Warn about any higher-tier embedders with mismatched dim
        for cls in hosted_dim_mismatched:
            logger.info(
                "Hosted embedder available (%s/%s, %dd) but corpus is "
                "locked at %dd. To upgrade: sage-memory reindex --re-embed "
                "--embedder %s",
                cls._name, cls._version, cls._dim, corpus_dim, cls._name,
            )
        return FastEmbedder()

    # Tier 3 — LocalEmbedder (384d, always available)
    if corpus_dim == 384:
        for cls in hosted_dim_mismatched:
            logger.info(
                "Hosted embedder available (%s/%s, %dd) but corpus is "
                "locked at %dd. To upgrade: sage-memory reindex --re-embed "
                "--embedder %s",
                cls._name, cls._version, cls._dim, corpus_dim, cls._name,
            )
        return LocalEmbedder()

    # No tier matches the corpus dim — refuse with actionable error
    available = (
        [f"{cls._name}({cls._dim}d)" for cls in hosted_dim_mismatched]
        + (["fastembed(384d)"] if _fastembed_available() else [])
        + ["local(384d)"]
    )
    raise DimMismatchRefuseError(
        f"corpus is locked at vec_dim={corpus_dim} but no available "
        f"embedder produces vectors of that dimension. "
        f"Available: {', '.join(available)}. "
        f"To proceed, either configure a {corpus_dim}d provider OR run "
        f"`sage-memory reindex --re-embed --embedder <name>` to relocate "
        f"the corpus to a different dim."
    )


def _api_key_env_for(cls: type[HostedEmbedder]) -> str:
    """Map a hosted-embedder class to its env var."""
    return {
        OpenAIEmbedder: "OPENAI_API_KEY",
        VoyageEmbedder: "VOYAGE_API_KEY",
        CohereEmbedder: "COHERE_API_KEY",
    }[cls]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Singleton + factory
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_instance: Embedder | None = None


def get_embedder() -> Embedder:
    global _instance
    if _instance is None:
        _instance = LocalEmbedder()
    return _instance


def set_embedder(embedder: Embedder) -> None:
    global _instance
    _instance = embedder


def serialize_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)
