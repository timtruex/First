"""
Kalshi API request signing.

Kalshi authenticates with an RSA key pair: you register a public key and sign
each request with the private half. The signature covers

    timestamp_ms + HTTP_METHOD + request_path

and travels in three headers alongside the key id.

Two details are easy to get wrong and both fail as opaque 401s:

  - The signature uses RSA-PSS with SHA-256 and a salt length equal to the
    digest length, not PKCS#1 v1.5. A v1.5 signature is well-formed and will
    be rejected.
  - The signed path is the path only — no scheme, no host, and no query
    string. Signing the full URL produces a valid signature over the wrong
    message.

The timestamp is milliseconds since epoch and Kalshi rejects skewed clocks, so
a persistent 401 on correct code is worth checking against NTP before
debugging the signature.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    _CRYPTO_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only without a working dep
    # Deliberately broad: a cryptography install with a missing or mismatched
    # native backend raises a pyo3 PanicException, not ImportError, and an
    # uncaught panic here takes down the whole scanner including the read-only
    # paths that never need signing.
    _CRYPTO_AVAILABLE = False

logger = logging.getLogger(__name__)


class KalshiAuthError(RuntimeError):
    """Raised when credentials are missing or a key cannot be loaded."""


def signing_path(url_or_path: str) -> str:
    """
    Reduce a URL to the exact path Kalshi expects in the signed message.

    Query strings are excluded — including them is a common cause of 401s that
    look like key problems.
    """
    parts = urlsplit(url_or_path)
    return parts.path or url_or_path


@dataclass
class KalshiSigner:
    """Signs requests with a registered RSA private key."""

    key_id: str
    private_key: "rsa.RSAPrivateKey"

    @classmethod
    def from_pem(
        cls, key_id: str, pem_data: bytes, *, password: bytes | None = None
    ) -> "KalshiSigner":
        if not _CRYPTO_AVAILABLE:
            raise KalshiAuthError(
                "the 'cryptography' package is required for Kalshi request signing"
            )
        if not key_id:
            raise KalshiAuthError("key_id is required")
        try:
            key = serialization.load_pem_private_key(pem_data, password=password)
        except Exception as exc:
            raise KalshiAuthError(f"could not load private key: {exc}") from exc
        if not isinstance(key, rsa.RSAPrivateKey):
            raise KalshiAuthError("Kalshi requires an RSA private key")
        return cls(key_id=key_id, private_key=key)

    @classmethod
    def from_file(
        cls, key_id: str, path: str | Path, *, password: bytes | None = None
    ) -> "KalshiSigner":
        p = Path(path).expanduser()
        if not p.exists():
            raise KalshiAuthError(f"private key file not found: {p}")
        return cls.from_pem(key_id, p.read_bytes(), password=password)

    # ------------------------------------------------------------------

    @staticmethod
    def _timestamp_ms() -> str:
        return str(int(time.time() * 1000))

    def sign_message(self, message: str) -> str:
        """RSA-PSS / SHA-256 signature, base64-encoded."""
        signature = self.private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def headers(self, method: str, url_or_path: str, *, timestamp_ms: str | None = None) -> dict[str, str]:
        """Auth headers for one request."""
        ts = timestamp_ms or self._timestamp_ms()
        path = signing_path(url_or_path)
        message = f"{ts}{method.upper()}{path}"
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": self.sign_message(message),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
