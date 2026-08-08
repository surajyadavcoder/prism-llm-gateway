"""
Resolves a requested model/alias to a concrete (provider, model) pair, and
drives retries + failover across the fallback chain.

Resolution order:
  1. "auto"             -> classify difficulty -> "fast" or "smart" alias -> step 2
  2. alias (fast/smart)  -> primary model, with an ordered fallback chain
  3. concrete model name -> used as-is, no fallback chain (single attempt)

Retry/failover policy: each candidate model gets up to `max_attempts` tries
with exponential backoff for *retryable* provider errors. If a candidate is
exhausted, we move to the next model in the fallback chain and record that a
fallback occurred (surfaced via the x-prism-fallback response header).
"""
import time
import random
from dataclasses import dataclass, field
from typing import List, Optional

from app.core.config import settings
from app.core.difficulty import classify
from app.core.cost import provider_for
from app.providers.mock import get_provider
from app.providers.base import ProviderError, CompletionResult


@dataclass
class ResolutionPlan:
    candidates: List[str]
    auto_classified_as: Optional[str] = None


def resolve_candidates(requested_model: str) -> ResolutionPlan:
    cfg = settings.gateway_config
    aliases = cfg["aliases"]

    if requested_model in aliases:
        a = aliases[requested_model]
        return ResolutionPlan(candidates=[a["primary"]] + list(a.get("fallback_chain", [])))

    if requested_model in settings.model_pricing:
        return ResolutionPlan(candidates=[requested_model])

    raise ValueError(f"Unknown model/alias '{requested_model}'")


def resolve_auto(prompt_text: str) -> ResolutionPlan:
    cfg = settings.gateway_config
    threshold = cfg["auto_routing"]["difficulty_threshold"]
    tier_alias = classify(prompt_text, threshold)  # "fast" | "smart"
    a = cfg["aliases"][tier_alias]
    return ResolutionPlan(
        candidates=[a["primary"]] + list(a.get("fallback_chain", [])),
        auto_classified_as=tier_alias,
    )


@dataclass
class AttemptOutcome:
    resolved_model: str
    provider: str
    result: CompletionResult
    fallback_used: bool
    attempts_log: List[str] = field(default_factory=list)


def run_with_failover(candidates: List[str], messages) -> AttemptOutcome:
    cfg = settings.gateway_config
    retry_cfg = cfg["retries"]
    timeout = cfg["timeouts"]["provider_request_timeout_seconds"]
    max_attempts = retry_cfg["max_attempts"]
    base_backoff = retry_cfg["base_backoff_seconds"]
    max_backoff = retry_cfg["max_backoff_seconds"]

    attempts_log = []
    last_error = None

    for idx, model in enumerate(candidates):
        provider_name = provider_for(model)
        provider = get_provider(provider_name)
        fallback_used = idx > 0

        for attempt in range(1, max_attempts + 1):
            try:
                result = provider.complete(model, messages, timeout=timeout)
                attempts_log.append(f"{model}@{provider_name} attempt {attempt}: ok")
                return AttemptOutcome(
                    resolved_model=model, provider=provider_name, result=result,
                    fallback_used=fallback_used, attempts_log=attempts_log,
                )
            except ProviderError as e:
                attempts_log.append(f"{model}@{provider_name} attempt {attempt}: {e}")
                last_error = e
                if not e.retryable or attempt == max_attempts:
                    break
                backoff = min(max_backoff, base_backoff * (2 ** (attempt - 1)))
                backoff += random.uniform(0, backoff * 0.1)
                time.sleep(backoff)
        # exhausted this candidate, move to next in fallback chain

    raise last_error or ProviderError("All candidates exhausted", retryable=False)


def run_stream_with_failover(candidates: List[str], messages):
    """Streaming variant (generator of dict events). Because tokens are
    already flowing to the client, we can only fail over to the next
    candidate if a candidate fails before it has emitted any token. Once a
    stream has started, a mid-stream error is surfaced to the client rather
    than silently swapped.
    """
    cfg = settings.gateway_config
    timeout = cfg["timeouts"]["provider_request_timeout_seconds"]
    max_attempts = cfg["retries"]["max_attempts"]
    base_backoff = cfg["retries"]["base_backoff_seconds"]
    max_backoff = cfg["retries"]["max_backoff_seconds"]

    last_error = None
    for idx, model in enumerate(candidates):
        provider_name = provider_for(model)
        provider = get_provider(provider_name)
        fallback_used = idx > 0

        for attempt in range(1, max_attempts + 1):
            started = False
            chunks = []
            try:
                for chunk in provider.stream(model, messages, timeout=timeout):
                    started = True
                    chunks.append(chunk)
                    yield {"type": "chunk", "text": chunk, "model": model,
                           "provider": provider_name, "fallback_used": fallback_used}
                yield {"type": "done", "model": model, "provider": provider_name,
                       "fallback_used": fallback_used, "full_text": "".join(chunks)}
                return
            except ProviderError as e:
                last_error = e
                if started:
                    yield {"type": "error", "message": str(e)}
                    return
                if attempt == max_attempts:
                    break
                backoff = min(max_backoff, base_backoff * (2 ** (attempt - 1)))
                time.sleep(backoff)
        # try next candidate in chain

    yield {"type": "error", "message": str(last_error) if last_error else "All candidates exhausted"}
