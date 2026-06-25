"""Password hashing and token helpers — stdlib only, no third-party crypto.

Passwords use PBKDF2-HMAC-SHA256 with a per-password random salt, stored as a
single self-describing string ``pbkdf2$<iter>$<salt_b64>$<hash_b64>`` so the
work factor can evolve without a schema change. Tokens (session + per-user API)
are URL-safe random strings from ``secrets``.

These functions are dependency-free and side-effect-free so they unit-test
without a database or network, mirroring ``stats.py`` / ``bonn_traffic.py``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

PBKDF2_ITERATIONS = 600_000
_ALGO = "sha256"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s.encode("ascii"))


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Return ``pbkdf2$<iter>$<salt>$<hash>`` for ``password``."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"), salt, iterations)
    return f"pbkdf2${iterations}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a stored hash string."""
    try:
        algo, iter_s, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2":
            return False
        iterations = int(iter_s)
        salt = _unb64(salt_b64)
        expected = _unb64(hash_b64)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(dk, expected)


def new_token(nbytes: int = 32) -> str:
    """A URL-safe random token (session id / API token)."""
    return secrets.token_urlsafe(nbytes)
