#!/usr/bin/env bash
#
# M3b (T4) — 4-way ablation harness (A10).
#
# Runs bench_longmemeval.py 4 times per mode:
#   all_on         — default 3-channel
#   graph_off      — --channels-disable=graph (2-channel: bm25+vector)
#   extraction_off — no LLM key + 3-channel (entities empty naturally,
#                    graph contributes 0)
#   all_off        — no LLM key + --channels-disable=graph
#                    (= the M2-equivalent path, byte-identity per A6)
#
# Each scenario is run for each retrieval mode (bm25, bm25-full,
# hybrid-temporal). Results written to results/m3b_<scenario>_<mode>.jsonl
# for downstream comparison.
#
# Usage: run from evaluation/longmemeval/ :
#   ./run_4way_ablation.sh           # all 3 modes
#   ./run_4way_ablation.sh bm25      # single mode
#
# The script scrubs LLM/embedder API keys for the *_off scenarios so
# entities stay empty (free-path floor). For the *_on scenarios, set
# ANTHROPIC_API_KEY (or OPENAI_API_KEY) in your env before running
# to actually populate the graph during ingestion — without that,
# all_on degrades to extraction_off byte-for-byte.

set -euo pipefail

cd "$(dirname "$0")"

DATA="data/longmemeval_s_cleaned.json"
MODES=("${@:-bm25 bm25-full hybrid-temporal}")
RESULTS="results"
mkdir -p "$RESULTS"

run_scenario() {
    local scenario="$1"
    local mode="$2"
    local extra_args="$3"
    local env_prefix="$4"

    local out="$RESULTS/m3b_${scenario}_${mode}.jsonl"
    echo "─── scenario=$scenario mode=$mode ─── → $out"
    eval "$env_prefix python bench_longmemeval.py \"$DATA\" \
        --mode \"$mode\" $extra_args --out \"$out\""
}

SCRUB_ENV="env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY \
    -u VOYAGE_API_KEY -u COHERE_API_KEY"

for mode in $MODES; do
    # all_on — keep current env (caller should set ANTHROPIC_API_KEY
    # before running to actually exercise the graph channel)
    run_scenario "all_on" "$mode" "" ""

    # graph_off — current env, graph channel disabled
    run_scenario "graph_off" "$mode" "--channels-disable=graph" ""

    # extraction_off — keys scrubbed, all channels enabled
    # (graph channel will return empty because entities stay empty)
    run_scenario "extraction_off" "$mode" "" "$SCRUB_ENV"

    # all_off — keys scrubbed AND graph disabled (= M2 byte-identity per A6)
    run_scenario "all_off" "$mode" "--channels-disable=graph" "$SCRUB_ENV"
done

echo
echo "─── 4-way ablation complete ───"
echo "Compare per-mode A10 gates:"
echo "  graph_off.R@10 ≤ all_on.R@10"
echo "  all_off ≡ M2 stored JSONL (cmp byte-identity per A6)"
