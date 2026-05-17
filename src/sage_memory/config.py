"""M5 — Three-layer config cascade.

Per ADR-004 §"Configuration cascade" + spec rev 2.1 §"Pinned design
decisions". Layer order:

  Layer 1 — per-call kwarg (`override=` arg on `get()`)
  Layer 2 — env vars (mechanical SAGE_MEMORY_* AND legacy aliases)
  Layer 3 — `.sage/config.yaml` in the project root (lazy-loaded)
  Layer 4 — built-in defaults (mirrors ADR-004's worked YAML)

Type coercion is driven by the yaml type at the key path. Env-only
keys (no yaml entry, no built-in entry) stay as str.

The 11-row legacy alias table preserves M3a/M3b/M4 env-var names as
Layer-2 aliases. Each accessed legacy name emits at most one DEBUG
log line per process (deduped via `_DEPRECATION_LOGGED`); the dedup
set is cleared by `reload()` so tests can re-trigger.

NEW M5 code (T2/T3/T4/T5 CLI subcommands + worker dedup task) uses
`config.get()` for new tunables. EXISTING M3a/M3b/M4 modules
(expand.py, rerank.py, etc.) continue env-direct reads —
backward-compat is preserved because the env-var names ARE Layer 2.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml as _yaml


logger = logging.getLogger("sage_memory.config")


# ─── Sentinels + errors ───────────────────────────────────────────


_UNSET = object()  # distinguishes "not provided" from "explicit None"


class ConfigError(RuntimeError):
    """Raised for malformed yaml, unknown keys, or coercion failures."""


# ─── Built-in defaults (Layer 4) — mirrors ADR-004 worked YAML ────


_BUILT_IN_DEFAULTS: dict = {
    "retrieval": {
        "expand": {
            "enabled": "auto",   # auto | true | false
            "strong_signal_top1_norm": 0.4,
            "strong_signal_ratio": 2.0,
            "llm_model": None,
        },
        "rerank": {
            "enabled": "auto",
            "top_k": 15,
            "blend_curve": [0.75, 0.6, 0.4],
            "blend_transitions": [3, 10],
            "llm_model": None,
            "failure_visibility": "warn",
        },
        "channels": {
            "bm25": {"weight": 1.0},
            "vector": {"weight": "auto"},
            "graph": {"weight": 0.7, "depth": 2, "rank_curve": "linear"},
        },
        "fusion": {"k_rrf": 60},
    },
    "dedup": {
        "interval": None,           # null = disabled
        "interval_enabled": False,  # paired bool for env-coercion tests
    },
    "llm": {
        # API keys are READ-ONLY passthroughs from env; no yaml form.
        "anthropic_api_key": None,
        "openai_api_key": None,
        "model_override": None,
    },
    "embedding": {
        "voyage_api_key": None,
        "cohere_api_key": None,
    },
}


# ─── Legacy env-var alias table (spec §"Pinned design decisions") ─


_LEGACY_ALIASES: dict[str, str] = {
    # 4 API keys (READ-ONLY passthroughs)
    "ANTHROPIC_API_KEY":              "llm.anthropic_api_key",
    "OPENAI_API_KEY":                 "llm.openai_api_key",
    "VOYAGE_API_KEY":                 "embedding.voyage_api_key",
    "COHERE_API_KEY":                 "embedding.cohere_api_key",
    # 7 SAGE_* knobs
    "SAGE_LLM_MODEL":                 "llm.model_override",
    "SAGE_GRAPH_RANK_CURVE":          "retrieval.channels.graph.rank_curve",
    "SAGE_EXPAND_TOP1_NORM":          "retrieval.expand.strong_signal_top1_norm",
    "SAGE_EXPAND_TOP1_RATIO":         "retrieval.expand.strong_signal_ratio",
    "SAGE_RERANK_TOP_K":              "retrieval.rerank.top_k",
    "SAGE_RERANK_FAILURE_VISIBILITY": "retrieval.rerank.failure_visibility",
    "SAGE_RERANK_BLEND_CURVE":        "retrieval.rerank.blend_curve",
}

# Reverse lookup: yaml_key → set of legacy env vars that target it.
_KEY_TO_LEGACY: dict[str, list[str]] = {}
for _env, _key in _LEGACY_ALIASES.items():
    _KEY_TO_LEGACY.setdefault(_key, []).append(_env)


# ─── Module-level state ───────────────────────────────────────────


_YAML_CACHE: dict | None = None
_DEPRECATION_LOGGED: set[str] = set()


def _config_dir() -> Path:
    """Where .sage/config.yaml lives. Env-overridable for tests."""
    override = os.environ.get("SAGE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.cwd()


def _config_yaml_path() -> Path:
    return _config_dir() / ".sage" / "config.yaml"


def _load_yaml() -> dict:
    """Lazy yaml load. Returns {} when the file is absent."""
    global _YAML_CACHE
    if _YAML_CACHE is not None:
        return _YAML_CACHE
    path = _config_yaml_path()
    if not path.exists():
        _YAML_CACHE = {}
        return _YAML_CACHE
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = _yaml.safe_load(raw) or {}
    except _yaml.YAMLError as e:
        raise ConfigError(
            f"malformed config.yaml at {path}: {e}"
        ) from e
    if not isinstance(parsed, dict):
        raise ConfigError(
            f"config.yaml at {path} must be a mapping at top level"
        )
    # Spec mandates sage_memory namespace at the top.
    inner = parsed.get("sage_memory", {})
    if not isinstance(inner, dict):
        raise ConfigError(
            f"config.yaml at {path}: 'sage_memory' must be a mapping"
        )
    _YAML_CACHE = inner
    return _YAML_CACHE


# ─── Lookups inside the nested defaults / yaml trees ──────────────


def _nested_get(tree: dict, key_path: str):
    """Return the value at dotted `key_path`, or `_UNSET` if absent."""
    cur: Any = tree
    for part in key_path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _UNSET
        cur = cur[part]
    return cur


# ─── Env var → yaml-key resolution ────────────────────────────────


def _env_value_for_key(key_path: str) -> tuple[Any, str | None]:
    """Return (env_value, legacy_env_name) or (_UNSET, None).

    Checks legacy aliases first; then the mechanical
    SAGE_MEMORY_<UPPER_SNAKE> form for new keys.
    """
    for legacy in _KEY_TO_LEGACY.get(key_path, []):
        if legacy in os.environ:
            return os.environ[legacy], legacy
    mechanical = "SAGE_MEMORY_" + key_path.upper().replace(".", "_")
    if mechanical in os.environ:
        return os.environ[mechanical], None
    return _UNSET, None


def _log_deprecation_once(legacy: str) -> None:
    if legacy in _DEPRECATION_LOGGED:
        return
    _DEPRECATION_LOGGED.add(legacy)
    logger.debug(
        "config: legacy env var %s honored (Layer 2 alias). "
        "Consider migrating to .sage/config.yaml.",
        legacy,
    )


# ─── Type coercion ────────────────────────────────────────────────


def _coerce(value: str, hint: Any) -> Any:
    """Coerce env string `value` based on the type of yaml-or-builtin
    `hint` at the same key path."""
    if hint is _UNSET or hint is None:
        return value
    if isinstance(hint, bool):
        if value.lower() in ("true", "1"):
            return True
        if value.lower() in ("false", "0"):
            return False
        raise ConfigError(
            f"cannot coerce env value {value!r} to bool"
        )
    if isinstance(hint, int) and not isinstance(hint, bool):
        try:
            return int(value)
        except ValueError as e:
            raise ConfigError(
                f"cannot coerce env value {value!r} to int"
            ) from e
    if isinstance(hint, float):
        try:
            return float(value)
        except ValueError as e:
            raise ConfigError(
                f"cannot coerce env value {value!r} to float"
            ) from e
    if isinstance(hint, list):
        # Comma-separated. Coerce each item to match the first
        # element's type if hint has elements; else leave as str.
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if hint and isinstance(hint[0], float):
            try:
                return [float(p) for p in parts]
            except ValueError as e:
                raise ConfigError(
                    f"cannot coerce env value {value!r} to list[float]"
                ) from e
        if hint and isinstance(hint[0], int):
            try:
                return [int(p) for p in parts]
            except ValueError as e:
                raise ConfigError(
                    f"cannot coerce env value {value!r} to list[int]"
                ) from e
        return parts
    # Other types (e.g., str): keep as str.
    return value


# ─── Public API ───────────────────────────────────────────────────


def get(key_path: str, *, override: Any = _UNSET) -> Any:
    """Resolve `key_path` through the 3-layer cascade.

    Args:
        key_path: dotted yaml path (e.g., 'retrieval.rerank.top_k').
            `.` is the separator. Unknown keys (not in built-ins,
            yaml, or env) raise ConfigError.
        override: per-call kwarg. `_UNSET` (default) = consult
            cascade; any other value (including None) returns
            that value directly.

    Returns: the resolved value, type-coerced per the yaml/built-in
    type at the key path.
    """
    if override is not _UNSET:
        return override

    # Layer 2 — env var (with legacy alias lookup).
    env_val, legacy = _env_value_for_key(key_path)
    # Layer 3 — yaml.
    yaml_val = _nested_get(_load_yaml(), key_path)
    # Layer 4 — built-in.
    builtin_val = _nested_get(_BUILT_IN_DEFAULTS, key_path)

    if env_val is not _UNSET:
        if legacy is not None:
            _log_deprecation_once(legacy)
        # Coercion hint: yaml type takes precedence over built-in.
        hint = yaml_val if yaml_val is not _UNSET else builtin_val
        return _coerce(env_val, hint)
    if yaml_val is not _UNSET:
        return yaml_val
    if builtin_val is not _UNSET:
        return builtin_val
    raise ConfigError(f"unknown config key: {key_path!r}")


def get_all() -> dict:
    """Return the fully-resolved config tree (built-ins overlaid by
    yaml; secrets excluded). Used by `.sage/config.yaml.example`
    generation + future status CLI extensions."""
    import copy
    out = copy.deepcopy(_BUILT_IN_DEFAULTS)
    yaml_tree = _load_yaml()

    def _merge(dest: dict, src: dict) -> None:
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dest.get(k), dict):
                _merge(dest[k], v)
            else:
                dest[k] = v

    _merge(out, yaml_tree)
    # Strip secrets.
    out.get("llm", {}).pop("anthropic_api_key", None)
    out.get("llm", {}).pop("openai_api_key", None)
    out.get("embedding", {}).pop("voyage_api_key", None)
    out.get("embedding", {}).pop("cohere_api_key", None)
    return out


def reload() -> None:
    """Drop the yaml cache + the per-process deprecation-log dedup
    set. Tests use this after writing/mutating yaml or env state.

    Production code does NOT call this — yaml edits require a process
    restart by convention (matches M3a/M3b/M4 env-var contract)."""
    global _YAML_CACHE
    _YAML_CACHE = None
    _DEPRECATION_LOGGED.clear()
