"""M4.1 — auth.refresh: Bearer injection + refresh-on-401.

4 tests per plan M4.1: bearer injection, 401→refresh→retry, refresh
failure surfaces clearly, expired-token skips the wasted request.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from sage_memory.auth import refresh, storage as auth_storage
from sage_memory.auth.storage import AuthStore, ProviderCredentials


def test_inject_bearer_adds_authorization_header():
    creds = ProviderCredentials(
        provider="openai", access_token="sk-test-bearer",
    )
    headers = refresh.inject_bearer({"Content-Type": "application/json"}, creds)
    assert headers["Authorization"] == "Bearer sk-test-bearer"
    assert headers["Content-Type"] == "application/json"


def test_401_triggers_refresh_and_retry(tmp_path):
    """Mock transport: first call returns 401, refresh-token call
    returns 200 with new tokens, retry returns 200. Final creds
    object reflects the refreshed access_token."""
    auth_path = tmp_path / "auth.json"
    initial = ProviderCredentials(
        provider="openai", access_token="old-tok",
        refresh_token="rt-1", expires_at=time.time() + 3600,
    )
    auth_storage.set_provider(initial, auth_path)

    call_log: list[tuple[str, str]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if "oauth/token" in str(request.url):
            call_log.append(("refresh", request.headers.get("Authorization", "")))
            return httpx.Response(200, json={
                "access_token": "new-tok",
                "refresh_token": "rt-1",
                "expires_in": 3600,
            })
        # API call
        auth = request.headers.get("Authorization", "")
        call_log.append(("api", auth))
        if auth == "Bearer old-tok":
            return httpx.Response(401, text="token expired")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(_handler)
    with httpx.Client(transport=transport) as client:
        resp, new_creds = refresh.request_with_refresh(
            "POST", "https://api.openai.com/v1/chat", initial,
            client=client, auth_path=auth_path,
        )
    assert resp.status_code == 200
    assert new_creds.access_token == "new-tok"
    # Sequence: api(old) → refresh → api(new)
    assert [c[0] for c in call_log] == ["api", "refresh", "api"]


def test_refresh_failure_raises_clear_error(tmp_path):
    auth_path = tmp_path / "auth.json"
    creds = ProviderCredentials(
        provider="openai", access_token="x",
        refresh_token="invalid-rt",
        expires_at=time.time() - 1,  # already expired
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="invalid_grant")

    transport = httpx.MockTransport(_handler)
    with httpx.Client(transport=transport) as client:
        with pytest.raises(refresh.RefreshError) as exc:
            refresh.refresh_token(
                creds, client=client, auth_path=auth_path,
            )
    msg = str(exc.value)
    assert "auth login" in msg.lower(), (
        f"refresh error must direct user to `auth login`; got {msg!r}"
    )


def test_user_headers_preserved_on_refresh_retry(tmp_path):
    """Regression for /review M4 MAJOR-1: caller-supplied headers
    (X-Trace-Id, anthropic-version, etc.) must survive the 401-refresh-
    retry path. Pre-fix did kwargs.pop("headers", ...) twice; the
    second pop returned the default `{}`, silently dropping the
    caller's headers from the retried request."""
    auth_path = tmp_path / "auth.json"
    initial = ProviderCredentials(
        provider="openai", access_token="old-tok",
        refresh_token="rt-1", expires_at=time.time() + 3600,
    )
    auth_storage.set_provider(initial, auth_path)

    api_call_headers: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if "oauth/token" in str(request.url):
            return httpx.Response(200, json={
                "access_token": "new-tok",
                "refresh_token": "rt-1",
                "expires_in": 3600,
            })
        api_call_headers.append(dict(request.headers))
        auth = request.headers.get("Authorization", "")
        if auth == "Bearer old-tok":
            return httpx.Response(401, text="expired")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(_handler)
    with httpx.Client(transport=transport) as client:
        resp, _ = refresh.request_with_refresh(
            "POST", "https://api.openai.com/v1/chat", initial,
            headers={"X-Trace-Id": "trace-42", "anthropic-version": "2024-01-01"},
            client=client, auth_path=auth_path,
        )

    assert resp.status_code == 200
    # Both the original 401 call AND the retry must carry the
    # caller's custom headers.
    assert len(api_call_headers) == 2, (
        f"expected 2 API calls (401 + retry); got {len(api_call_headers)}"
    )
    for h in api_call_headers:
        assert h.get("x-trace-id") == "trace-42", (
            f"X-Trace-Id lost; got headers={h!r}"
        )
        assert h.get("anthropic-version") == "2024-01-01", (
            f"anthropic-version lost; got headers={h!r}"
        )


def test_no_refresh_when_token_still_valid(tmp_path):
    """is_expired returns False for a token with plenty of headroom —
    request_with_refresh sends only the API call, no refresh."""
    auth_path = tmp_path / "auth.json"
    creds = ProviderCredentials(
        provider="openai", access_token="still-good",
        refresh_token="rt-1",
        expires_at=time.time() + 3600,
    )
    calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if "oauth/token" in str(request.url):
            calls.append("refresh")
        else:
            calls.append("api")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(_handler)
    with httpx.Client(transport=transport) as client:
        resp, _ = refresh.request_with_refresh(
            "GET", "https://api.openai.com/v1/models", creds,
            client=client, auth_path=auth_path,
        )
    assert resp.status_code == 200
    assert calls == ["api"], (
        f"valid token should skip refresh; got calls={calls!r}"
    )
