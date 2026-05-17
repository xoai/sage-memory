"""M5 T1 — config.py cascade resolution tests.

Spec A1: per-call > env > yaml > built-in. Includes `_UNSET` sentinel
for `override`, `.` separator, type coercion driven by yaml type at
the key path, unknown-key raises ConfigError.

Tests run with `monkeypatch.setenv` + `config.reload()` (NOT
`importlib.reload(config)`) because the env-read is read-through
at every `get()` call; yaml is cached and only re-read on reload().
"""

from __future__ import annotations

import importlib

import pytest


# ─── Helpers ──────────────────────────────────────────────────────


def _scrub_all_legacy_env(monkeypatch):
    """Remove every legacy env var so cascade tests start from a clean
    Layer-2 state."""
    for var in [
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
        "VOYAGE_API_KEY", "COHERE_API_KEY",
        "SAGE_LLM_MODEL", "SAGE_GRAPH_RANK_CURVE",
        "SAGE_EXPAND_TOP1_NORM", "SAGE_EXPAND_TOP1_RATIO",
        "SAGE_RERANK_TOP_K", "SAGE_RERANK_FAILURE_VISIBILITY",
        "SAGE_RERANK_BLEND_CURVE",
        "SAGE_MEMORY_RETRIEVAL_RERANK_TOP_K",
        "SAGE_MEMORY_RETRIEVAL_EXPAND_STRONG_SIGNAL_TOP1_NORM",
    ]:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fresh_config(monkeypatch, tmp_path):
    """Return a fresh config module pointed at an empty tmp yaml path.

    Tests can write tmp_path/.sage/config.yaml to exercise Layer 3.
    """
    _scrub_all_legacy_env(monkeypatch)
    monkeypatch.setenv("SAGE_CONFIG_DIR", str(tmp_path))
    from sage_memory import config
    importlib.reload(config)
    return config


def _write_yaml(tmp_path, content: str):
    sage_dir = tmp_path / ".sage"
    sage_dir.mkdir(exist_ok=True)
    (sage_dir / "config.yaml").write_text(content, encoding="utf-8")


# ─── A1: cascade layer resolution ─────────────────────────────────


def test_layer4_builtin_returned_when_nothing_set(fresh_config):
    """No yaml + no env + no override → built-in default returned."""
    assert fresh_config.get("retrieval.rerank.top_k") == 15


def test_layer3_yaml_overrides_builtin(fresh_config, tmp_path):
    """Layer 3 (yaml) overrides Layer 4 (built-in)."""
    _write_yaml(tmp_path, """
sage_memory:
  retrieval:
    rerank:
      top_k: 25
""")
    fresh_config.reload()
    assert fresh_config.get("retrieval.rerank.top_k") == 25


def test_layer2_env_overrides_yaml(fresh_config, monkeypatch, tmp_path):
    """Layer 2 (env) overrides Layer 3 (yaml)."""
    _write_yaml(tmp_path, """
sage_memory:
  retrieval:
    rerank:
      top_k: 25
""")
    fresh_config.reload()
    monkeypatch.setenv("SAGE_RERANK_TOP_K", "30")
    assert fresh_config.get("retrieval.rerank.top_k") == 30


def test_layer1_override_returns_override_unchanged(fresh_config):
    """Layer 1 (override kwarg) wins over cascade."""
    assert fresh_config.get("retrieval.rerank.top_k", override=99) == 99


def test_layer1_override_none_returns_none(fresh_config):
    """`override=None` is treated as an explicit None — does NOT
    fall through to cascade. The `_UNSET` sentinel separates
    'not provided' from 'explicitly None'."""
    assert fresh_config.get("retrieval.rerank.top_k", override=None) is None


def test_missing_yaml_uses_builtins_silently(fresh_config):
    """No yaml file at all → built-ins resolve; no error."""
    # fresh_config fixture points at tmp_path with no .sage/config.yaml
    assert fresh_config.get("retrieval.rerank.top_k") == 15


def test_malformed_yaml_raises_config_error_with_path(
    fresh_config, tmp_path,
):
    """Bad yaml → ConfigError whose message contains the file path."""
    _write_yaml(tmp_path, "this: is: not: valid: yaml: at: all\n  :")
    fresh_config.reload()
    with pytest.raises(fresh_config.ConfigError) as excinfo:
        fresh_config.get("retrieval.rerank.top_k")
    assert "config.yaml" in str(excinfo.value)


def test_unknown_key_raises_config_error(fresh_config):
    """get() of a key not in built-ins/yaml/env → ConfigError."""
    with pytest.raises(fresh_config.ConfigError):
        fresh_config.get("nonsense.path.here")


def test_type_coercion_bool_from_env(fresh_config, monkeypatch):
    """Env strings 'true'/'True'/'1' → True; 'false'/'False'/'0' → False
    when the yaml type at the key path is bool."""
    # `retrieval.expand.enabled` has yaml type bool (or 'auto' str —
    # for this test we use a yaml-typed bool path).
    for true_val in ("true", "True", "1"):
        monkeypatch.setenv("SAGE_MEMORY_DEDUP_INTERVAL_ENABLED", true_val)
        fresh_config.reload()
        # built-in for this hypothetical key is False; env override
        assert fresh_config.get("dedup.interval_enabled") is True
    for false_val in ("false", "False", "0"):
        monkeypatch.setenv("SAGE_MEMORY_DEDUP_INTERVAL_ENABLED", false_val)
        fresh_config.reload()
        assert fresh_config.get("dedup.interval_enabled") is False


def test_type_coercion_list_from_csv_env(fresh_config, monkeypatch):
    """Env CSV string + yaml list[float] type → list[float]."""
    monkeypatch.setenv("SAGE_RERANK_BLEND_CURVE", "0.9,0.5,0.1")
    assert fresh_config.get("retrieval.rerank.blend_curve") == [0.9, 0.5, 0.1]
