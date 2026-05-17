"""T4 — Ablation harness tests.

Covers A10 setup: the `--channels-disable` flag plumbing on
`bench_longmemeval.py`. Bench-run integration is out of scope here
(it's slow and lives outside CI); this just verifies the wiring.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


BENCH_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "evaluation/longmemeval/bench_longmemeval.py"
)


def test_bench_script_exists():
    assert BENCH_SCRIPT.exists(), (
        f"bench_longmemeval.py not found at {BENCH_SCRIPT}"
    )


def test_bench_script_has_channels_disable_flag():
    """argparse --help mentions --channels-disable."""
    result = subprocess.run(
        [sys.executable, str(BENCH_SCRIPT), "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0
    assert "--channels-disable" in result.stdout, (
        "bench_longmemeval.py --help must list --channels-disable "
        "for A10 ablation; got:\n" + result.stdout
    )


def test_bench_channels_disable_accepts_csv():
    """The flag accepts comma-separated channel names without crashing
    at argparse-time. Use --limit 0 + a non-existent data file path
    to exit fast after argparse but before bench actually runs."""
    result = subprocess.run(
        [
            sys.executable, str(BENCH_SCRIPT),
            "/nonexistent/file.json",
            "--limit", "1",
            "--mode", "bm25",
            "--channels-disable", "graph,vector",
        ],
        capture_output=True, text=True, timeout=15,
    )
    # We expect SOME failure (file not found), but NOT an argparse error.
    # argparse errors print to stderr with "argument --channels-disable"
    assert "argument --channels-disable" not in result.stderr, (
        "argparse rejected --channels-disable=graph,vector value; "
        "stderr:\n" + result.stderr
    )


def test_4way_runner_script_exists():
    """The 4-way ablation runner script is present."""
    runner = (
        Path(__file__).resolve().parent.parent
        / "evaluation/longmemeval/run_4way_ablation.sh"
    )
    assert runner.exists(), (
        "run_4way_ablation.sh missing — required by A10"
    )


# ─── M4 (T6) — expand/rerank ablation flags ───────────────────────


def test_bench_script_has_expand_disable_flag():
    """argparse --help mentions --expand-disable."""
    result = subprocess.run(
        [sys.executable, str(BENCH_SCRIPT), "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0
    assert "--expand-disable" in result.stdout, (
        "bench_longmemeval.py --help must list --expand-disable "
        "for M4 T6 ablation; got:\n" + result.stdout
    )


def test_bench_script_has_rerank_disable_flag():
    """argparse --help mentions --rerank-disable."""
    result = subprocess.run(
        [sys.executable, str(BENCH_SCRIPT), "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0
    assert "--rerank-disable" in result.stdout, (
        "bench_longmemeval.py --help must list --rerank-disable "
        "for M4 T6 ablation; got:\n" + result.stdout
    )


def test_bench_all_disable_flags_compose():
    """--channels-disable + --expand-disable + --rerank-disable accept
    together without argparse error."""
    result = subprocess.run(
        [
            sys.executable, str(BENCH_SCRIPT),
            "/nonexistent/file.json",
            "--limit", "1",
            "--mode", "bm25",
            "--channels-disable", "graph",
            "--expand-disable",
            "--rerank-disable",
        ],
        capture_output=True, text=True, timeout=15,
    )
    # File-not-found is fine; argparse errors are not.
    assert "argument --expand-disable" not in result.stderr, (
        "argparse rejected --expand-disable; stderr:\n" + result.stderr
    )
    assert "argument --rerank-disable" not in result.stderr, (
        "argparse rejected --rerank-disable; stderr:\n" + result.stderr
    )
