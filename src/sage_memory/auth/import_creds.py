"""Import credentials from existing CLI tools' auth files.

Per ADR-010: lets teams reuse the OAuth grant they've already
performed via the official CLI tool (``codex`` for OpenAI,
``claude`` for Anthropic). Each tool stores creds in a known
location; this module reads them, validates the shape, and writes
to our ``~/.sage-memory/auth.json``.

We accept a relaxed input shape (different CLI tools store slightly
different JSON layouts) but write our canonical shape. Malformed
or missing source files raise ``ImportCredsError`` with the
specific issue.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from . import providers as _providers
from . import storage as _storage
from .storage import ProviderCredentials


logger = logging.getLogger("sage_memory.auth.import_creds")


class ImportCredsError(RuntimeError):
    """Surfaces every failure mode of the import path."""


def _source_path_for(provider: str, home: Path | None = None) -> Path:
    cfg = _providers.get(provider)
    if not cfg.cli_cred_path:
        raise ImportCredsError(
            f"provider {provider!r} has no known CLI tool to import from"
        )
    home = home if home is not None else Path.home()
    return home / cfg.cli_cred_path


def import_provider(
    provider: str,
    *,
    home: Path | None = None,
    auth_path: Path | None = None,
) -> ProviderCredentials:
    """Read the CLI tool's auth file, convert, write to our store.

    Returns the credentials we just stored.
    """
    src = _source_path_for(provider, home=home)
    if not src.exists():
        raise ImportCredsError(
            f"source credentials not found at {src}; "
            f"complete `{provider}` CLI login first"
        )
    try:
        raw: Any = json.loads(src.read_text())
    except json.JSONDecodeError as exc:
        raise ImportCredsError(
            f"source credentials at {src} are not valid JSON: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise ImportCredsError(
            f"source credentials at {src} must be a JSON object"
        )

    # CLI tools use varied field names; accept the common forms.
    access = (
        raw.get("access_token")
        or raw.get("accessToken")
        or raw.get("token")
        or ""
    )
    refresh = (
        raw.get("refresh_token")
        or raw.get("refreshToken")
        or ""
    )
    expires = (
        raw.get("expires_at")
        or raw.get("expiresAt")
        or 0
    )
    if not access:
        raise ImportCredsError(
            f"source credentials at {src} have no access_token field "
            f"(checked: access_token, accessToken, token)"
        )

    creds = ProviderCredentials(
        provider=provider,
        type="subscription",
        access_token=str(access),
        refresh_token=str(refresh),
        # Best-effort expiry. If absent, set 1h from now so the
        # refresh path kicks in on first 401.
        expires_at=float(expires) if expires else (time.time() + 3600),
        imported_from=f"{provider}-cli",
    )
    _storage.set_provider(creds, auth_path)
    return creds
