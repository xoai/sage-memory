"""`sage-memory install-skills` CLI subcommand.

Diverges from the other cli_*.py modules by using stdlib `argparse`
internally (the surface is too structured to hand-roll cleanly). The
top-level dispatch in `__init__.py` is unchanged — still calls
`run_install_skills(argv)` and uses the int return.

Adapters self-register into `_ADAPTERS` when their module is imported
in Tasks 4-7. Until that happens, requesting an agent surfaces a
clear "no adapter registered" error.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Callable, Sequence


# Adapter registry. Each adapter module (agent_claude_code.py etc.)
# inserts itself here at import time via `register_adapter()`. Keys
# are the public agent names; values are callables conforming to the
# Adapter protocol (see Task 4).
_ADAPTERS: dict[str, object] = {}

# Modules to attempt-import when resolving an agent. Order matches the
# public agent list. Modules absent from disk are silently skipped
# (which is how Task 3 can land cleanly before Tasks 4-7).
_ADAPTER_MODULES = {
    "claude-code": "sage_memory.install_skills.agent_claude_code",
    "cursor":      "sage_memory.install_skills.agent_cursor",
    "codex":       "sage_memory.install_skills.agent_codex",
    "gemini":      "sage_memory.install_skills.agent_gemini",
    "opencode":    "sage_memory.install_skills.agent_opencode",
}

AGENT_CHOICES = ["claude-code", "codex", "gemini", "cursor", "opencode", "all"]
SKILL_CHOICES = ["memory", "ontology", "self-learning"]


def register_adapter(name: str, adapter: object) -> None:
    """Adapters self-register at module-import time. The duck-typed
    contract every adapter implements:
        install_to(
            *, target: Path, skill_name: str, skill_dir: Path,
            version: str, dry_run: bool, yes: bool,
        ) -> list[FileResult]
    """
    _ADAPTERS[name] = adapter


def _try_import_adapters() -> None:
    """Best-effort import of all adapter modules. Modules that don't
    exist (because their Task hasn't landed yet) are silently skipped.
    """
    for agent, mod in _ADAPTER_MODULES.items():
        if agent in _ADAPTERS:
            continue
        try:
            importlib.import_module(mod)
        except ImportError:
            pass


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sage-memory install-skills",
        description=(
            "Install sage-memory's built-in skills into an AI coding agent. "
            "Targets: claude-code, codex, gemini, cursor, opencode (or 'all')."
        ),
        # We control all exits ourselves — keeps the int-return contract clean.
        exit_on_error=False,
    )
    p.add_argument(
        "agents", nargs="*", choices=AGENT_CHOICES,
        help="agent(s) to install for; use 'all' to install for every supported agent",
    )
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--project", action="store_true",
                       help="install to the current working directory")
    scope.add_argument("--global", action="store_true", dest="global_",
                       help="install to the user's home-level config")
    p.add_argument("--skill", action="append", default=None, choices=SKILL_CHOICES,
                   help="install only this skill (repeatable; default: all three)")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would change, write nothing")
    p.add_argument("-y", "--yes", action="store_true",
                   help="auto-overwrite on conflicts (required for non-TTY use)")
    return p


def _print_help_to_stdout() -> int:
    _build_parser().print_help(sys.stdout)
    return 0


def _print_err(msg: str) -> None:
    print(f"sage-memory install-skills: {msg}", file=sys.stderr)


def _resolve_targets(
    agents: list[str], project_scope: bool, global_scope: bool, cwd: Path,
) -> list[tuple[str, Path]]:
    """Resolve (agent, target_path) pairs in dispatch order.

    Codex and OpenCode share `./AGENTS.md` at project scope (and
    different files at global scope). Both adapters run regardless —
    the second pass on a shared target reports `UNCHANGED` via the
    marker-block body-equality check, so there's no work duplicated.
    Keeping both agents in the list makes the contract explicit:
    every requested agent gets an install (or an UNCHANGED report),
    not a silent drop.
    """
    from sage_memory.install_skills import paths
    pairs: list[tuple[str, Path]] = []
    for agent in agents:
        if global_scope:
            target = paths.global_target(agent)
        else:
            target = paths.project_target(agent, cwd)
        pairs.append((agent, target))
    return pairs


def run_install_skills(argv: Sequence[str]) -> int:
    # No args (or just --help / -h) → print help, exit 0.
    if not argv or argv[0] in ("-h", "--help"):
        return _print_help_to_stdout()

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except argparse.ArgumentError as e:
        _print_err(str(e))
        return 1
    except SystemExit as e:
        # argparse may still call sys.exit for some failure modes; the
        # error message has already been printed.
        return int(e.code or 0)

    if not args.agents:
        # `install-skills --project` (or --global) with no agent.
        _print_err("at least one agent is required (or 'all')")
        return 1

    if not args.project and not args.global_:
        _print_err("exactly one of --project or --global is required")
        return 1

    # Expand 'all' to the five concrete agents (preserving order).
    if "all" in args.agents:
        agents = list(_ADAPTER_MODULES.keys())
    else:
        # Deduplicate while preserving order.
        seen: set[str] = set()
        agents = []
        for a in args.agents:
            if a not in seen:
                seen.add(a)
                agents.append(a)

    skills = args.skill or SKILL_CHOICES

    cwd = Path.cwd()
    if args.project:
        from sage_memory.install_skills import paths
        warning = paths.warn_if_no_project_markers(cwd)
        if warning is not None:
            print(warning, file=sys.stderr)

    # Best-effort import of adapter modules.
    _try_import_adapters()

    targets = _resolve_targets(agents, args.project, args.global_, cwd)

    # Resolve the bundled-skills root (importlib.resources).
    from importlib.resources import files
    skills_root = files("sage_memory") / "skills"

    # Version string for the marker-block version line.
    from sage_memory import __version__ as _SAGE_VERSION  # noqa

    print(f"sage-memory install-skills v{_SAGE_VERSION}")
    scope_label = "project" if args.project else "global"
    aggregate_counts: dict[str, int] = {}
    exit_code = 0
    for agent, target in targets:
        adapter = _ADAPTERS.get(agent)
        if adapter is None:
            _print_err(f"no adapter registered for '{agent}' "
                       f"(this is a sage-memory bug — please report)")
            exit_code = 1
            continue
        print(f"  agent={agent} scope={scope_label} target={target}")
        for skill in skills:
            skill_dir = skills_root / skill
            try:
                results = adapter.install_to(
                    target=target,
                    skill_name=skill,
                    skill_dir=Path(str(skill_dir)),
                    version=_SAGE_VERSION,
                    dry_run=args.dry_run,
                    yes=args.yes,
                )
            except Exception as e:
                _print_err(f"{agent}: {skill}: {e}")
                exit_code = max(exit_code, 2)
                continue
            for r in results:
                aggregate_counts[r.status.value] = (
                    aggregate_counts.get(r.status.value, 0) + 1
                )
                print(f"    {r.status.value:>14}: {r.path}")

    if aggregate_counts:
        parts = ", ".join(
            f"{count} {label}" for label, count in sorted(aggregate_counts.items())
        )
        print(f"\n{parts}.")
    return exit_code
