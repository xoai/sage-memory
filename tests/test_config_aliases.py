"""M5 T1 — config.py legacy env-var alias matrix tests.

Spec A2: 11 legacy env vars (4 API keys + 7 SAGE_* knobs), each
preserved as a Layer-2 alias. For each var, 3 tests:
  - Test 1: env+reload → resolved value matches.
  - Test 2: yaml alone (env unset) → resolved value matches.
  - Test 3: env+yaml both set → env wins (Layer 2 > Layer 3).

Plus 2 wrap-up tests:
  - no-double-DEBUG-log within a single process.
  - deprecation log re-plays after config.reload().

Per spec §"Pinned design decisions" → "Env var → yaml-key mapping".
"""

from __future__ import annotations

import importlib
import logging

import pytest


# ─── Fixtures (mirror test_config_cascade.py) ─────────────────────


_ALL_LEGACY_ENV = [
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "VOYAGE_API_KEY", "COHERE_API_KEY",
    "SAGE_LLM_MODEL", "SAGE_GRAPH_RANK_CURVE",
    "SAGE_EXPAND_TOP1_NORM", "SAGE_EXPAND_TOP1_RATIO",
    "SAGE_RERANK_TOP_K", "SAGE_RERANK_FAILURE_VISIBILITY",
    "SAGE_RERANK_BLEND_CURVE",
]


def _scrub(monkeypatch):
    for var in _ALL_LEGACY_ENV:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fresh_config(monkeypatch, tmp_path):
    _scrub(monkeypatch)
    monkeypatch.setenv("SAGE_CONFIG_DIR", str(tmp_path))
    from sage_memory import config
    importlib.reload(config)
    return config


def _write_yaml(tmp_path, content: str):
    sage_dir = tmp_path / ".sage"
    sage_dir.mkdir(exist_ok=True)
    (sage_dir / "config.yaml").write_text(content, encoding="utf-8")


# Each row: (legacy_env_name, yaml_key, env_value, yaml_value, expected_env, expected_yaml)
# `expected_*` is what config.get(yaml_key) returns when only that
# source is set (after coercion per yaml type at the key path).
_ALIASES = [
    # 4 API keys — strings, no coercion
    ("ANTHROPIC_API_KEY", "llm.anthropic_api_key",
     "sk-ant-test", None, "sk-ant-test", None),
    ("OPENAI_API_KEY", "llm.openai_api_key",
     "sk-openai-test", None, "sk-openai-test", None),
    ("VOYAGE_API_KEY", "embedding.voyage_api_key",
     "voyage-test", None, "voyage-test", None),
    ("COHERE_API_KEY", "embedding.cohere_api_key",
     "cohere-test", None, "cohere-test", None),
    # SAGE_LLM_MODEL — string
    ("SAGE_LLM_MODEL", "llm.model_override",
     "claude-haiku-4-5", "gpt-4o-mini",
     "claude-haiku-4-5", "gpt-4o-mini"),
    # SAGE_GRAPH_RANK_CURVE — string (one of: linear/harmonic/type-weighted)
    ("SAGE_GRAPH_RANK_CURVE", "retrieval.channels.graph.rank_curve",
     "harmonic", "linear", "harmonic", "linear"),
    # SAGE_EXPAND_TOP1_NORM — float
    ("SAGE_EXPAND_TOP1_NORM", "retrieval.expand.strong_signal_top1_norm",
     "0.7", 0.5, 0.7, 0.5),
    # SAGE_EXPAND_TOP1_RATIO — float
    ("SAGE_EXPAND_TOP1_RATIO", "retrieval.expand.strong_signal_ratio",
     "3.5", 2.5, 3.5, 2.5),
    # SAGE_RERANK_TOP_K — int
    ("SAGE_RERANK_TOP_K", "retrieval.rerank.top_k",
     "20", 10, 20, 10),
    # SAGE_RERANK_FAILURE_VISIBILITY — string (warn|silent|error)
    ("SAGE_RERANK_FAILURE_VISIBILITY", "retrieval.rerank.failure_visibility",
     "silent", "warn", "silent", "warn"),
    # SAGE_RERANK_BLEND_CURVE — list[float] CSV
    ("SAGE_RERANK_BLEND_CURVE", "retrieval.rerank.blend_curve",
     "0.9,0.5,0.1", [0.7, 0.5, 0.3],
     [0.9, 0.5, 0.1], [0.7, 0.5, 0.3]),
]


@pytest.mark.parametrize("legacy,yaml_key,env_val,_y,expected,_e", _ALIASES)
def test_alias_env_resolves(
    fresh_config, monkeypatch, legacy, yaml_key, env_val, _y, expected, _e,
):
    """A2 Test 1: env-only → resolved value matches."""
    monkeypatch.setenv(legacy, env_val)
    fresh_config.reload()
    assert fresh_config.get(yaml_key) == expected


@pytest.mark.parametrize("legacy,yaml_key,_e,yaml_val,_x,expected", _ALIASES)
def test_alias_yaml_alone_resolves(
    fresh_config, tmp_path, legacy, yaml_key, _e, yaml_val, _x, expected,
):
    """A2 Test 2: yaml-only (env unset) → resolved value matches."""
    if yaml_val is None:
        pytest.skip("API key has no yaml form (READ-ONLY passthrough)")
    # Build yaml from the yaml_key path.
    yaml_path = yaml_key.split(".")
    # Construct nested dict
    obj = yaml_val
    for part in reversed(yaml_path):
        obj = {part: obj}
    obj = {"sage_memory": obj}
    import yaml as yamllib
    _write_yaml(tmp_path, yamllib.dump(obj))
    fresh_config.reload()
    assert fresh_config.get(yaml_key) == expected


@pytest.mark.parametrize("legacy,yaml_key,env_val,yaml_val,expected,_y", _ALIASES)
def test_alias_env_wins_over_yaml(
    fresh_config, monkeypatch, tmp_path,
    legacy, yaml_key, env_val, yaml_val, expected, _y,
):
    """A2 Test 3 (rev1-review CRIT #3): env + yaml both set → env wins.

    Layer 2 > Layer 3 per ADR-004.
    """
    if yaml_val is None:
        pytest.skip("API key has no yaml form (READ-ONLY passthrough)")
    yaml_path = yaml_key.split(".")
    obj = yaml_val
    for part in reversed(yaml_path):
        obj = {part: obj}
    obj = {"sage_memory": obj}
    import yaml as yamllib
    _write_yaml(tmp_path, yamllib.dump(obj))
    monkeypatch.setenv(legacy, env_val)
    fresh_config.reload()
    assert fresh_config.get(yaml_key) == expected


# ─── Wrap-up tests ────────────────────────────────────────────────


def test_alias_no_double_DEBUG_log_within_process(
    fresh_config, monkeypatch, caplog,
):
    """Reading the same legacy alias twice within a process emits at
    most one DEBUG log line — deduplicated via the process-level
    `_DEPRECATION_LOGGED` set."""
    monkeypatch.setenv("SAGE_RERANK_TOP_K", "20")
    fresh_config.reload()
    with caplog.at_level(logging.DEBUG, logger="sage_memory.config"):
        fresh_config.get("retrieval.rerank.top_k")
        fresh_config.get("retrieval.rerank.top_k")
    deprecation_records = [
        r for r in caplog.records
        if r.name == "sage_memory.config"
        and r.levelno == logging.DEBUG
        and "SAGE_RERANK_TOP_K" in r.message
    ]
    assert len(deprecation_records) <= 1, (
        f"expected ≤1 DEBUG log per process; got {len(deprecation_records)}"
    )


def test_alias_deprecation_log_replays_after_reload(
    fresh_config, monkeypatch, caplog,
):
    """`config.reload()` clears `_DEPRECATION_LOGGED` so a subsequent
    `get()` re-emits the DEBUG. Lets tests re-trigger the deprecation
    log without `importlib.reload(config)`."""
    monkeypatch.setenv("SAGE_RERANK_TOP_K", "20")
    fresh_config.reload()

    with caplog.at_level(logging.DEBUG, logger="sage_memory.config"):
        fresh_config.get("retrieval.rerank.top_k")
    first_run_logs = [
        r for r in caplog.records
        if r.name == "sage_memory.config"
        and r.levelno == logging.DEBUG
        and "SAGE_RERANK_TOP_K" in r.message
    ]
    assert len(first_run_logs) == 1

    caplog.clear()
    fresh_config.reload()  # clears _DEPRECATION_LOGGED

    with caplog.at_level(logging.DEBUG, logger="sage_memory.config"):
        fresh_config.get("retrieval.rerank.top_k")
    second_run_logs = [
        r for r in caplog.records
        if r.name == "sage_memory.config"
        and r.levelno == logging.DEBUG
        and "SAGE_RERANK_TOP_K" in r.message
    ]
    assert len(second_run_logs) == 1, (
        "DEBUG should re-emit after reload() clears _DEPRECATION_LOGGED"
    )
