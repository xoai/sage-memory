"""``~/.sage-memory/auth.json`` load/save with strict 0600 permissions.

Per ADR-010 §"Storage": credentials are user-private; the file mode
is part of the security contract. Anything looser is a security bug.

Symlink targets are rejected on save — preventing a malicious
symlink-substitution attack where an attacker pre-creates
``~/.sage-memory/auth.json`` as a symlink to a writable shared
location to capture tokens.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


logger = logging.getLogger("sage_memory.auth.storage")


_SAGE_DIR = ".sage-memory"
_AUTH_FILENAME = "auth.json"
_REQUIRED_MODE = 0o600


def default_auth_path() -> Path:
    """Canonical location: ``~/.sage-memory/auth.json``. Resolved at
    call time so monkeypatched ``HOME`` propagates in tests."""
    return Path.home() / _SAGE_DIR / _AUTH_FILENAME


@dataclass(frozen=True)
class ProviderCredentials:
    """One provider's stored credentials. ``type`` is always
    ``"subscription"`` for the OAuth-flow path; env-var providers
    aren't stored here."""

    provider: str
    type: str = "subscription"
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0
    imported_from: str = ""


@dataclass
class AuthStore:
    """In-memory representation of ``~/.sage-memory/auth.json``."""

    providers: dict[str, ProviderCredentials] = field(default_factory=dict)


class AuthStorageError(RuntimeError):
    """Surface for invariant violations (mode wrong, symlink found)."""


def load(path: Path | None = None) -> AuthStore:
    """Read the auth store. Returns an empty ``AuthStore`` if the file
    doesn't exist (auth is opt-in; missing file = no providers
    configured)."""
    target = Path(path) if path is not None else default_auth_path()
    if not target.exists():
        return AuthStore()
    if target.is_symlink():
        raise AuthStorageError(
            f"refusing to load auth credentials via symlink: {target}"
        )
    try:
        raw = json.loads(target.read_text())
    except json.JSONDecodeError as exc:
        raise AuthStorageError(
            f"auth file at {target} is not valid JSON: {exc}"
        ) from exc
    providers_raw = raw.get("providers") or {}
    if not isinstance(providers_raw, dict):
        raise AuthStorageError(
            f"auth file at {target}: 'providers' must be a mapping"
        )
    providers: dict[str, ProviderCredentials] = {}
    for name, entry in providers_raw.items():
        if not isinstance(entry, dict):
            continue
        providers[name] = ProviderCredentials(
            provider=name,
            type=str(entry.get("type", "subscription")),
            access_token=str(entry.get("access_token", "")),
            refresh_token=str(entry.get("refresh_token", "")),
            expires_at=float(entry.get("expires_at", 0.0)),
            imported_from=str(entry.get("imported_from", "")),
        )
    return AuthStore(providers=providers)


def save(store: AuthStore, path: Path | None = None) -> None:
    """Write the auth store with strict ``0600`` permissions.

    Refuses to write through symlinks (rejects symlink-substitution).
    Atomic via tempfile + ``os.replace`` so concurrent loaders don't
    see a partial file. The temp file is created with mode 0600 so
    no other user ever sees the plaintext tokens.
    """
    target = Path(path) if path is not None else default_auth_path()
    if target.exists() and target.is_symlink():
        raise AuthStorageError(
            f"refusing to write auth credentials through symlink: {target}"
        )
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    payload = {
        "providers": {
            name: {
                "type": p.type,
                "access_token": p.access_token,
                "refresh_token": p.refresh_token,
                "expires_at": p.expires_at,
                "imported_from": p.imported_from,
            }
            for name, p in store.providers.items()
        }
    }

    fd, tmp_str = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp = Path(tmp_str)
    try:
        # mkstemp creates with 0600 on POSIX, but make it explicit so
        # the contract is auditable regardless of platform defaults.
        os.chmod(tmp, _REQUIRED_MODE)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, target)
        # Re-assert mode after rename — some POSIX systems reset on
        # replace; the chmod is cheap and load-bearing for security.
        os.chmod(target, _REQUIRED_MODE)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def get_provider(
    provider: str, path: Path | None = None,
) -> ProviderCredentials | None:
    """Convenience: return one provider's credentials, or None."""
    store = load(path)
    return store.providers.get(provider)


def set_provider(
    creds: ProviderCredentials, path: Path | None = None,
) -> None:
    """Convenience: upsert one provider's credentials."""
    store = load(path)
    store.providers[creds.provider] = creds
    save(store, path)


def remove_provider(provider: str, path: Path | None = None) -> bool:
    """Convenience: remove a provider; returns True if it existed."""
    store = load(path)
    if provider not in store.providers:
        return False
    del store.providers[provider]
    save(store, path)
    return True
