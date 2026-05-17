"""M5 T5 — Embedding-tier comparison runner smoke tests.

Per spec A12: 3 smoke tests covering argparse + tier detection +
subprocess plumbing. No actual bench run in CI (opt-in, ~$5-10).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


_RUNNER = (
    Path(__file__).resolve().parent.parent
    / "evaluation/longmemeval/run_tier_comparison.py"
)


def _load_runner_module():
    spec = importlib.util.spec_from_file_location(
        "tier_comparison_runner", _RUNNER,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tier_comparison_argparse_help():
    """`--help` works + exits 0."""
    result = subprocess.run(
        [sys.executable, str(_RUNNER), "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0
    assert "tier" in result.stdout.lower()
    assert "modes" in result.stdout.lower()


def test_tier_comparison_skips_missing_tiers(monkeypatch, capsys, tmp_path):
    """Env scrubbed → only `local` tier available; runner reports the
    pruned list without failing."""
    for var in ("OPENAI_API_KEY", "VOYAGE_API_KEY", "COHERE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    mod = _load_runner_module()
    tiers = mod._available_tiers()
    assert tiers == ["local"], (
        f"expected only 'local' available when no hosted keys; "
        f"got {tiers}"
    )


def test_tier_comparison_with_mocked_subprocess(
    monkeypatch, tmp_path,
):
    """Patch subprocess.run to return synthetic bench output; assert
    the runner aggregates correctly into the log file."""
    mod = _load_runner_module()
    for var in ("OPENAI_API_KEY", "VOYAGE_API_KEY", "COHERE_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    # Synthesize JSONL output the runner would parse.
    fake_results = tmp_path / "results"
    fake_results.mkdir()

    written_outs: list[Path] = []

    def _fake_subprocess(cmd, *_, **kwargs):
        # Find the --out path in the cmd
        out_idx = cmd.index("--out") + 1
        out_path = Path(cmd[out_idx])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Write 2 synthetic per-question rows with reasonable metrics
        import json as jsonlib
        with out_path.open("w") as f:
            for _ in range(2):
                f.write(jsonlib.dumps({
                    "question_id": "q1",
                    "retrieval_results": {
                        "metrics": {
                            "session": {
                                "recall_any@1": 0.8,
                                "recall_any@3": 0.95,
                                "recall_any@5": 1.0,
                                "recall_any@10": 1.0,
                            },
                        },
                    },
                }) + "\n")
        written_outs.append(out_path)

        class _R:
            returncode = 0
            stderr = ""
            stdout = ""
        return _R()

    monkeypatch.setattr(subprocess, "run", _fake_subprocess)

    log_path = tmp_path / "out.log"
    rc = mod.main([
        "--data", str(tmp_path / "fake_data.json"),
        "--out", str(log_path),
        "--modes", "bm25,bm25-full",
        "--limit", "2",
    ])
    assert rc == 0
    assert log_path.exists()
    log_content = log_path.read_text()
    assert "local" in log_content
    assert "bm25" in log_content
    assert "0.950" in log_content or "0.95" in log_content
