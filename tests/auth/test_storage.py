"""M4.1 — auth.storage: load/save with 0600 enforcement.

4 tests per plan M4.1: 0600 on save, round-trip, concurrent
read-safety, symlink rejection.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest

from sage_memory.auth import storage as auth_storage
from sage_memory.auth.storage import AuthStore, ProviderCredentials


def test_save_enforces_0600_permissions(tmp_path):
    """The auth file MUST have mode 0600 after save — not 0644, not
    0660. Any other reader on the host can read tokens otherwise."""
    path = tmp_path / "auth.json"
    store = AuthStore(providers={
        "openai": ProviderCredentials(
            provider="openai",
            access_token="sk-test",
            refresh_token="rt-test",
            expires_at=9999999999.0,
        ),
    })
    auth_storage.save(store, path)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, (
        f"auth file must be 0600 after save; got {oct(mode)}"
    )


def test_save_load_round_trip_preserves_typed_credentials(tmp_path):
    path = tmp_path / "auth.json"
    store = AuthStore(providers={
        "openai": ProviderCredentials(
            provider="openai", access_token="sk-1",
            refresh_token="rt-1", expires_at=12345.0,
            imported_from="oauth-login",
        ),
        "claude": ProviderCredentials(
            provider="claude", access_token="sk-2",
            refresh_token="rt-2", expires_at=67890.0,
            imported_from="claude-cli",
        ),
    })
    auth_storage.save(store, path)
    reloaded = auth_storage.load(path)
    assert set(reloaded.providers) == {"openai", "claude"}
    assert reloaded.providers["openai"].access_token == "sk-1"
    assert reloaded.providers["openai"].refresh_token == "rt-1"
    assert reloaded.providers["claude"].imported_from == "claude-cli"


def test_concurrent_saves_do_not_corrupt(tmp_path):
    """Multiple threads invoking save() concurrently must leave the
    file valid (atomic mkstemp+os.replace under the hood)."""
    path = tmp_path / "auth.json"
    auth_storage.save(AuthStore(), path)

    errors: list[Exception] = []

    def _worker(idx: int) -> None:
        try:
            store = auth_storage.load(path)
            store.providers[f"p{idx}"] = ProviderCredentials(
                provider=f"p{idx}", access_token=f"tok-{idx}",
            )
            auth_storage.save(store, path)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # File must still parse cleanly — no partial writes.
    data = json.loads(path.read_text())
    assert isinstance(data.get("providers"), dict)
    assert errors == [], f"workers raised: {errors!r}"


def test_save_refuses_symlink_target(tmp_path):
    """Symlink-substitution attack: an attacker pre-creates the auth
    file as a symlink to /tmp/captured. save() must refuse rather
    than write the user's tokens into the attacker's path."""
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"providers": {}}))
    target = tmp_path / "auth.json"
    target.symlink_to(real)

    store = AuthStore(providers={
        "openai": ProviderCredentials(
            provider="openai", access_token="sk-symlink",
        ),
    })
    with pytest.raises(auth_storage.AuthStorageError) as exc:
        auth_storage.save(store, target)
    assert "symlink" in str(exc.value).lower()
