"""M4.1 — auth.providers: endpoint configs.

2 tests per plan M4.1: config completeness + URL/headers shape.
"""

from __future__ import annotations

import pytest

from sage_memory.auth import providers


def test_supported_providers_have_complete_config():
    """Each declared provider must carry: name, authorize_url,
    token_url, api_base, scopes (non-empty), cli_cred_path. Missing
    any field = silent failure mode (auth login can't construct the
    URL, import can't find the source file, etc.)."""
    for name in providers.list_supported():
        cfg = providers.get(name)
        assert cfg.name == name
        assert cfg.authorize_url.startswith("https://"), (
            f"{name}: authorize_url must be https; got {cfg.authorize_url!r}"
        )
        assert cfg.token_url.startswith("https://"), (
            f"{name}: token_url must be https; got {cfg.token_url!r}"
        )
        assert cfg.api_base.startswith("https://")
        assert cfg.scopes, f"{name}: scopes must be non-empty"
        assert cfg.cli_cred_path, (
            f"{name}: cli_cred_path required for import flow"
        )


def test_get_unknown_provider_errors_with_supported_list():
    """Discovery affordance: error names every supported provider
    so the user can correct without grepping the source."""
    with pytest.raises(ValueError) as exc:
        providers.get("not-a-real-provider")
    msg = str(exc.value)
    assert "not-a-real-provider" in msg
    for name in providers.list_supported():
        assert name in msg, (
            f"error message must list supported provider {name!r}"
        )
