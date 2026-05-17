#!/usr/bin/env python3
"""M5 T5 — Embedding-tier comparison runner (opt-in, manual).

Runs `bench_longmemeval.py` once per available hosted tier × N modes
and aggregates the metrics into a side-by-side table written to
.sage/work/20260516-retrieval-upgrade/M5/embedding-tier-comparison.log.

Tier detection at runtime:
  - openai (text-embedding-3-small, 1536d): OPENAI_API_KEY set
  - voyage (voyage-3-lite, 512d):            VOYAGE_API_KEY set
  - local  (LocalEmbedder, 384d):            always available

Decision rule per spec A12: if a non-default tier wins by ≥3pp R@3,
the runner flags this for an ADR-005 update.

Cost: ~$5-10 across 3 tiers × 3 modes × 500 questions. Opt-in.

Usage:
    python evaluation/longmemeval/run_tier_comparison.py
    python evaluation/longmemeval/run_tier_comparison.py --modes bm25,hybrid-temporal
    python evaluation/longmemeval/run_tier_comparison.py --limit 50  # smoke
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BENCH_SCRIPT = _REPO_ROOT / "evaluation/longmemeval/bench_longmemeval.py"
_DEFAULT_DATA = _REPO_ROOT / "evaluation/longmemeval/data/longmemeval_s_cleaned.json"
_DEFAULT_LOG = (
    _REPO_ROOT
    / ".sage/work/20260516-retrieval-upgrade/M5/embedding-tier-comparison.log"
)

_DEFAULT_MODES = ["bm25", "bm25-full", "hybrid-temporal"]

# Tier name → required env var (None for always-available).
_TIERS = [
    ("local", None),
    ("openai", "OPENAI_API_KEY"),
    ("voyage", "VOYAGE_API_KEY"),
]


def _tier_available(tier: str, env_var: str | None) -> bool:
    return env_var is None or env_var in os.environ


def _available_tiers() -> list[str]:
    return [t for t, v in _TIERS if _tier_available(t, v)]


def _run_bench(
    *, tier: str, mode: str, data: Path, limit: int, out_jsonl: Path,
) -> dict:
    """Invoke bench_longmemeval.py once and return parsed metrics
    from the JSONL output. Subprocess isolation lets us swap env per
    tier without polluting our own process."""
    env = os.environ.copy()
    # Force tier selection by toggling env vars: scrub the OTHER
    # hosted-tier keys to force the resolver to pick `tier`.
    if tier == "local":
        env.pop("OPENAI_API_KEY", None)
        env.pop("VOYAGE_API_KEY", None)
        env.pop("COHERE_API_KEY", None)
    elif tier == "openai":
        env.pop("VOYAGE_API_KEY", None)
        env.pop("COHERE_API_KEY", None)
    elif tier == "voyage":
        env.pop("OPENAI_API_KEY", None)
        env.pop("COHERE_API_KEY", None)

    cmd = [
        sys.executable, str(_BENCH_SCRIPT), str(data),
        "--mode", mode,
        "--out", str(out_jsonl),
    ]
    if limit > 0:
        cmd += ["--limit", str(limit)]
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=1800,
    )
    if proc.returncode != 0:
        return {
            "tier": tier, "mode": mode, "ok": False,
            "error": proc.stderr[-2000:],
        }
    return _parse_metrics(out_jsonl, tier=tier, mode=mode)


def _parse_metrics(jsonl: Path, *, tier: str, mode: str) -> dict:
    """Compute aggregate session-level R@k + MRR from per-question JSONL."""
    if not jsonl.exists():
        return {"tier": tier, "mode": mode, "ok": False,
                "error": "no output JSONL"}
    n = 0
    sums = {f"R@{k}": 0.0 for k in (1, 3, 5, 10)}
    mrr_sum = 0.0
    with jsonl.open() as f:
        for line in f:
            entry = json.loads(line)
            m = entry.get("retrieval_results", {}).get("metrics", {})
            session = m.get("session", {})
            for k in (1, 3, 5, 10):
                sums[f"R@{k}"] += session.get(f"recall_any@{k}", 0.0)
            # MRR ≈ 1/(rank of first correct hit). The JSONL stores
            # recall_any@k, not MRR directly; we synthesize via 1/R@1
            # when R@1>0 else 1/3 when R@3>0 etc. (rough proxy; the
            # full bench prints MRR but doesn't persist it per-entry).
            for k in (1, 3, 5, 10):
                if session.get(f"recall_any@{k}", 0.0) > 0:
                    mrr_sum += 1.0 / k
                    break
            n += 1
    if n == 0:
        return {"tier": tier, "mode": mode, "ok": False, "error": "empty"}
    return {
        "tier": tier, "mode": mode, "ok": True, "n": n,
        **{k: v / n for k, v in sums.items()},
        "MRR": mrr_sum / n,
    }


def _format_table(rows: list[dict]) -> str:
    """Markdown-ish side-by-side table for the log."""
    header = (
        f"| {'tier':10} | {'mode':18} | {'n':>4} |"
        f" {'R@1':>5} | {'R@3':>5} | {'R@5':>5} | {'R@10':>5} | {'MRR':>5} |"
    )
    sep = "|" + "|".join(["-" * (len(p) + 2) for p in [
        "tier".ljust(10), "mode".ljust(18), "   n",
        " R@1 ", " R@3 ", " R@5 ", " R@10", " MRR ",
    ]]) + "|"
    lines = [header, sep]
    for r in rows:
        if not r.get("ok"):
            lines.append(
                f"| {r['tier']:10} | {r['mode']:18} | {'-':>4} |"
                f" {'-':>5} | {'-':>5} | {'-':>5} | {'-':>5} | {'-':>5} |"
                f"  # SKIPPED: {r.get('error', 'unknown')[:50]}"
            )
            continue
        lines.append(
            f"| {r['tier']:10} | {r['mode']:18} | {r['n']:>4} |"
            f" {r['R@1']:.3f} | {r['R@3']:.3f} | {r['R@5']:.3f} |"
            f" {r['R@10']:.3f} | {r['MRR']:.3f} |"
        )
    return "\n".join(lines)


def _detect_winner_lift(
    rows: list[dict], default_tier: str = "local",
) -> str | None:
    """Per spec A12: if a non-default tier wins R@3 by ≥3pp, return a
    message naming the tier + the lift. Else None."""
    ok_rows = [r for r in rows if r.get("ok")]
    by_tier = {}
    for r in ok_rows:
        by_tier.setdefault(r["tier"], []).append(r["R@3"])
    if default_tier not in by_tier or not by_tier[default_tier]:
        return None
    default_avg = sum(by_tier[default_tier]) / len(by_tier[default_tier])
    best_tier = default_tier
    best_avg = default_avg
    for tier, vals in by_tier.items():
        if tier == default_tier:
            continue
        avg = sum(vals) / len(vals)
        if avg > best_avg:
            best_tier = tier
            best_avg = avg
    lift_pp = (best_avg - default_avg) * 100
    if best_tier != default_tier and lift_pp >= 3.0:
        return (
            f"DECISION: tier {best_tier!r} beats default {default_tier!r} "
            f"by {lift_pp:.1f}pp on avg R@3. Suggest ADR-005 update + "
            f"decisions.md entry."
        )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Embedding-tier comparison runner (M5 T5)",
    )
    parser.add_argument("--data", default=str(_DEFAULT_DATA),
                        help="LongMemEval data JSON")
    parser.add_argument("--out", default=str(_DEFAULT_LOG),
                        help="Where to write the comparison log")
    parser.add_argument(
        "--modes", default=",".join(_DEFAULT_MODES),
        help="Comma-separated bench modes (default: %(default)s)",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Limit per-bench question count (0=all)",
    )
    args = parser.parse_args(argv)

    tiers = _available_tiers()
    if not tiers:
        print("no tiers available — skipping comparison.")
        return 0
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    print(
        f"run_tier_comparison: tiers={tiers}, modes={modes}, "
        f"limit={args.limit}"
    )

    rows: list[dict] = []
    for tier in tiers:
        for mode in modes:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            out_jsonl = Path(
                f"evaluation/longmemeval/results/"
                f"tier_{tier}_{mode}_{stamp}.jsonl"
            )
            row = _run_bench(
                tier=tier, mode=mode,
                data=Path(args.data), limit=args.limit,
                out_jsonl=out_jsonl,
            )
            rows.append(row)
            if row.get("ok"):
                print(
                    f"  {tier:8} {mode:18} R@3={row['R@3']:.3f} "
                    f"MRR={row['MRR']:.3f}"
                )
            else:
                print(
                    f"  {tier:8} {mode:18} SKIPPED: "
                    f"{row.get('error', 'unknown')[:80]}"
                )

    log = _format_table(rows)
    decision = _detect_winner_lift(rows)
    if decision:
        log += f"\n\n{decision}"
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(log + "\n", encoding="utf-8")
    print(f"\nwrote comparison log to {out_path}")
    if decision:
        print(decision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
