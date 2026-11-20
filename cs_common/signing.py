"""HMAC request signing for IDE -> Agent Engine calls (contract v1).

Signature = HMAC-SHA256(token, "{ts}\\n{METHOD}\\n{raw_path_with_query}\\n{sha256(body)}")
Headers: X-CS-Timestamp, X-CS-Signature, X-CS-Api-Version.
Requests older/newer than ``MAX_SKEW_SECONDS`` are rejected (replay window).
"""
from __future__ import annotations

import hashlib
import hmac
import time

API_VERSION = "1"
HEADER_TS = "X-CS-Timestamp"
HEADER_SIG = "X-CS-Signature"
HEADER_VERSION = "X-CS-Api-Version"
MAX_SKEW_SECONDS = 300


def _message(ts: str, method: str, path: str, body: bytes) -> bytes:
    digest = hashlib.sha256(body or b"").hexdigest()
    return f"{ts}\n{method.upper()}\n{path}\n{digest}".encode("utf-8")


def sign(token: str, method: str, path: str, body: bytes = b"", ts: int | None = None) -> dict[str, str]:
    ts_s = str(int(ts if ts is not None else time.time()))
    sig = hmac.new(token.encode("utf-8"), _message(ts_s, method, path, body), hashlib.sha256).hexdigest()
    return {HEADER_TS: ts_s, HEADER_SIG: sig, HEADER_VERSION: API_VERSION}


def verify(token: str, method: str, path: str, body: bytes, ts: str | None, sig: str | None,
           now: float | None = None) -> bool:
    if not token or not ts or not sig:
        return False
    try:
        ts_i = int(ts)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts_i) > MAX_SKEW_SECONDS:
        return False
    expected = hmac.new(token.encode("utf-8"), _message(ts, method, path, body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)
