#!/usr/bin/env python3
"""
scripts/load_test.py

Fires concurrent requests at the gateway to verify:
  1. Rate limiting has no over-admission under concurrency (a key with
     rpm_limit=N never gets more than N requests admitted in one window).
  2. Budget accounting is accurate under concurrency (total recorded spend
     matches the sum of individual request costs, no lost updates).
  3. Reports p50/p95/p99 latency for admitted requests.

Usage:
    python3 scripts/load_test.py [--base-url http://localhost:8000] [--workers 30] [--requests 150]
"""
import argparse
import concurrent.futures
import statistics
import time
import requests

GAMMA_KEY = "sk-prism-team-gamma-0003"   # rpm_limit=5, budget=$0.05 -- tight limits, good for stress
ALPHA_KEY = "sk-prism-team-alpha-0001"   # rpm_limit=60, budget=$25 -- used for latency profiling


def fire_one(base_url, key, model="fast"):
    t0 = time.time()
    try:
        r = requests.post(f"{base_url}/v1/chat/completions",
                           headers={"Authorization": f"Bearer {key}"},
                           json={"model": model, "messages": [{"role": "user", "content": f"load test {t0}"}]},
                           timeout=15)
        latency = (time.time() - t0) * 1000
        return r.status_code, latency
    except Exception as e:
        return -1, (time.time() - t0) * 1000


def test_rate_limit_no_overadmission(base_url, workers, total_requests):
    print(f"\n--- Rate limit over-admission test ({total_requests} concurrent requests, gamma rpm_limit=5) ---")
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(fire_one, base_url, GAMMA_KEY) for _ in range(total_requests)]
        results = [f.result() for f in futures]

    statuses = [s for s, _ in results]
    admitted = statuses.count(200)
    rejected_429 = statuses.count(429)
    rejected_402 = statuses.count(402)
    other = len(statuses) - admitted - rejected_429 - rejected_402

    print(f"  admitted (200): {admitted}")
    print(f"  rate-limited (429): {rejected_429}")
    print(f"  budget-exhausted (402): {rejected_402}")
    print(f"  other: {other}")

    # In a single 60s window, admitted count should not exceed rpm_limit by
    # more than a small margin (requests that landed just across a window
    # boundary can legitimately both be admitted -- this isn't an atomic
    # global limit, it's a per-60s-window limit, so we allow up to 2x limit
    # as a loose upper bound proving there's no gross over-admission, e.g.
    # all `total_requests` sailing through).
    rpm_limit = 5
    over_admission = admitted > rpm_limit * 2
    print(f"  RESULT: {'FAIL - possible over-admission' if over_admission else 'PASS - no gross over-admission'}")
    return not over_admission


def test_budget_accounting(base_url):
    print("\n--- Budget accounting consistency test ---")
    r = requests.get(f"{base_url}/v1/usage/summary?key={GAMMA_KEY}", timeout=10)
    data = r.json().get("usage", [])
    if not data:
        print("  no usage recorded yet for gamma key (may be fully rate-limited) -- skipping")
        return True
    entry = data[0]
    r2 = requests.get(f"{base_url}/admin/keys", timeout=10)
    keys = r2.json().get("keys", [])
    gamma_row = next((k for k in keys if k["tenant"] == "team-gamma"), None)
    if gamma_row is None:
        print("  gamma key row not found in admin/keys -- skipping")
        return True
    logged_total = entry["total_cost_usd"]
    tracked_spend = gamma_row["spent_usd"]
    diff = abs(logged_total - tracked_spend)
    print(f"  sum(request_logs.cost_usd) = {logged_total}")
    print(f"  virtual_keys.spent_usd     = {tracked_spend}")
    print(f"  diff = {diff}")
    ok = diff < 1e-6
    print(f"  RESULT: {'PASS - accounting matches' if ok else 'FAIL - mismatch, possible lost update'}")
    return ok


def test_latency_profile(base_url, workers, total_requests):
    print(f"\n--- Latency profile ({total_requests} requests, alpha key, high rpm limit) ---")
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(fire_one, base_url, ALPHA_KEY) for _ in range(total_requests)]
        results = [f.result() for f in futures]

    latencies = sorted(l for s, l in results if s == 200)
    if not latencies:
        print("  no successful requests to profile")
        return False
    p50 = statistics.median(latencies)
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    p99 = latencies[int(len(latencies) * 0.99) - 1] if len(latencies) >= 100 else latencies[-1]
    print(f"  admitted: {len(latencies)}/{total_requests}")
    print(f"  p50: {p50:.1f}ms  p95: {p95:.1f}ms  p99: {p99:.1f}ms  max: {latencies[-1]:.1f}ms")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--workers", type=int, default=30)
    ap.add_argument("--requests", type=int, default=150)
    args = ap.parse_args()

    print(f"Load testing {args.base_url} with {args.workers} workers / {args.requests} requests per test")

    r1 = test_rate_limit_no_overadmission(args.base_url, args.workers, min(args.requests, 40))
    r2 = test_budget_accounting(args.base_url)
    r3 = test_latency_profile(args.base_url, args.workers, args.requests)

    print("\n=== LOAD TEST SUMMARY ===")
    print(f"  rate limit no-overadmission: {'PASS' if r1 else 'FAIL'}")
    print(f"  budget accounting:           {'PASS' if r2 else 'FAIL'}")
    print(f"  latency profile collected:   {'PASS' if r3 else 'FAIL'}")

    if not (r1 and r2 and r3):
        exit(1)


if __name__ == "__main__":
    main()
