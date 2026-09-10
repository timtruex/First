"""Request signing: right algorithm, right message."""
import base64

import pytest

from kalshi_auth import KalshiAuthError, KalshiSigner, signing_path

crypto = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return key, pem


def test_query_string_is_excluded_from_the_signed_path():
    """Signing the query string is a common cause of opaque 401s."""
    assert signing_path("https://api.elections.kalshi.com/trade-api/v2/markets?limit=5") \
        == "/trade-api/v2/markets"
    assert signing_path("/trade-api/v2/portfolio/orders") == "/trade-api/v2/portfolio/orders"


def test_signature_verifies_over_timestamp_method_path(keypair):
    key, pem = keypair
    signer = KalshiSigner.from_pem("kid", pem)
    headers = signer.headers("get", "/trade-api/v2/markets?limit=5", timestamp_ms="1700000000000")

    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        b"1700000000000GET/trade-api/v2/markets",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )


def test_uses_pss_not_pkcs1v15(keypair):
    """A PKCS#1 v1.5 signature is well-formed and rejected by Kalshi."""
    key, pem = keypair
    signer = KalshiSigner.from_pem("kid", pem)
    sig = base64.b64decode(signer.sign_message("msg"))
    with pytest.raises(Exception):
        key.public_key().verify(sig, b"msg", padding.PKCS1v15(), hashes.SHA256())


def test_method_is_upper_cased(keypair):
    """
    PSS is randomised, so two signatures of the same message differ. The
    property to check is that both verify against the UPPERCASE message.
    """
    key, pem = keypair
    signer = KalshiSigner.from_pem("kid", pem)
    for method in ("get", "GET", "GeT"):
        headers = signer.headers(method, "/x", timestamp_ms="1")
        key.public_key().verify(
            base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
            b"1GET/x",
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )


def test_pss_salt_makes_signatures_non_deterministic(keypair):
    _, pem = keypair
    signer = KalshiSigner.from_pem("kid", pem)
    assert signer.sign_message("same") != signer.sign_message("same")


def test_all_required_headers_present(keypair):
    _, pem = keypair
    headers = KalshiSigner.from_pem("kid", pem).headers("GET", "/x")
    assert headers["KALSHI-ACCESS-KEY"] == "kid"
    assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()
    assert len(headers["KALSHI-ACCESS-TIMESTAMP"]) == 13, "milliseconds, not seconds"


def test_missing_key_id_rejected(keypair):
    _, pem = keypair
    with pytest.raises(KalshiAuthError, match="key_id"):
        KalshiSigner.from_pem("", pem)


def test_garbage_key_rejected():
    with pytest.raises(KalshiAuthError, match="could not load"):
        KalshiSigner.from_pem("kid", b"not a pem")


def test_missing_key_file_rejected():
    with pytest.raises(KalshiAuthError, match="not found"):
        KalshiSigner.from_file("kid", "/nonexistent/key.pem")


def test_non_rsa_key_rejected():
    from cryptography.hazmat.primitives.asymmetric import ed25519
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(KalshiAuthError, match="RSA"):
        KalshiSigner.from_pem("kid", pem)
