#!/usr/bin/env python3
"""
scripts/routing_eval.py

Runs the labeled eval set (scripts/routing_eval_set.json) against Prism's
`auto` routing classifier and reports accuracy, then compares against a
naive length-only baseline (classify as "smart" if word count exceeds a
tuned threshold) to prove the real classifier isn't just measuring length.

The eval set is deliberately adversarial to a length baseline:
  - "short-but-hard" prompts (e.g. "Prove sqrt(2) is irrational") are SHORT
    but need "smart" -- a length baseline will get these wrong.
  - "long-but-trivial" prompts (e.g. "list these 8 fruits in a table") are
    LONG but only need "fast" -- a length baseline will get these wrong too.

Can run either:
  (a) against a live gateway (--base-url), calling /v1/chat/completions with
      model=auto and reading `prism_auto_routed_to` from the response, or
  (b) directly against the classifier function (--offline), which is faster
      and doesn't require a running server.

Usage:
    python3 scripts/routing_eval.py --offline
    python3 scripts/routing_eval.py --base-url http://localhost:8000
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def load_eval_set():
    path = os.path.join(os.path.dirname(__file__), "routing_eval_set.json")
    with open(path) as f:
        return json.load(f)


def length_only_baseline(prompt: str, threshold_words: int = 12) -> str:
    return "smart" if len(prompt.split()) > threshold_words else "fast"


def run_offline(eval_set):
    from app.core.difficulty import classify
    from app.core.config import settings
    threshold = settings.gateway_config["auto_routing"]["difficulty_threshold"]
    results = []
    for case in eval_set:
        predicted = classify(case["prompt"], threshold)
        results.append((case, predicted))
    return results


def run_live(eval_set, base_url, key="sk-prism-team-alpha-0001"):
    import requests
    results = []
    for case in eval_set:
        r = requests.post(f"{base_url}/v1/chat/completions",
                           headers={"Authorization": f"Bearer {key}"},
                           json={"model": "auto", "messages": [{"role": "user", "content": case["prompt"]}]},
                           timeout=15)
        predicted = r.json().get("prism_auto_routed_to", "?")
        results.append((case, predicted))
    return results


def report(results, label):
    correct = sum(1 for case, pred in results if pred == case["expected"])
    total = len(results)
    trap_results = [(c, p) for c, p in results if "trap" in c.get("note", "")]
    trap_correct = sum(1 for c, p in trap_results if p == c["expected"])

    print(f"\n=== {label} ===")
    print(f"Overall accuracy: {correct}/{total} ({100*correct/total:.1f}%)")
    if trap_results:
        print(f"Trap-case accuracy (short-but-hard / long-but-trivial): "
              f"{trap_correct}/{len(trap_results)} ({100*trap_correct/len(trap_results):.1f}%)")
    print("\nPer-case:")
    for case, pred in results:
        mark = "OK " if pred == case["expected"] else "ERR"
        note = f"  [{case['note']}]" if "note" in case else ""
        print(f"  [{mark}] expected={case['expected']:5s} got={pred:5s} :: {case['prompt'][:70]}{note}")
    return correct / total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=None, help="If set, run against a live gateway instead of offline.")
    ap.add_argument("--offline", action="store_true", help="Run directly against the classifier (no server needed).")
    args = ap.parse_args()

    eval_set = load_eval_set()

    if args.base_url and not args.offline:
        results = run_live(eval_set, args.base_url)
        prism_acc = report(results, "Prism auto-routing (LIVE via /v1/chat/completions)")
    else:
        results = run_offline(eval_set)
        prism_acc = report(results, "Prism auto-routing (OFFLINE classifier)")

    baseline_results = [(case, length_only_baseline(case["prompt"])) for case in eval_set]
    baseline_acc = report(baseline_results, "Length-only baseline")

    print("\n=== COMPARISON ===")
    print(f"Prism auto-routing accuracy: {100*prism_acc:.1f}%")
    print(f"Length-only baseline accuracy: {100*baseline_acc:.1f}%")
    if prism_acc > baseline_acc:
        print(f"RESULT: PASS - Prism beats the length-only baseline by {100*(prism_acc-baseline_acc):.1f} points.")
        sys.exit(0)
    else:
        print("RESULT: FAIL - Prism does not beat the length-only baseline.")
        sys.exit(1)


if __name__ == "__main__":
    main()
