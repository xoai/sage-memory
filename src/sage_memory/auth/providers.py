"""Provider-specific OAuth + API endpoint configuration.

Per ADR-010: mirrors sage-wiki's ``internal/auth/providers.go``
shape. Each provider declares the OAuth authorize/token endpoints,
the API base URL (where the Bearer token is sent), the scopes
required, and the CLI-tool cred-file format we can import from.

OAuth endpoints are correct as of the cycle date (2026-05-24) but
may change at the provider's discretion. Refresh-time failures
surface clear errors so the operator can re-run ``auth login``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderConfig:
    """OAuth + API endpoints for one subscription provider."""

    name: str
    authorize_url: str
    token_url: str
    api_base: str
    scopes: tuple[str, ...] = ()
    # Path under ``~`` where the official CLI tool stores creds we
    # can import via ``sage-memory auth import``. Empty string =
    # no known importer.
    cli_cred_path: str = ""


_PROVIDERS: dict[str, ProviderConfig] = {
    "openai": ProviderConfig(
        name="openai",
        authorize_url="https://auth.openai.com/oauth/authorize",
        token_url="https://auth.openai.com/oauth/token",
        api_base="https://api.openai.com/v1",
        scopes=("read", "write"),
        cli_cred_path=".codex/auth.json",
    ),
    "claude": ProviderConfig(
        name="claude",
        authorize_url="https://auth.anthropic.com/oauth/authorize",
        token_url="https://auth.anthropic.com/oauth/token",
        api_base="https://api.anthropic.com/v1",
        scopes=("read", "write"),
        cli_cred_path=".claude/auth.json",
    ),
}


def get(name: str) -> ProviderConfig:
    """Lookup a provider by name. Raises ValueError on unknown name."""
    if name not in _PROVIDERS:
        raise ValueError(
            f"unknown provider {name!r}; supported: {sorted(_PROVIDERS)}"
        )
    return _PROVIDERS[name]


def list_supported() -> list[str]:
    """Names of all supported providers."""
    return sorted(_PROVIDERS)
