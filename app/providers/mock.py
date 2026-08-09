import random
import time
from typing import Iterator, List, Dict

from app.providers.base import ProviderAdapter, CompletionResult, ProviderError


class HealthRegistry:
    """Provider health switchboard, backed by SQLite (not just in-memory) so
    that toggling a provider down/degraded from the ops console is visible
    across all Gunicorn worker processes, not just the process that handled
    the admin request. Falls back to a per-process cache to avoid a DB hit
    on every single provider call in the hot path.
    """
    def __init__(self):
        self._cache = {}
        self._cache_ts = 0
        self._cache_ttl = 1.0  # seconds -- bounds staleness after a toggle

    def set(self, provider: str, state: str):
        from app.core.db import get_conn
        conn = get_conn()
        conn.execute(
            "INSERT INTO provider_health (provider, state) VALUES (?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET state = excluded.state",
            (provider, state),
        )
        self._cache[provider] = state

    def get(self, provider: str) -> str:
        import time
        now = time.time()
        if now - self._cache_ts > self._cache_ttl:
            self._refresh()
        return self._cache.get(provider, "up")

    def all(self):
        self._refresh()
        return dict(self._cache)

    def _refresh(self):
        import time
        from app.core.db import get_conn
        conn = get_conn()
        rows = conn.execute("SELECT provider, state FROM provider_health").fetchall()
        self._cache = {r["provider"]: r["state"] for r in rows}
        self._cache_ts = time.time()


health = HealthRegistry()


def _fake_tokenize_count(text: str) -> int:
    return max(1, int(len(text.split()) * 1.3))


def _fake_answer(messages: List[Dict]) -> str:
    last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = m.get("content", "")
            break
    return (
        f"[mock answer] Here's a response to: '{last_user[:120]}'. "
        "This is a deterministic mock completion used for local/offline testing "
        "of the Prism gateway's routing, streaming, caching and metering behavior."
    )


class SimpleMockProvider(ProviderAdapter):
    """Simulates a real LLM provider: latency, occasional transient errors,
    and a health switch the ops console/tests can flip to force outages.
    """

    def __init__(self, name: str, base_latency: float = 0.15, error_rate: float = 0.03):
        self.name = name
        self.base_latency = base_latency
        self.error_rate = error_rate

    def complete(self, model: str, messages: List[Dict], timeout: float) -> CompletionResult:
        state = health.get(self.name)
        if state == "down":
            raise ProviderError(f"{self.name} is down", retryable=True)
        if state == "degraded":
            # Degraded providers are slow enough to trip the caller's timeout.
            time.sleep(min(timeout + 0.5, timeout + 0.5))
            raise ProviderError(f"{self.name} request timed out after {timeout}s", retryable=True)
        if random.random() < self.error_rate:
            time.sleep(self.base_latency)
            raise ProviderError(f"{self.name} transient error", retryable=True)

        latency = self.base_latency + random.random() * 0.1
        if latency > timeout:
            raise ProviderError(f"{self.name} request timed out after {timeout}s", retryable=True)
        time.sleep(latency)

        answer = _fake_answer(messages)
        input_tokens = sum(_fake_tokenize_count(m.get("content", "")) for m in messages)
        output_tokens = _fake_tokenize_count(answer)
        return CompletionResult(text=answer, input_tokens=input_tokens, output_tokens=output_tokens)

    def stream(self, model: str, messages: List[Dict], timeout: float) -> Iterator[str]:
        state = health.get(self.name)
        if state == "down":
            raise ProviderError(f"{self.name} is down", retryable=True)
        answer = _fake_answer(messages)
        words = answer.split(" ")
        start = time.time()
        for w in words:
            if state == "degraded" or (time.time() - start) > timeout:
                raise ProviderError(f"{self.name} timed out mid-stream", retryable=True)
            time.sleep(0.02)
            yield w + " "


PROVIDERS = {
    "openai": SimpleMockProvider("openai", base_latency=0.12, error_rate=0.03),
    "anthropic": SimpleMockProvider("anthropic", base_latency=0.18, error_rate=0.03),
}

import os as _os
if _os.environ.get("PRISM_USE_REAL_PROVIDERS", "false").lower() == "true":
    # Route both "openai" and "anthropic" pricing-table providers through the
    # single OpenRouter key -- OpenRouter itself picks the right upstream
    # based on the model name prefix (openai/... vs anthropic/...).
    from app.providers.openrouter import OpenRouterProvider
    _or = OpenRouterProvider("openrouter")
    PROVIDERS["openai"] = _or
    PROVIDERS["anthropic"] = _or


def get_provider(name: str) -> ProviderAdapter:
    if name not in PROVIDERS:
        raise ValueError(f"Unknown provider '{name}'")
    return PROVIDERS[name]
