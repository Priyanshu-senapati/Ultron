"""HashAnonymizer — deterministic hashing of LOCAL_ONLY fields.

For data that's safe to share *as long as the original value can't be
recovered* (e.g. focus_app names for Ghost Network peer correlation),
hash the value with a per-machine salt. The same input always produces
the same output on this machine, but the salt is never exported, so
peers can't reverse-engineer values.

Uses BLAKE3 if available (the same hash family ULTRON's quantum log
uses); falls back to BLAKE2b from stdlib.
"""
from __future__ import annotations

import hashlib
from typing import Iterable

try:
    from blake3 import blake3 as _blake3  # type: ignore[import-not-found]
    _HAS_BLAKE3 = True
except ImportError:  # pragma: no cover — covered when the wheel is missing
    _blake3 = None
    _HAS_BLAKE3 = False


class HashAnonymizer:
    def __init__(self, salt: str) -> None:
        if not salt:
            raise ValueError("salt must not be empty")
        self._salt_bytes = salt.encode("utf-8")

    def hash_value(self, value: str) -> str:
        """Return first 16 hex chars of `BLAKE3(salt || value)`.

        16 hex = 64 bits = enough entropy to avoid practical collisions
        in the size of any single user's data; short enough to be readable
        in logs.
        """
        if not isinstance(value, str):
            value = str(value)
        payload = self._salt_bytes + value.encode("utf-8")
        if _HAS_BLAKE3:
            digest = _blake3(payload).hexdigest()  # type: ignore[union-attr]
        else:
            digest = hashlib.blake2b(payload, digest_size=16).hexdigest()
        return digest[:16]

    def redact_dict(self, data: dict, local_only_keys: Iterable[str]) -> dict:
        """Return a new dict with `local_only_keys` values replaced by hash.

        Other keys are passed through unchanged. Operates one level deep;
        callers that need recursion can call repeatedly.
        """
        keys = {k.lower() for k in local_only_keys}
        out: dict = {}
        for key, value in data.items():
            if key.lower() in keys and isinstance(value, str):
                out[key] = f"hash:{self.hash_value(value)}"
            elif isinstance(value, dict):
                out[key] = self.redact_dict(value, local_only_keys)
            else:
                out[key] = value
        return out

    def redact_string(self, text: str) -> str:
        """Replace the entire string with its hash. Used when a free-text
        field contains LOCAL_ONLY data and we still want to send a
        deterministic identifier instead of dropping the field entirely."""
        return f"hash:{self.hash_value(text)}"
