"""
Lightweight semantic similarity used for the cache and (optionally) routing.

We avoid a network call to a real embeddings API so the gateway stays fully
offline-testable with mock providers. Instead we use a hashed bag-of-words
vector (a mini "feature hashing" embedding, the same trick used by Vowpal
Wabbit): each token is hashed into one of N buckets and the bucket counts,
L2-normalized, form the vector. Cosine similarity on these vectors catches
paraphrases that share vocabulary ("cancel my order" vs "I want to cancel
my order") much better than exact string matching, while staying dependency
-free. Swapping in a real embeddings endpoint later only touches this file.
"""
import re
import math
import hashlib
from typing import List

VECTOR_DIM = 256

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "to", "of", "and",
    "in", "on", "for", "with", "my", "me", "i", "you", "your", "please",
    "can", "could", "would", "do", "does", "it", "this", "that",
}


def _tokenize(text: str) -> List[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    # Drop 1-char tokens (mostly artifacts of possessives like "France's" ->
    # "france","s") and stopwords -- both just dilute the vector for short
    # queries without adding signal.
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 1]


def embed(text: str) -> List[float]:
    vec = [0.0] * VECTOR_DIM
    for tok in _tokenize(text):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        idx = h % VECTOR_DIM
        sign = 1.0 if (h // VECTOR_DIM) % 2 == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def cosine(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))
