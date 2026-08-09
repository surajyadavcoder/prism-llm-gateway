"""
Virtual keys are never stored in plaintext. We store a SHA-256 hash and a
short prefix (for display/debugging in the ops console, e.g. "sk-prism-...a1b2").
The plaintext key only ever exists in the client's Authorization header and
in data/seed_keys.json (the demo credentials handed to users) -- never in
the database or in request_logs.
"""
import hashlib


def hash_key(plaintext_key: str) -> str:
    return hashlib.sha256(plaintext_key.encode("utf-8")).hexdigest()


def key_prefix(plaintext_key: str, visible: int = 12) -> str:
    if len(plaintext_key) <= visible:
        return plaintext_key
    return plaintext_key[:visible] + "..." + plaintext_key[-4:]
