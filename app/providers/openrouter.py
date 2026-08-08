"""
Real provider adapter backed by OpenRouter (https://openrouter.ai), which
proxies OpenAI, Anthropic, Google, etc. behind one OpenAI-compatible API.

This follows the exact same ProviderAdapter interface as
app/providers/mock.py, so swapping mock -> real is a one-line change in
PROVIDERS (see get_provider below) and nothing in routing, retries,
budgets, or caching has to change.

Auth: reads the key from the OPENROUTER_API_KEY (preferred) or
OPENAI_API_KEY env var -- never hardcode a key in source or commit it.
Put it in a local .env file (see .env.example) which is gitignored.
"""
import os
import json
import time
from typing import Iterator, List, Dict

import requests

from app.providers.base import ProviderAdapter, CompletionResult, ProviderError

OPENROUTER_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")


def _api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ProviderError(
            "No API key configured. Set OPENROUTER_API_KEY (or OPENAI_API_KEY) in your environment.",
            retryable=False,
        )
    return key


# Maps Prism's internal model names to OpenRouter's prefixed model names.
# Extend this as you add more real models to data/model_pricing.json.
OPENROUTER_MODEL_MAP = {
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "gpt-4o": "openai/gpt-4o",
    "claude-haiku": "anthropic/claude-3-5-haiku",
    "claude-sonnet": "anthropic/claude-3-5-sonnet",
}


class OpenRouterProvider(ProviderAdapter):
    def __init__(self, name: str = "openrouter"):
        self.name = name

    def _headers(self):
        return {
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
        }

    def _resolve(self, model: str) -> str:
        return OPENROUTER_MODEL_MAP.get(model, model)

    def complete(self, model: str, messages: List[Dict], timeout: float) -> CompletionResult:
        or_model = self._resolve(model)
        try:
            resp = requests.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers=self._headers(),
                json={"model": or_model, "messages": messages},
                timeout=timeout,
            )
        except requests.exceptions.Timeout:
            raise ProviderError(f"{self.name} request timed out after {timeout}s", retryable=True)
        except requests.exceptions.RequestException as e:
            raise ProviderError(f"{self.name} network error: {e}", retryable=True)

        if resp.status_code == 429:
            raise ProviderError(f"{self.name} rate limited", retryable=True)
        if resp.status_code >= 500:
            raise ProviderError(f"{self.name} server error {resp.status_code}", retryable=True)
        if resp.status_code >= 400:
            # Auth errors, bad model name, etc. -- not worth retrying.
            raise ProviderError(f"{self.name} error {resp.status_code}: {resp.text[:200]}", retryable=False)

        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        return CompletionResult(text=text, input_tokens=input_tokens, output_tokens=output_tokens)

    def stream(self, model: str, messages: List[Dict], timeout: float) -> Iterator[str]:
        or_model = self._resolve(model)
        try:
            resp = requests.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers=self._headers(),
                json={"model": or_model, "messages": messages, "stream": True},
                timeout=timeout,
                stream=True,
            )
        except requests.exceptions.Timeout:
            raise ProviderError(f"{self.name} request timed out after {timeout}s", retryable=True)
        except requests.exceptions.RequestException as e:
            raise ProviderError(f"{self.name} network error: {e}", retryable=True)

        if resp.status_code >= 400:
            raise ProviderError(f"{self.name} error {resp.status_code}: {resp.text[:200]}",
                                 retryable=resp.status_code >= 500 or resp.status_code == 429)

        start = time.time()
        for line in resp.iter_lines():
            if time.time() - start > timeout:
                raise ProviderError(f"{self.name} timed out mid-stream", retryable=True)
            if not line:
                continue
            line = line.decode("utf-8")
            if not line.startswith("data: "):
                continue
            payload = line[len("data: "):]
            if payload.strip() == "[DONE]":
                return
            try:
                chunk = json.loads(payload)
                delta = chunk["choices"][0]["delta"].get("content")
                if delta:
                    yield delta
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
