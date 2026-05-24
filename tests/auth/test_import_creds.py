"""M4.1 — auth.import_creds: read CLI tool creds, convert, store.

3 tests per plan M4.1: codex import, claude import, malformed → error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sage_memory.auth import import_creds, storage as auth_storage


def _write_cli_creds(home: Path, rel_path: str, payload: dict) -> Path:
    src = home / rel_path
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(json.dumps(payload))
    return src


def test_import_codex_cli_credentials(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _write_cli_creds(home, ".codex/auth.json", {
        "access_token": "sk-codex-stored",
        "refresh_token": "rt-codex-stored",
        "expires_at": 9999999999,
    })
    auth_path = tmp_path / "auth.json"
    creds = import_creds.import_provider(
        "openai", home=home, auth_path=auth_path,
    )
    assert creds.access_token == "sk-codex-stored"
    assert creds.imported_from == "openai-cli"
    # And the credentials persisted to our own auth file.
    reloaded = auth_storage.load(auth_path)
    assert "openai" in reloaded.providers


def test_import_claude_cli_credentials(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _write_cli_creds(home, ".claude/auth.json", {
        "accessToken": "sk-claude-stored",  # camelCase variant
        "refreshToken": "rt-claude-stored",
    })
    auth_path = tmp_path / "auth.json"
    creds = import_creds.import_provider(
        "claude", home=home, auth_path=auth_path,
    )
    assert creds.access_token == "sk-claude-stored"
    assert creds.refresh_token == "rt-claude-stored"
    assert creds.imported_from == "claude-cli"


def test_import_malformed_credentials_raises_clear_error(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    # JSON object but missing the access_token field.
    _write_cli_creds(home, ".codex/auth.json", {"unrelated": "value"})

    with pytest.raises(import_creds.ImportCredsError) as exc:
        import_creds.import_provider("openai", home=home)
    msg = str(exc.value)
    assert "access_token" in msg
