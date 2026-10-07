"""Password / token hashing helpers.

We deliberately stick to the standard library (PBKDF2-HMAC-SHA256) so the
relay has no native build dependencies.  Secrets are stored as::

    pbkdf2_sha256$<iterations>$<base64 salt>$<base64 derived key>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

_ALGORITHM = "pbkdf2_sha256"
_DEFAULT_ITERATIONS = 240_000


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def hash_secret(secret: str, *, iterations: int = _DEFAULT_ITERATIONS) -> str:
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations)
    return f"{_ALGORITHM}${iterations}${_b64e(salt)}${_b64e(derived)}"


def verify_secret(secret: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != _ALGORITHM:
            return False
        iterations = int(iterations_text)
        salt = _b64d(salt_text)
        expected = _b64d(digest_text)
    except (ValueError, TypeError):
        return False
    derived = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(derived, expected)


def new_token(nbytes: int = 32) -> str:
    """Generate a fresh agent registration token.

    Prefixed so it never looks like a command-line option and is easy to spot.
    """
    return "xq_" + secrets.token_urlsafe(nbytes)
