#!/usr/bin/env python3
"""
scripts/smoke_test.py

Fast sanity check of the core Prism flows. Exits non-zero on any failure so
it can be used as a CI gate. Assumes the gateway is already running (default
http://localhost:8000) with mock providers and the seeded demo keys.

Usage:
    python3 scripts/smoke_test.py [--base-url http://localhost:8000]
"""
import argparse
import sys
import time
import os
import requests

ALPHA_KEY = "sk-prism-team-alpha-0001"
BETA_KEY = "sk-prism-team-beta-0002"
GAMMA_KEY = "sk-prism-team-gamma-0003"
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"[{PASS}] {name}")
    else:
        print(f"[{FAIL}] {name} {detail}")
        failures.append(name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    args = ap.parse_args()
    base = args.base_url

    # 1. Health check
    r = requests.get(f"{base}/healthz", timeout=5)
    check("gateway healthz responds 200", r.status_code == 200)

    # 2. Basic completion, fast alias
    r = requests.post(f"{base}/v1/chat/completions",
                       headers={"Authorization": f"Bearer {ALPHA_KEY}"},
                       json={"model": "fast", "messages": [{"role": "user", "content": "smoke test hello"}]},
                       timeout=15)
    check("fast alias completion returns 200", r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
    check("response has choices[0].message.content", "choices" in r.json() and r.json()["choices"][0]["message"]["content"],
          f"body={r.text[:200]}")
    check("x-prism-provider header present", "x-prism-provider" in r.headers)
    check("x-prism-cost-usd header present", "x-prism-cost-usd" in r.headers)
    check("x-prism-cache header present", "x-prism-cache" in r.headers)
    check("x-prism-fallback header present", "x-prism-fallback" in r.headers)

    # 3. Smart alias
    r = requests.post(f"{base}/v1/chat/completions",
                       headers={"Authorization": f"Bearer {ALPHA_KEY}"},
                       json={"model": "smart", "messages": [{"role": "user", "content": "smoke test smart"}]},
                       timeout=15)
    check("smart alias completion returns 200", r.status_code == 200)

    # 4. Auto routing classifies and returns a real model
    r = requests.post(f"{base}/v1/chat/completions",
                       headers={"Authorization": f"Bearer {ALPHA_KEY}"},
                       json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
                       timeout=15)
    check("auto routing returns 200", r.status_code == 200)
    check("auto routing sets prism_auto_routed_to", "prism_auto_routed_to" in r.json(), f"body={r.text[:200]}")

    # 5. Streaming
    r = requests.post(f"{base}/v1/chat/completions",
                       headers={"Authorization": f"Bearer {ALPHA_KEY}"},
                       json={"model": "fast", "messages": [{"role": "user", "content": "smoke test stream"}], "stream": True},
                       timeout=15, stream=True)
    body = r.content.decode("utf-8", errors="ignore")
    check("streaming returns 200", r.status_code == 200)
    check("stream contains data: chunks", "data:" in body)
    check("stream terminates with [DONE]", "[DONE]" in body)

    # 6. Semantic cache: same question, paraphrased, should eventually hit
    q1 = {"model": "fast", "messages": [{"role": "user", "content": "What is the capital of Japan?"}]}
    q2 = {"model": "fast", "messages": [{"role": "user", "content": "Tell me Japan's capital city"}]}
    r1 = requests.post(f"{base}/v1/chat/completions", headers={"Authorization": f"Bearer {ALPHA_KEY}"}, json=q1, timeout=15)
    r2 = requests.post(f"{base}/v1/chat/completions", headers={"Authorization": f"Bearer {ALPHA_KEY}"}, json=q2, timeout=15)
    check("first cache query is a miss", r1.headers.get("x-prism-cache") == "miss")
    # Not asserting hard pass/fail on r2 here since it depends on the tuned
    # threshold -- covered explicitly by tests/test_cache.py instead.
    print(f"    (paraphrase cache result: {r2.headers.get('x-prism-cache')})")

    # 7. Invalid key rejected
    r = requests.post(f"{base}/v1/chat/completions",
                       headers={"Authorization": "Bearer not-a-real-key"},
                       json={"model": "fast", "messages": [{"role": "user", "content": "x"}]},
                       timeout=10)
    check("invalid key returns 401", r.status_code == 401)

    # 8. Model not allowed for beta key
    r = requests.post(f"{base}/v1/chat/completions",
                       headers={"Authorization": f"Bearer {BETA_KEY}"},
                       json={"model": "smart", "messages": [{"role": "user", "content": "x"}]},
                       timeout=10)
    check("disallowed model returns 403", r.status_code == 403)

    # 9. Rate limiting for gamma key (rpm_limit=5)
    codes = []
    for _ in range(8):
        rr = requests.post(f"{base}/v1/chat/completions",
                            headers={"Authorization": f"Bearer {GAMMA_KEY}"},
                            json={"model": "fast", "messages": [{"role": "user", "content": "rl test"}]},
                            timeout=10)
        codes.append(rr.status_code)
    check("rate limit eventually returns 429", 429 in codes, f"codes={codes}")

    # 10. Admin provider health toggle + failover
    admin_headers = {"Authorization": f"Bearer {ADMIN_API_KEY}"} if ADMIN_API_KEY else {}
    r_admin = requests.post(f"{base}/admin/provider_health", json={"provider": "openai", "state": "down"},
                             headers=admin_headers, timeout=5)
    check("admin auth accepted for health toggle", r_admin.status_code == 200,
          f"got {r_admin.status_code} -- set ADMIN_API_KEY env var to match the running server")
    r = requests.post(f"{base}/v1/chat/completions",
                       headers={"Authorization": f"Bearer {ALPHA_KEY}"},
                       json={"model": "fast", "messages": [{"role": "user", "content": "failover smoke"}]},
                       timeout=15)
    check("failover succeeds when openai is down", r.status_code == 200 and r.headers.get("x-prism-fallback") == "true",
          f"status={r.status_code} fallback={r.headers.get('x-prism-fallback')}")
    requests.post(f"{base}/admin/provider_health", json={"provider": "openai", "state": "up"},
                  headers=admin_headers, timeout=5)

    # 11. Admin endpoints reject requests with no/bad key
    r = requests.get(f"{base}/admin/provider_health", timeout=5)
    check("admin endpoint rejects missing auth", r.status_code == 401)

    # 11. Usage summary API
    r = requests.get(f"{base}/v1/usage/summary", timeout=10)
    check("usage summary returns 200", r.status_code == 200)
    check("usage summary has entries", len(r.json().get("usage", [])) > 0)

    print()
    if failures:
        print(f"SMOKE TEST FAILED: {len(failures)} check(s) failed: {failures}")
        sys.exit(1)
    else:
        print("SMOKE TEST PASSED: all checks green.")
        sys.exit(0)


if __name__ == "__main__":
    main()
