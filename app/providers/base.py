"""
Provider adapter interface.

Every provider (mock or real) implements `complete()` (blocking, returns a
CompletionResult) and `stream()` (a generator yielding text chunks). The
gateway core never talks to a provider SDK directly -- it only calls through
this interface, so a real OpenAI/Anthropic HTTP adapter can be dropped in
later without touching routing, retries, budgets, or caching logic.

Synchronous + generator-based rather than asyncio because the gateway runs
on Flask's threaded dev server here (concurrency comes from OS threads, one
per in-flight request), which keeps the whole codebase dependency-free.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator, List, Dict


@dataclass
class CompletionResult:
    text: str
    input_tokens: int
    output_tokens: int


class ProviderError(Exception):
    """Raised for any upstream failure: outage, rate limit, timeout, degraded."""
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class ProviderAdapter(ABC):
    name: str

    @abstractmethod
    def complete(self, model: str, messages: List[Dict], timeout: float) -> CompletionResult:
        ...

    @abstractmethod
    def stream(self, model: str, messages: List[Dict], timeout: float) -> Iterator[str]:
        """Yield response text incrementally (word chunks)."""
        ...
