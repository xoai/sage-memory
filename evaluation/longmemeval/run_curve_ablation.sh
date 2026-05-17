#!/usr/bin/env bash
#
# M3b (T5) — 3-curve ablation (A7).
#
# Runs bench_longmemeval.py 3 times per mode with each
# SAGE_GRAPH_RANK_CURVE value: linear (default), harmonic,
# type-weighted. Output written to results/m3b_curves_<curve>_<mode>.jsonl
# for downstream comparison.
#
# Note: the graph channel only matters when entities are populated.
# In the bench env (free-path scrub) entities stay empty and all 3
# curves produce identical output — A7's signal comes from running
# with ANTHROPIC_API_KEY set so M3a's worker populates the graph
# during ingestion. The script does NOT scrub LLM keys for this reason.
#
# Decision rule per A7: if any non-default curve wins by ≥2pp R@3,
# update ADR-004 default + prepend an entry to .sage/decisions.md.

set -euo pipefail

cd "$(dirname "$0")"

DATA="data/longmemeval_s_cleaned.json"
MODES=("${@:-bm25 bm25-full hybrid-temporal}")
CURVES=(linear harmonic type-weighted)
RESULTS="results"
mkdir -p "$RESULTS"

for mode in $MODES; do
    for curve in "${CURVES[@]}"; do
        out="$RESULTS/m3b_curves_${curve}_${mode}.jsonl"
        echo "─── curve=$curve mode=$mode ─── → $out"
        SAGE_GRAPH_RANK_CURVE="$curve" \
            python bench_longmemeval.py "$DATA" \
            --mode "$mode" --out "$out"
    done
done

echo
echo "─── 3-curve ablation complete ───"
echo "Compare recall@3 per curve per mode."
echo "If non-default wins by ≥2pp R@3:"
echo "  (a) update ADR-004 default rank_curve"
echo "  (b) prepend decisions.md entry explaining the switch"
