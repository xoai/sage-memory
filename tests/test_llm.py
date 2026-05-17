"""T1 — sage_memory.llm tests.

Covers spec A1: provider cascade + retry. Mocks httpx (no live API).
Mirrors M1's HostedEmbedder retry test pattern (test_embedder_cascade.py
lines 159-199): monkeypatch httpx.post + time.sleep, count calls,
assert behavior.

Plan T1 done-when reference: 11 test gates.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest


# ─── Helpers ──────────────────────────────────────────────────────


def _scrub_keys(monkeypatch):
    for var in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                "SAGE_LLM_MODEL"]:
        monkeypatch.delenv(var, raising=False)


def _mock_response(status_code, json_body=None, headers=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    if 200 <= status_code < 300:
        resp.json.return_value = json_body or {}
        resp.raise_for_status.return_value = None
    else:
        # Build a mocked HTTPStatusError that mirrors httpx's behavior.
        err = httpx.HTTPStatusError(
            f"{status_code}", request=MagicMock(), response=resp,
        )
        resp.raise_for_status.side_effect = err
    return resp


def _anthropic_success_body(text):
    """Anthropic Messages API response shape."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": "claude-haiku-4-5",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }


def _openai_success_body(text):
    """OpenAI Chat Completions response shape."""
    return {
        "id": "cmpl-test",
        "object": "chat.completion",
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20,
                  "total_tokens": 30},
    }


# ─── 1. Lazy import (module imports with no keys) ─────────────────


def test_llm_lazy_import_with_no_keys(monkeypatch):
    """The module imports without raising even when no LLM keys are set."""
    _scrub_keys(monkeypatch)
    import importlib
    import sage_memory.llm as llm_mod
    importlib.reload(llm_mod)
    # Module loaded; no exception. is_configured returns False.
    assert llm_mod.is_configured() is False


# ─── 2. Provider routing ──────────────────────────────────────────


def test_llm_anthropic_path_returns_json(monkeypatch):
    """With Anthropic key, uses Anthropic endpoint and parses Messages JSON."""
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    from sage_memory import llm

    captured = {}

    def fake_post(url, *args, **kwargs):
        captured["url"] = url
        captured["payload"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        # LLM returns valid JSON in the text content
        return _mock_response(
            200,
            _anthropic_success_body('{"entities": [], "relations": []}'),
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    result = llm.extract_entities("Some memory content.")
    assert "anthropic.com" in captured["url"]
    assert result == {"entities": [], "relations": []}
    # Auth header for Anthropic is x-api-key, not Bearer
    assert captured["headers"]["x-api-key"] == "sk-ant-test"


def test_llm_openai_fallback_when_no_anthropic_key(monkeypatch):
    """With only OpenAI key, uses OpenAI Chat Completions endpoint."""
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")

    from sage_memory import llm

    captured = {}

    def fake_post(url, *args, **kwargs):
        captured["url"] = url
        captured["payload"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return _mock_response(
            200,
            _openai_success_body('{"entities": [], "relations": []}'),
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    result = llm.extract_entities("Some memory content.")
    assert "openai.com" in captured["url"]
    assert result == {"entities": [], "relations": []}
    assert captured["headers"]["Authorization"] == "Bearer sk-openai-test"
    # OpenAI uses response_format json_object per plan T1
    assert captured["payload"].get("response_format") == {
        "type": "json_object"
    }


# ─── 3. Retry policy ──────────────────────────────────────────────


def test_llm_retry_3_attempts_on_500(monkeypatch):
    """500, 500, 200 → 3 calls, result returned."""
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    from sage_memory import llm

    call_count = {"n": 0}

    def fake_post(url, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            return _mock_response(500)
        return _mock_response(
            200,
            _anthropic_success_body('{"entities": [], "relations": []}'),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("time.sleep", lambda _: None)

    result = llm.extract_entities("Some memory content.")
    assert call_count["n"] == 3
    assert result == {"entities": [], "relations": []}


def test_llm_terminal_failure_raises_after_3(monkeypatch):
    """500 × 3 → raises after retries exhausted."""
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    from sage_memory import llm

    call_count = {"n": 0}

    def always_500(url, *args, **kwargs):
        call_count["n"] += 1
        return _mock_response(500)

    monkeypatch.setattr(httpx, "post", always_500)
    monkeypatch.setattr("time.sleep", lambda _: None)

    with pytest.raises(httpx.HTTPStatusError):
        llm.extract_entities("Some memory content.")
    assert call_count["n"] == 3


def test_llm_4xx_no_retry(monkeypatch):
    """400 → 1 call, raises immediately (no retry)."""
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    from sage_memory import llm

    call_count = {"n": 0}

    def fake_post(url, *args, **kwargs):
        call_count["n"] += 1
        return _mock_response(400)

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("time.sleep", lambda _: None)

    with pytest.raises(httpx.HTTPStatusError):
        llm.extract_entities("Some memory content.")
    assert call_count["n"] == 1


def test_llm_429_retried_with_backoff(monkeypatch):
    """429 (no Retry-After) → exponential backoff applied."""
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    from sage_memory import llm

    call_count = {"n": 0}
    sleeps = []

    def fake_post(url, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 2:
            return _mock_response(429)
        return _mock_response(
            200,
            _anthropic_success_body('{"entities": [], "relations": []}'),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    result = llm.extract_entities("Some memory content.")
    assert call_count["n"] == 2
    # Exponential backoff: first sleep is 2 ** 0 = 1
    assert sleeps == [1]
    assert result == {"entities": [], "relations": []}


def test_llm_429_honors_retry_after(monkeypatch):
    """429 with Retry-After: 2 → sleeps for 2 seconds."""
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    from sage_memory import llm

    call_count = {"n": 0}
    sleeps = []

    def fake_post(url, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 2:
            return _mock_response(429, headers={"Retry-After": "2"})
        return _mock_response(
            200,
            _anthropic_success_body('{"entities": [], "relations": []}'),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    result = llm.extract_entities("Some memory content.")
    assert sleeps == [2]
    assert result == {"entities": [], "relations": []}


def test_llm_429_retry_after_capped_at_30s(monkeypatch):
    """429 with Retry-After: 120 → sleep capped at 30."""
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    from sage_memory import llm

    call_count = {"n": 0}
    sleeps = []

    def fake_post(url, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 2:
            return _mock_response(429, headers={"Retry-After": "120"})
        return _mock_response(
            200,
            _anthropic_success_body('{"entities": [], "relations": []}'),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    llm.extract_entities("Some memory content.")
    assert sleeps == [30]


# ─── 4. Code-fence stripping (regression for real-API behavior) ───


def test_llm_strips_markdown_code_fence(monkeypatch):
    """Anthropic Haiku wraps JSON in ```json ... ``` even when told
    'JSON only'. Regression for M3a bug surfaced by the first real
    E2E LLM run (2026-05-17): llm.py must tolerate the fence and
    parse the JSON inside."""
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    from sage_memory import llm

    fenced_response = (
        '```json\n'
        '{"entities": [{"name": "Bob", "type": "PERSON", '
        '"surface_form": "Bob"}], "relations": []}\n'
        '```'
    )

    def fake_post(url, *args, **kwargs):
        return _mock_response(
            200, _anthropic_success_body(fenced_response),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    result = llm.extract_entities("Bob did something.")
    assert result == {
        "entities": [
            {"name": "Bob", "type": "PERSON", "surface_form": "Bob"}
        ],
        "relations": [],
    }


def test_llm_strips_bare_triple_backtick(monkeypatch):
    """Some models use bare ``` (no language tag). Tolerate that too."""
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    from sage_memory import llm

    fenced = '```\n{"entities": [], "relations": []}\n```'

    def fake_post(url, *args, **kwargs):
        return _mock_response(200, _anthropic_success_body(fenced))

    monkeypatch.setattr(httpx, "post", fake_post)
    result = llm.extract_entities("Some content.")
    assert result == {"entities": [], "relations": []}


def test_llm_no_fence_unchanged(monkeypatch):
    """Clean JSON (no fence) parses unchanged — strip is a no-op."""
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    from sage_memory import llm

    def fake_post(url, *args, **kwargs):
        return _mock_response(
            200, _anthropic_success_body(
                '{"entities": [], "relations": []}'
            ),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    result = llm.extract_entities("Some content.")
    assert result == {"entities": [], "relations": []}


def test_llm_fence_with_trailing_text(monkeypatch):
    """Trailing text AFTER the closing fence is discarded (regression
    for cumulative-review finding: previous _strip_code_fence didn't
    handle this — endswith('```') was False, returning JSON + garbage
    which then failed json.loads)."""
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    from sage_memory import llm

    fenced_with_trailing = (
        '```json\n'
        '{"entities": [], "relations": []}\n'
        '```\n'
        'Note: this is the result for your query.'
    )

    def fake_post(url, *args, **kwargs):
        return _mock_response(
            200, _anthropic_success_body(fenced_with_trailing),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    result = llm.extract_entities("Some content.")
    assert result == {"entities": [], "relations": []}


def test_llm_single_line_fence_no_newline(monkeypatch):
    """Single-line fence with no newline (e.g. ```{}```) — earlier
    impl returned the raw text including the leading ```, causing
    JSONDecodeError. Regression test."""
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    from sage_memory import llm

    single_line = '```{"entities": [], "relations": []}```'

    def fake_post(url, *args, **kwargs):
        return _mock_response(
            200, _anthropic_success_body(single_line),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    result = llm.extract_entities("Some content.")
    assert result == {"entities": [], "relations": []}


def test_llm_single_line_fence_with_lang_tag(monkeypatch):
    """Single-line fence WITH language tag: ```json{}```."""
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    from sage_memory import llm

    single_line_lang = '```json{"entities": [], "relations": []}```'

    def fake_post(url, *args, **kwargs):
        return _mock_response(
            200, _anthropic_success_body(single_line_lang),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    result = llm.extract_entities("Some content.")
    assert result == {"entities": [], "relations": []}


def test_llm_fence_with_leading_whitespace(monkeypatch):
    """Leading whitespace before the fence — common when LLM emits
    a blank line first."""
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    from sage_memory import llm

    padded = '\n   ```json\n{"entities": [], "relations": []}\n```   \n'

    def fake_post(url, *args, **kwargs):
        return _mock_response(200, _anthropic_success_body(padded))

    monkeypatch.setattr(httpx, "post", fake_post)
    result = llm.extract_entities("Some content.")
    assert result == {"entities": [], "relations": []}


# ─── 5. Configuration & errors ────────────────────────────────────


def test_llm_no_key_raises_not_configured(monkeypatch):
    """No provider key set → raises LlmNotConfiguredError on first call."""
    _scrub_keys(monkeypatch)
    import importlib
    import sage_memory.llm as llm_mod
    importlib.reload(llm_mod)

    with pytest.raises(llm_mod.LlmNotConfiguredError):
        llm_mod.extract_entities("Some content.")


def test_llm_sage_llm_model_override(monkeypatch):
    """SAGE_LLM_MODEL=foo + Anthropic key → POST body uses 'foo' as model."""
    _scrub_keys(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("SAGE_LLM_MODEL", "claude-test-override")

    from sage_memory import llm

    captured = {}

    def fake_post(url, *args, **kwargs):
        captured["payload"] = kwargs.get("json")
        return _mock_response(
            200,
            _anthropic_success_body('{"entities": [], "relations": []}'),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    llm.extract_entities("Some content.")

    assert captured["payload"]["model"] == "claude-test-override"
