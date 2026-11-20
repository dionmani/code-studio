"""Sealing of stored Git tokens (stdlib only, no native extensions).

Encrypt-then-MAC: a keystream from HMAC-SHA256(enc_key, nonce || counter)
(HMAC used as a PRF in counter mode) is XORed with the token, and the result is
authenticated with HMAC-SHA256(mac_key, version || nonce || ciphertext). Both
keys come from ``<data dir>/credentials.key``, which is kept separate from the
database.

What this protects against: a copied or backed-up database (db.sqlite3) does
not reveal tokens. What it does not protect against: someone who can read the
whole data directory (key + database), e.g. a root user on the server.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from django.conf import settings

from cs_common.datadir import read_or_create_secret

_VERSION = b"v1"


def _keys() -> tuple[bytes, bytes]:
    master = read_or_create_secret(settings.DATA_DIR / "credentials.key", 48).encode()
    return (hmac.new(master, b"code-studio git token enc", hashlib.sha256).digest(),
            hmac.new(master, b"code-studio git token mac", hashlib.sha256).digest())


def _stream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def seal(plaintext: str) -> str:
    enc_key, mac_key = _keys()
    data = plaintext.encode("utf-8")
    nonce = os.urandom(16)
    ct = bytes(a ^ b for a, b in zip(data, _stream(enc_key, nonce, len(data))))
    tag = hmac.new(mac_key, _VERSION + nonce + ct, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(_VERSION + nonce + ct + tag).decode("ascii")


class SealError(ValueError):
    pass


def unseal(sealed: str) -> str:
    enc_key, mac_key = _keys()
    try:
        raw = base64.urlsafe_b64decode(sealed.encode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise SealError("malformed") from exc
    if len(raw) < 2 + 16 + 32 or raw[:2] != _VERSION:
        raise SealError("malformed")
    nonce, ct, tag = raw[2:18], raw[18:-32], raw[-32:]
    if not hmac.compare_digest(tag, hmac.new(mac_key, _VERSION + nonce + ct, hashlib.sha256).digest()):
        raise SealError("token could not be decrypted (credentials.key changed?)")
    return bytes(a ^ b for a, b in zip(ct, _stream(enc_key, nonce, len(ct)))).decode("utf-8")
