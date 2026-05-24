"""M4.2 — cli_auth.py: 5 subcommands + negatives.

8 tests per plan M4.2: 5 happy + login no-provider + import
non-existent CLI + status with no creds (empty list).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from sage_memory.auth import oauth as auth_oauth
from sage_memory.auth import storage as auth_storage
from sage_memory.auth.storage import ProviderCredentials


def _fake_oauth_login(monkeypatch, provider: str = "openai"):
    """Stub out run_oauth_flow so login tests don't open a browser."""
    def _fake(provider_name, **kwargs):
        return ProviderCredentials(
            provider=provider_name,
            access_token=f"sk-{provider_name}-test",
            refresh_token=f"rt-{provider_name}-test",
            expires_at=time.time() + 3600,
            imported_from="oauth-login",
        )
    monkeypatch.setattr(auth_oauth, "run_oauth_flow", _fake)


def test_cli_login_happy(tmp_path, monkeypatch, capsys):
    from sage_memory.cli_auth import run_auth
    _fake_oauth_login(monkeypatch)

    auth_path = tmp_path / "auth.json"
    rc = run_auth([
        "login", "--provider", "openai",
        "--auth-path", str(auth_path),
    ])
    out = capsys.readouterr().out
    assert rc == 0, f"login failed: out={out!r}"
    assert "logged in" in out

    # Credentials persisted.
    store = auth_storage.load(auth_path)
    assert "openai" in store.providers
    assert store.providers["openai"].access_token.startswith("sk-openai")


def test_cli_import_happy(tmp_path, monkeypatch, capsys):
    from sage_memory.cli_auth import run_auth

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".codex").mkdir()
    (fake_home / ".codex" / "auth.json").write_text(json.dumps({
        "access_token": "sk-codex-test",
        "refresh_token": "rt-codex-test",
        "expires_at": time.time() + 3600,
    }))
    monkeypatch.setenv("HOME", str(fake_home))

    auth_path = tmp_path / "auth.json"
    rc = run_auth([
        "import", "--provider", "openai",
        "--auth-path", str(auth_path),
    ])
    out = capsys.readouterr().out
    assert rc == 0, f"import failed: out={out!r}"
    assert "imported" in out


def test_cli_list_happy(tmp_path, capsys):
    from sage_memory.cli_auth import run_auth

    auth_path = tmp_path / "auth.json"
    auth_storage.set_provider(
        ProviderCredentials(
            provider="openai", access_token="sk-1",
            imported_from="oauth-login",
        ),
        auth_path,
    )

    rc = run_auth(["list", "--auth-path", str(auth_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "openai" in out
    assert "oauth-login" in out
    # Token values must NOT appear in list output.
    assert "sk-1" not in out, "list must not leak token values"


def test_cli_remove_happy(tmp_path, capsys):
    from sage_memory.cli_auth import run_auth

    auth_path = tmp_path / "auth.json"
    auth_storage.set_provider(
        ProviderCredentials(provider="openai", access_token="x"),
        auth_path,
    )
    rc = run_auth([
        "remove", "--provider", "openai",
        "--auth-path", str(auth_path),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "removed" in out
    reloaded = auth_storage.load(auth_path)
    assert "openai" not in reloaded.providers


def test_cli_status_happy(tmp_path, capsys):
    from sage_memory.cli_auth import run_auth

    auth_path = tmp_path / "auth.json"
    auth_storage.set_provider(
        ProviderCredentials(
            provider="openai", access_token="x",
            expires_at=time.time() + 7200,
        ),
        auth_path,
    )
    rc = run_auth(["status", "--auth-path", str(auth_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "openai" in out
    assert "active" in out


def test_cli_login_without_provider_errors(tmp_path, capsys):
    from sage_memory.cli_auth import run_auth

    rc = run_auth([
        "login",
        "--auth-path", str(tmp_path / "auth.json"),
    ])
    err = capsys.readouterr().err
    assert rc == 2
    assert "--provider" in err


def test_cli_import_nonexistent_source_errors(tmp_path, monkeypatch, capsys):
    from sage_memory.cli_auth import run_auth

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    rc = run_auth([
        "import", "--provider", "openai",
        "--auth-path", str(tmp_path / "auth.json"),
    ])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not found" in err.lower() or "no such" in err.lower()


def test_cli_status_with_no_credentials_reports_empty(tmp_path, capsys):
    from sage_memory.cli_auth import run_auth

    rc = run_auth([
        "status",
        "--auth-path", str(tmp_path / "auth.json"),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no providers" in out.lower()
