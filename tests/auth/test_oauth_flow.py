"""M4.1 — auth.oauth: OAuth flow via mock provider + local callback.

5 tests per plan M4.1: mock provider exchange, callback server
lifecycle, code exchange, error-callback path, state validation.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import urllib.parse
import urllib.request

import httpx
import pytest

from sage_memory.auth import oauth


def _spawn_caller(port: int, code: str = "TEST-CODE", state: str = "STATE-OK",
                  error: str | None = None) -> threading.Thread:
    """Simulate the user's browser by GET-ing the callback URL with
    the provider's response embedded in the query string."""
    def _hit():
        time.sleep(0.05)  # let the server.handle_request() start
        params = {"state": state}
        if error:
            params["error"] = error
        else:
            params["code"] = code
        url = f"http://127.0.0.1:{port}/callback?" + urllib.parse.urlencode(params)
        try:
            urllib.request.urlopen(url, timeout=5).read()
        except Exception:
            pass
    t = threading.Thread(target=_hit, daemon=True)
    t.start()
    return t


def test_callback_server_captures_code_and_state():
    port = oauth._free_port()
    t = _spawn_caller(port, code="abc", state="xyz")
    captured = oauth.run_callback_server(port, timeout_seconds=5)
    t.join(timeout=5)
    assert captured["code"] == "abc"
    assert captured["state"] == "xyz"
    assert captured["error"] == ""


def test_callback_server_captures_error_param():
    port = oauth._free_port()
    _spawn_caller(port, error="access_denied", state="xyz")
    captured = oauth.run_callback_server(port, timeout_seconds=5)
    assert captured["error"] == "access_denied"


def test_exchange_code_returns_credentials():
    """Mock httpx transport returns a canonical OAuth token response;
    exchange_code_for_token parses it into ProviderCredentials."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "access_token": "new-access-tok",
            "refresh_token": "new-refresh-tok",
            "expires_in": 3600,
        })

    transport = httpx.MockTransport(_handler)
    with httpx.Client(transport=transport) as client:
        creds = oauth.exchange_code_for_token(
            "openai", code="DUMMY", port=8000, client=client,
        )
    assert creds.access_token == "new-access-tok"
    assert creds.refresh_token == "new-refresh-tok"
    assert creds.expires_at > time.time()


def test_exchange_code_failure_raises_oauth_error():
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    transport = httpx.MockTransport(_handler)
    with httpx.Client(transport=transport) as client:
        with pytest.raises(oauth.OAuthError) as exc:
            oauth.exchange_code_for_token(
                "openai", code="BAD", port=8000, client=client,
            )
    assert "400" in str(exc.value)


def test_run_oauth_flow_rejects_state_mismatch(monkeypatch):
    """If the callback's state doesn't match the state we generated,
    abort the login (CSRF defense). Uses the full run_oauth_flow
    path with a mocked browser opener that fires a callback with
    the WRONG state."""
    captured_url: dict[str, str] = {}

    # Capture the authorize URL so the test can pull our generated
    # state from it (we need to KNOW the state to send a different one).
    def _fake_open(url: str) -> None:
        captured_url["url"] = url
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        # Pull port from redirect_uri so we know where to fire.
        redirect = query["redirect_uri"][0]
        port = int(urllib.parse.urlparse(redirect).port)
        # Fire callback with WRONG state.
        _spawn_caller(port, code="abc", state="WRONG-STATE")

    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"access_token": "x"}),
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(oauth.OAuthError) as exc:
            oauth.run_oauth_flow(
                "openai",
                open_browser=_fake_open,
                client=client,
                timeout_seconds=5,
            )
    assert "state" in str(exc.value).lower()
