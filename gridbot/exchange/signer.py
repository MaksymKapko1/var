"""Подпись запросов Omni API: Ed25519 над 7 строками (см. docs/api-specification/authentication)."""
import hashlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SCHEME = "variational-omni-api/v1"


def canonical_string(environment: str, public_key: str, ts_ms: int, method: str,
                     path_and_query: str, body: bytes) -> str:
    return "\n".join([SCHEME, environment, public_key, str(ts_ms), method.upper(),
                      path_and_query, hashlib.sha256(body).hexdigest()])


class Signer:
    def __init__(self, secret_hex: str, environment: str, expected_public_key: str = ""):
        raw = bytes.fromhex(secret_hex.strip().lower().removeprefix("0x"))
        if len(raw) == 64:  # seed + public
            raw = raw[:32]
        if len(raw) != 32:
            raise ValueError("VAR_API_SECRET должен быть 32-байтным Ed25519 приватным ключом в hex (64 символа)")
        self._key = Ed25519PrivateKey.from_private_bytes(raw)
        pub = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public_key = "0x" + pub.hex()
        self.environment = environment
        if expected_public_key and expected_public_key.lower() != self.public_key:
            raise ValueError(f"VAR_API_KEY {expected_public_key} не соответствует VAR_API_SECRET "
                             f"(из секрета выходит {self.public_key})")

    def headers(self, ts_ms: int, method: str, path_and_query: str, body: bytes) -> dict[str, str]:
        msg = canonical_string(self.environment, self.public_key, ts_ms, method, path_and_query, body)
        return {
            "x-variational-key": self.public_key,
            "x-request-timestamp-ms": str(ts_ms),
            "x-variational-signature": "0x" + self._key.sign(msg.encode()).hex(),
        }
