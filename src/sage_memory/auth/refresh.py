"""Authorization-Bearer header injection + refresh-on-401.

Per ADR-010: when ``config.auth.worker_llm.<provider>: subscription``,
the worker's LLM call reads the access_token from
``~/.sage-memory/auth.json`` and injects ``Authorization: Bearer
<token>``. On 401 (token expired), attempts a refresh via the
refresh_token, persists the new tokens, retries the request once.

Refresh failure (refresh_token revoked, provider endpoint changed,
network down) surfaces a clear ``RefreshError`` so the operator
sees what to do next (``sage-memory auth login --provider ...``).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

import httpx

from . import providers as _providers
from . import storage as _storage
from .storage import ProviderCredentials


logger = logging.getLogger("sage_memory.auth.refresh")


class RefreshError(RuntimeError):
    """Token refresh failed; user must re-authenticate."""


def inject_bearer(
    headers: dict[str, str], creds: ProviderCredentials,
) -> dict[str, str]:
    """Return a new dict with ``Authorization: Bearer <token>`` added.

    Pure function — easier to compose with the LLM call path which
    already builds a headers dict per request.
    """
    out = dict(headers)
    out["Authorization"] = f"Bearer {creds.access_token}"
    return out


def is_expired(creds: ProviderCredentials, *, now: float | None = None) -> bool:
    """A small skew tolerance avoids racing the provider's clock."""
    now = now if now is not None else time.time()
    # 30s skew — refresh slightly early to avoid the racy "valid
    # at call construction, expired by the time it reaches the
    # provider" failure mode.
    return creds.expires_at <= (now + 30.0)


def refresh_token(
    creds: ProviderCredentials,
    *,
    client: httpx.Client | None = None,
    auth_path: Path | None = None,
) -> ProviderCredentials:
    """Use the refresh_token to obtain a new access_token.

    Persists the new credentials to ``auth_path`` (default:
    ``~/.sage-memory/auth.json``) on success.
    """
    if not creds.refresh_token:
        raise RefreshError(
            f"provider {creds.provider!r} has no refresh_token; "
            f"re-run `sage-memory auth login --provider {creds.provider}`"
        )
    cfg = _providers.get(creds.provider)
    own_client = False
    if client is None:
        client = httpx.Client(timeout=30.0)
        own_client = True
    try:
        resp = client.post(
            cfg.token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": creds.refresh_token,
                "client_id": "sage-memory",
            },
        )
    finally:
        if own_client:
            client.close()
    if resp.status_code >= 400:
        raise RefreshError(
            f"refresh failed ({resp.status_code}): {resp.text[:200]}. "
            f"Re-run `sage-memory auth login --provider "
            f"{creds.provider}`"
        )
    body = resp.json()
    access = body.get("access_token")
    if not access:
        raise RefreshError(
            f"refresh endpoint returned no access_token: {body!r}"
        )
    expires_in = float(body.get("expires_in", 3600))
    new_creds = ProviderCredentials(
        provider=creds.provider,
        type=creds.type,
        access_token=str(access),
        # Some providers rotate the refresh_token; honor that.
        refresh_token=str(body.get("refresh_token") or creds.refresh_token),
        expires_at=time.time() + expires_in,
        imported_from=creds.imported_from or "oauth-refresh",
    )
    _storage.set_provider(new_creds, auth_path)
    return new_creds


def request_with_refresh(
    method: str,
    url: str,
    creds: ProviderCredentials,
    *,
    client: httpx.Client | None = None,
    auth_path: Path | None = None,
    **kwargs,
) -> tuple[httpx.Response, ProviderCredentials]:
    """Send an HTTP request with Bearer auth; refresh-and-retry on 401.

    Returns ``(response, possibly_refreshed_creds)``. Used by the
    M4.3 ``llm.py`` integration as the canonical "make this LLM call
    on behalf of the worker" helper.
    """
    if is_expired(creds):
        creds = refresh_token(creds, client=client, auth_path=auth_path)

    # M4 review MAJOR-1 fix: save caller-supplied headers ONCE before
    # the retry. Pre-fix did kwargs.pop("headers", ...) twice — the
    # second pop returned the default `{}` because the first pop
    # consumed the entry, silently dropping any caller-supplied
    # trace / content-type / API-version headers on the refresh
    # retry path.
    original_headers = kwargs.pop("headers", {}) or {}
    headers = inject_bearer(original_headers, creds)
    own_client = False
    if client is None:
        client = httpx.Client(timeout=30.0)
        own_client = True
    try:
        resp = client.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 401:
            logger.info(
                "auth.refresh: 401 on %s — refreshing token", url,
            )
            creds = refresh_token(creds, client=client, auth_path=auth_path)
            headers = inject_bearer(original_headers, creds)
            resp = client.request(method, url, headers=headers, **kwargs)
    finally:
        if own_client:
            client.close()
    return resp, creds
