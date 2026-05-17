#!/usr/bin/env python3
"""LongMemEval evaluation — supports both Anthropic and OpenAI as judge.

Drop-in replacement for LongMemEval's evaluate_qa.py with multi-backend support.

Usage:
  # With Claude (default)
  export ANTHROPIC_API_KEY=sk-...
  python evaluate.py --hyp results/output.jsonl --ref data/longmemeval_oracle.json --llm anthropic

  # With OpenAI (compatible with official LongMemEval evaluation)
  export OPENAI_API_KEY=sk-...
  python evaluate.py --hyp results/output.jsonl --ref data/longmemeval_oracle.json --llm openai

  # With custom OpenAI model
  python evaluate.py --hyp results/output.jsonl --ref data/longmemeval_oracle.json --llm openai --model gpt-4o
"""

import argparse
import json
import os
import sys
import urllib.request
from collections import defaultdict


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LLM clients
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def call_anthropic(prompt: str, model: str = "claude-sonnet-4-20250514") -> str:
    body = json.dumps({
        "model": model,
        "max_tokens": 10,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data["content"][0]["text"].strip()


def call_openai(prompt: str, model: str = "gpt-4o-mini") -> str:
    body = json.dumps({
        "model": model,
        "max_tokens": 10,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"].strip()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Evaluation prompts (matching LongMemEval's evaluate_qa.py)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_eval_prompt(task: str, question: str, answer: str, response: str,
                    abstention: bool = False) -> str:
    if not abstention:
        if task in ["single-session-user", "single-session-assistant", "multi-session"]:
            return (
                "I will give you a question, a correct answer, and a response from a model. "
                "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
                "If the response is equivalent to the correct answer or contains all the intermediate "
                "steps to get the correct answer, you should also answer yes. If the response only "
                "contains a subset of the information required by the answer, answer no.\n\n"
                f"Question: {question}\n\nCorrect Answer: {answer}\n\n"
                f"Model Response: {response}\n\n"
                "Is the model response correct? Answer yes or no only."
            )
        elif task == "temporal-reasoning":
            return (
                "I will give you a question, a correct answer, and a response from a model. "
                "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
                "If the response is equivalent to the correct answer or contains all the intermediate "
                "steps to get the correct answer, you should also answer yes. If the response only "
                "contains a subset of the information required by the answer, answer no. "
                "In addition, do not penalize off-by-one errors for the number of days. If the question "
                "asks for the number of days/weeks/months, etc., and the model makes off-by-one errors "
                "(e.g., predicting 19 days when the answer is 18), the model's response is still correct.\n\n"
                f"Question: {question}\n\nCorrect Answer: {answer}\n\n"
                f"Model Response: {response}\n\n"
                "Is the model response correct? Answer yes or no only."
            )
        elif task == "knowledge-update":
            return (
                "I will give you a question, a correct answer, and a response from a model. "
                "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
                "If the response contains some previous information along with an updated answer, "
                "the response should be considered as correct as long as the updated answer is the "
                "required answer.\n\n"
                f"Question: {question}\n\nCorrect Answer: {answer}\n\n"
                f"Model Response: {response}\n\n"
                "Is the model response correct? Answer yes or no only."
            )
        elif task == "single-session-preference":
            return (
                "I will give you a question, a rubric for desired personalized response, "
                "and a response from a model. Please answer yes if the response satisfies the desired "
                "response. Otherwise, answer no. The model does not need to reflect all the points in "
                "the rubric. The response is correct as long as it recalls and utilizes the user's "
                "personal information correctly.\n\n"
                f"Question: {question}\n\nRubric: {answer}\n\n"
                f"Model Response: {response}\n\n"
                "Is the model response correct? Answer yes or no only."
            )
        else:
            return (
                f"Question: {question}\nCorrect Answer: {answer}\n"
                f"Model Response: {response}\n\n"
                "Is the model response correct? Answer yes or no only."
            )
    else:
        return (
            "I will give you an unanswerable question, an explanation, and a response from a model. "
            "Please answer yes if the model correctly identifies the question as unanswerable. "
            "The model could say that the information is incomplete, or some other information is given "
            "but the asked information is not.\n\n"
            f"Question: {question}\n\nExplanation: {answer}\n\n"
            f"Model Response: {response}\n\n"
            "Does the model correctly identify the question as unanswerable? Answer yes or no only."
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    parser = argparse.ArgumentParser(description="Evaluate LongMemEval results")
    parser.add_argument("--hyp", required=True, help="Hypothesis JSONL from run_longmemeval.py")
    parser.add_argument("--ref", required=True, help="Reference JSON (longmemeval_*.json)")
    parser.add_argument("--output", default=None, help="Output JSONL with eval labels (default: hyp + .eval)")
    parser.add_argument("--llm", choices=["anthropic", "openai"], default="anthropic",
                        help="LLM backend for judging (default: anthropic)")
    parser.add_argument("--model", default=None,
                        help="Model override (default: claude-sonnet-4-20250514 for anthropic, gpt-4o-mini for openai)")
    args = parser.parse_args()

    output_file = args.output or (args.hyp + ".eval")

    # Select LLM backend
    if args.llm == "anthropic":
        model = args.model or "claude-sonnet-4-20250514"
        call_llm = lambda prompt: call_anthropic(prompt, model)
        if "ANTHROPIC_API_KEY" not in os.environ:
            print("Error: ANTHROPIC_API_KEY not set"); sys.exit(1)
    else:
        model = args.model or "gpt-4o-mini"
        call_llm = lambda prompt: call_openai(prompt, model)
        if "OPENAI_API_KEY" not in os.environ:
            print("Error: OPENAI_API_KEY not set"); sys.exit(1)

    # Load data
    with open(args.ref) as f:
        references = json.load(f)
    qid2data = {e["question_id"]: e for e in references}

    with open(args.hyp) as f:
        hypotheses = [json.loads(line) for line in f if line.strip()]

    print(f"Evaluating {len(hypotheses)} hypotheses")
    print(f"Judge: {args.llm} ({model})")

    qtype2acc = defaultdict(list)
    results = []
    errors = 0

    for i, entry in enumerate(hypotheses):
        qid = entry["question_id"]
        if qid not in qid2data:
            print(f"  Warning: {qid} not in reference, skipping")
            continue

        ref = qid2data[qid]
        qtype = ref["question_type"]
        question = ref["question"]
        answer = ref["answer"]
        hypothesis = entry["hypothesis"]
        is_abstention = qid.endswith("_abs")

        print(f"  [{i+1}/{len(hypotheses)}] {qid} ({qtype})...", end=" ", flush=True)

        prompt = get_eval_prompt(qtype, question, answer, hypothesis, is_abstention)

        try:
            eval_response = call_llm(prompt)
            label = "yes" in eval_response.lower()
        except Exception as e:
            print(f"error: {e}")
            label = False
            errors += 1

        entry["autoeval_label"] = {"model": model, "label": label}
        results.append(entry)
        qtype2acc[qtype].append(1 if label else 0)
        print("✅" if label else "❌")

    # Write results
    with open(output_file, "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")

    # Print metrics
    all_scores = [1 if r["autoeval_label"]["label"] else 0 for r in results]
    overall = sum(all_scores) / max(len(all_scores), 1)

    print(f"\n{'='*60}")
    print(f"  EVALUATION RESULTS — {args.llm} ({model})")
    print(f"{'='*60}")
    print(f"  Overall accuracy: {overall:.1%} ({sum(all_scores)}/{len(all_scores)})")
    if errors:
        print(f"  API errors: {errors}")
    print()

    for qtype in sorted(qtype2acc.keys()):
        scores = qtype2acc[qtype]
        acc = sum(scores) / max(len(scores), 1)
        print(f"  {qtype:<30s}  {acc:.1%}  (n={len(scores)})")

    print(f"\n  Results saved to {output_file}")


if __name__ == "__main__":
    main()
