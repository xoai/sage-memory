"""OAuth authorization-code flow with localhost callback server.

Per ADR-010: ports sage-wiki's ``internal/auth/oauth.go`` pattern.
The CLI invokes ``run_oauth_flow(provider)`` which:
  1. Starts a local HTTP server on a free port (``localhost:0``)
  2. Opens the provider's authorize URL in the user's browser
     with ``redirect_uri=http://localhost:<port>/callback``
  3. Waits up to ``timeout_seconds`` for the redirect carrying
     ``code=...``
  4. Exchanges the code for an access/refresh token via the
     provider's token endpoint
  5. Returns ``ProviderCredentials`` ready for ``storage.save``

OAuth state mitigation: the flow generates a random ``state`` value,
includes it in the authorize URL, and verifies the callback
returns the same value. Mismatch → reject.

Network calls are made via ``httpx`` so tests can mock with
``httpx.MockTransport``. The callback server uses Python's stdlib
``http.server`` to avoid pulling in a new dep for the narrow case.
"""

from __future__ import annotations

import http.server
import logging
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from typing import Any, Callable

import httpx

from . import providers as _providers
from .storage import ProviderCredentials


logger = logging.getLogger("sage_memory.auth.oauth")


# Generous default — the user may take a moment to authenticate in
# the browser. Tests override with small values.
_DEFAULT_TIMEOUT_SECONDS = 300.0


class OAuthError(RuntimeError):
    """Surfaces every failure mode of the OAuth flow."""


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _redirect_uri(port: int) -> str:
    return f"http://localhost:{port}/callback"


def build_authorize_url(
    provider: str,
    port: int,
    state: str,
    client_id: str = "sage-memory",
) -> str:
    """Construct the provider's authorize URL with our callback URI
    + state. Pure function — extracted so tests can inspect the URL
    shape without spinning up the server."""
    cfg = _providers.get(provider)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": _redirect_uri(port),
        "state": state,
        "scope": " ".join(cfg.scopes),
    }
    return cfg.authorize_url + "?" + urllib.parse.urlencode(params)


def exchange_code_for_token(
    provider: str,
    code: str,
    port: int,
    *,
    client: httpx.Client | None = None,
) -> ProviderCredentials:
    """Exchange an authorization code for access + refresh tokens.

    ``client`` is injectable so tests can pass an ``httpx.Client``
    backed by a ``MockTransport``.
    """
    cfg = _providers.get(provider)
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(port),
        "client_id": "sage-memory",
    }
    own_client = False
    if client is None:
        client = httpx.Client(timeout=30.0)
        own_client = True
    try:
        resp = client.post(cfg.token_url, data=payload)
    finally:
        if own_client:
            client.close()
    if resp.status_code >= 400:
        raise OAuthError(
            f"token exchange failed ({resp.status_code}): "
            f"{resp.text[:200]}"
        )
    body = resp.json()
    access = body.get("access_token")
    refresh = body.get("refresh_token", "")
    if not access:
        raise OAuthError(
            f"token endpoint returned no access_token: {body!r}"
        )
    expires_in = float(body.get("expires_in", 3600))
    return ProviderCredentials(
        provider=provider,
        type="subscription",
        access_token=str(access),
        refresh_token=str(refresh),
        expires_at=time.time() + expires_in,
        imported_from="oauth-login",
    )


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the OAuth callback's query string into a shared dict.

    The handler subclass instances are created per-request; the
    captured-args dict lives on the server (set by ``_run_callback_server``).
    """

    def do_GET(self):  # noqa: N802 — http.server interface
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        query = urllib.parse.parse_qs(parsed.query)
        self.server.captured = {  # type: ignore[attr-defined]
            "code": query.get("code", [""])[0],
            "state": query.get("state", [""])[0],
            "error": query.get("error", [""])[0],
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"sage-memory: authentication received. You may close this window."
        )

    def log_message(self, format, *args):  # noqa: A002 — http.server interface
        # Quiet by default; sage-memory has its own logger.
        return


def run_callback_server(
    port: int, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """Run a one-shot HTTP server until /callback fires (or timeout).

    Returns the captured query-string dict
    (``{"code": ..., "state": ..., "error": ...}``). Used by both
    ``run_oauth_flow`` and tests that exercise the callback path
    in isolation.
    """
    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.captured = None  # type: ignore[attr-defined]

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        # Force shutdown if the user never completed the flow.
        server.server_close()
        raise OAuthError(
            f"timed out after {timeout_seconds}s waiting for callback"
        )
    server.server_close()
    captured = server.captured  # type: ignore[attr-defined]
    if captured is None:
        raise OAuthError("callback server returned no payload")
    return captured


def run_oauth_flow(
    provider: str,
    *,
    open_browser: Callable[[str], Any] | None = None,
    client: httpx.Client | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> ProviderCredentials:
    """End-to-end OAuth login. ``open_browser`` is injectable for
    tests; defaults to ``webbrowser.open``."""
    port = _free_port()
    state = secrets.token_urlsafe(16)
    auth_url = build_authorize_url(provider, port, state)

    opener = open_browser if open_browser is not None else webbrowser.open
    opener(auth_url)

    captured = run_callback_server(port, timeout_seconds=timeout_seconds)
    if captured.get("error"):
        raise OAuthError(
            f"provider returned error in callback: {captured['error']}"
        )
    if captured.get("state") != state:
        raise OAuthError(
            "OAuth state mismatch — possible CSRF; aborting login"
        )
    code = captured.get("code")
    if not code:
        raise OAuthError("callback missing authorization code")
    return exchange_code_for_token(provider, code, port, client=client)
