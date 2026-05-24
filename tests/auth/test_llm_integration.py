"""M4.3 — llm.py subscription-auth integration.

4 tests per plan M4.3:
  1. config=env (no config file) → env-var path
  2. config=subscription + creds → subscription path
  3. config=subscription + no creds → falls back to env
  4. refresh fires on 401 from worker
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from sage_memory import llm as llm_mod
from sage_memory.auth import refresh as auth_refresh
from sage_memory.auth import storage as auth_storage
from sage_memory.auth.storage import ProviderCredentials


def _scrub_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_no_config_file_uses_env_var_path(tmp_path, monkeypatch):
    """No ~/.sage-memory/config.yaml → worker_llm mode is "env" for
    every provider. is_configured("openai") follows OPENAI_API_KEY."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _scrub_env(monkeypatch)

    assert llm_mod._worker_auth_mode("openai") == "env"
    assert llm_mod.is_configured("openai") is False

    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert llm_mod.is_configured("openai") is True


def test_subscription_mode_with_creds_uses_subscription_path(
    tmp_path, monkeypatch,
):
    """config opts in + auth.json has creds → is_configured returns
    True even with NO env var set."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _scrub_env(monkeypatch)

    home = tmp_path / "home"
    home.mkdir()
    (home / ".sage-memory").mkdir()
    (home / ".sage-memory" / "config.yaml").write_text(
        "auth:\n  worker_llm:\n    openai: subscription\n"
    )

    auth_path = home / ".sage-memory" / "auth.json"
    auth_storage.set_provider(
        ProviderCredentials(
            provider="openai", access_token="sk-subscription-tok",
            refresh_token="rt-1",
            expires_at=time.time() + 3600,
        ),
        auth_path,
    )

    assert llm_mod._worker_auth_mode("openai") == "subscription"
    assert llm_mod.is_configured("openai") is True


def test_subscription_mode_without_creds_falls_back_to_env(
    tmp_path, monkeypatch,
):
    """Config opts in but auth.json absent → fall back to env var so
    a half-configured worker isn't dead-in-the-water."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _scrub_env(monkeypatch)

    home = tmp_path / "home"
    home.mkdir()
    (home / ".sage-memory").mkdir()
    (home / ".sage-memory" / "config.yaml").write_text(
        "auth:\n  worker_llm:\n    openai: subscription\n"
    )

    # No auth.json present; env var also absent.
    assert llm_mod.is_configured("openai") is False

    # Add env var: config still says subscription, but no creds → falls
    # back to env. is_configured returns True.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fallback-env")
    assert llm_mod.is_configured("openai") is True


def test_refresh_path_fires_on_401_for_worker_request(tmp_path, monkeypatch):
    """End-to-end: when the worker's LLM call gets 401, the refresh
    helper fires (token rotated; subsequent call uses the new token).
    Tests the auth.refresh layer in isolation rather than wiring it
    into _call_llm — that's a larger refactor deferred per plan."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _scrub_env(monkeypatch)

    auth_path = tmp_path / "auth.json"
    initial = ProviderCredentials(
        provider="openai", access_token="old-tok",
        refresh_token="rt-1",
        expires_at=time.time() + 3600,
    )
    auth_storage.set_provider(initial, auth_path)

    request_log: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "oauth/token" in url:
            request_log.append("refresh")
            return httpx.Response(200, json={
                "access_token": "new-tok",
                "refresh_token": "rt-1",
                "expires_in": 3600,
            })
        auth = request.headers.get("Authorization", "")
        if auth == "Bearer old-tok":
            request_log.append("api-401")
            return httpx.Response(401, text="expired")
        request_log.append("api-200")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(_handler)
    with httpx.Client(transport=transport) as client:
        resp, new_creds = auth_refresh.request_with_refresh(
            "POST", "https://api.openai.com/v1/chat", initial,
            client=client, auth_path=auth_path,
        )

    assert resp.status_code == 200
    assert new_creds.access_token == "new-tok"
    assert request_log == ["api-401", "refresh", "api-200"]
