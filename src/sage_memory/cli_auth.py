"""M4.2 — `sage-memory auth` CLI subcommand tree.

Per ADR-010: 5 subcommands following the cli_dedup hand-rolled
pattern. Subscription auth is opt-in via
``~/.sage-memory/config.yaml``; these subcommands only manage the
``~/.sage-memory/auth.json`` credential store.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from . import auth as _auth_pkg
from .auth import (
    import_creds as _auth_import_creds,
    oauth as _auth_oauth,
    providers as _auth_providers,
    storage as _auth_storage,
)


logger = logging.getLogger("sage_memory.cli_auth")


_HELP_TEXT = """\
sage-memory auth — subscription auth for the worker (0.13.0+)

Usage:
  sage-memory auth login --provider {openai,claude}
      Run the OAuth flow in the browser; store tokens in
      ~/.sage-memory/auth.json (mode 0600).

  sage-memory auth import --provider {openai,claude}
      Import existing CLI tool credentials (codex for openai;
      claude for anthropic).

  sage-memory auth list
      Show configured providers (no token values).

  sage-memory auth remove --provider <name>
      Forget credentials for one provider.

  sage-memory auth status
      Show auth state per provider (expiry, age).

Common flags:
  --auth-path <path>     Override ~/.sage-memory/auth.json location.
"""


class _FlagError(ValueError):
    pass


def _print_error(message: str) -> None:
    print(f"sage-memory auth: {message}\n", file=sys.stderr)
    print(_HELP_TEXT, file=sys.stderr)


def run_auth(argv: list[str]) -> int:
    """Entry point dispatched from ``__init__.py:main()``."""
    if not argv or argv[0] in ("-h", "--help"):
        print(_HELP_TEXT)
        return 0 if argv else 2

    sub = argv[0]
    rest = argv[1:]

    dispatch = {
        "login": _run_login,
        "import": _run_import,
        "list": _run_list,
        "remove": _run_remove,
        "status": _run_status,
    }
    handler = dispatch.get(sub)
    if handler is None:
        _print_error(f"unknown subcommand: {sub}")
        return 2
    return handler(rest)


def _take_auth_path(argv: list[str]) -> tuple[list[str], Path | None]:
    """Pull ``--auth-path <path>`` out of argv if present."""
    out: list[str] = []
    explicit: Path | None = None
    i = 0
    while i < len(argv):
        if argv[i] == "--auth-path":
            if i + 1 >= len(argv):
                raise _FlagError("--auth-path requires a value")
            explicit = Path(argv[i + 1])
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out, explicit


def _take_provider(argv: list[str]) -> tuple[list[str], str | None]:
    """Pull ``--provider <name>`` out of argv if present."""
    out: list[str] = []
    explicit: str | None = None
    i = 0
    while i < len(argv):
        if argv[i] == "--provider":
            if i + 1 >= len(argv):
                raise _FlagError("--provider requires a value")
            explicit = argv[i + 1]
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out, explicit


# ─── login ────────────────────────────────────────────────────────


def _run_login(argv: list[str]) -> int:
    try:
        argv, auth_path = _take_auth_path(argv)
        argv, provider = _take_provider(argv)
    except _FlagError as e:
        _print_error(str(e))
        return 2
    if argv:
        _print_error(f"`login` takes no positional args: {argv}")
        return 2
    if not provider:
        _print_error("`login` requires --provider {openai,claude}")
        return 2
    if provider not in _auth_providers.list_supported():
        _print_error(
            f"--provider must be one of "
            f"{_auth_providers.list_supported()} (got {provider!r})"
        )
        return 2

    try:
        creds = _auth_oauth.run_oauth_flow(provider)
    except _auth_oauth.OAuthError as exc:
        _print_error(f"login failed: {exc}")
        return 2

    _auth_storage.set_provider(creds, auth_path)
    print(f"sage-memory auth: logged in to {provider!r}")
    return 0


# ─── import ───────────────────────────────────────────────────────


def _run_import(argv: list[str]) -> int:
    try:
        argv, auth_path = _take_auth_path(argv)
        argv, provider = _take_provider(argv)
    except _FlagError as e:
        _print_error(str(e))
        return 2
    if argv:
        _print_error(f"`import` takes no positional args: {argv}")
        return 2
    if not provider:
        _print_error("`import` requires --provider {openai,claude}")
        return 2
    if provider not in _auth_providers.list_supported():
        _print_error(
            f"--provider must be one of "
            f"{_auth_providers.list_supported()} (got {provider!r})"
        )
        return 2

    try:
        creds = _auth_import_creds.import_provider(
            provider, auth_path=auth_path,
        )
    except _auth_import_creds.ImportCredsError as exc:
        _print_error(str(exc))
        return 2

    print(
        f"sage-memory auth: imported {provider!r} credentials from "
        f"{creds.imported_from}"
    )
    return 0


# ─── list ─────────────────────────────────────────────────────────


def _run_list(argv: list[str]) -> int:
    try:
        argv, auth_path = _take_auth_path(argv)
    except _FlagError as e:
        _print_error(str(e))
        return 2
    if argv:
        _print_error(f"`list` takes no positional args: {argv}")
        return 2

    store = _auth_storage.load(auth_path)
    if not store.providers:
        print("sage-memory auth: no providers configured")
        return 0
    print(f"{'PROVIDER':<16} {'TYPE':<14} IMPORTED_FROM")
    for name, p in sorted(store.providers.items()):
        print(f"{name:<16} {p.type:<14} {p.imported_from or '(none)'}")
    return 0


# ─── remove ───────────────────────────────────────────────────────


def _run_remove(argv: list[str]) -> int:
    try:
        argv, auth_path = _take_auth_path(argv)
        argv, provider = _take_provider(argv)
    except _FlagError as e:
        _print_error(str(e))
        return 2
    if argv:
        _print_error(f"`remove` takes no positional args: {argv}")
        return 2
    if not provider:
        _print_error("`remove` requires --provider <name>")
        return 2

    removed = _auth_storage.remove_provider(provider, auth_path)
    if not removed:
        print(f"sage-memory auth: {provider!r} was not configured")
        return 0
    print(f"sage-memory auth: removed {provider!r}")
    return 0


# ─── status ───────────────────────────────────────────────────────


def _run_status(argv: list[str]) -> int:
    try:
        argv, auth_path = _take_auth_path(argv)
    except _FlagError as e:
        _print_error(str(e))
        return 2
    if argv:
        _print_error(f"`status` takes no positional args: {argv}")
        return 2

    store = _auth_storage.load(auth_path)
    if not store.providers:
        print("sage-memory auth: no providers configured")
        return 0
    now = time.time()
    print(f"{'PROVIDER':<16} {'EXPIRES_IN':<14} STATUS")
    for name, p in sorted(store.providers.items()):
        seconds = int(p.expires_at - now)
        if seconds <= 0:
            status_text = "expired"
            expiry_text = f"{-seconds}s ago"
        else:
            status_text = "active"
            if seconds < 3600:
                expiry_text = f"{seconds // 60}m"
            else:
                expiry_text = f"{seconds // 3600}h"
        print(f"{name:<16} {expiry_text:<14} {status_text}")
    return 0
