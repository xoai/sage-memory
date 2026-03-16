"""Embedding backends with a clean Protocol interface.

Built-in:
  LocalEmbedder  — zero-dep, char n-gram TF-IDF hashing (default)
  FastEmbedder   — neural embeddings via fastembed (pip install sage-memory[neural])

The local embedder uses character n-grams (3-5 chars) which capture morphological
similarity: "authenticate" ↔ "authentication" ↔ "auth" ↔ "OAuth" all share
trigrams and produce closer vectors. Effective for LLM-authored content where
vocabulary is consistent between stored knowledge and queries.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from typing import Protocol

EMBEDDING_DIM = 384


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Protocol
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class Embedder(Protocol):
    @property
    def dim(self) -> int: ...
    @property
    def name(self) -> str: ...
    @property
    def quality(self) -> float:
        """Signal quality in [0, 1]. Controls whether vec search is used.
        Neural ≈ 0.85, local TF-IDF ≈ 0.45."""
        ...

    def embed(self, text: str) -> list[float]: ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Local embedder (zero dependencies)
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
    def quality(self) -> float:
        return 0.45

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
# Neural embedder (optional: pip install sage-memory[neural])
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class FastEmbedder:
    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        from fastembed import TextEmbedding
        self._model = TextEmbedding(model_name=model)
        self._dim = len(list(self._model.embed(["test"]))[0])

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def name(self) -> str:
        return "fastembed"

    @property
    def quality(self) -> float:
        return 0.85

    def embed(self, text: str) -> list[float]:
        return list(self._model.embed([text]))[0].tolist()


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
