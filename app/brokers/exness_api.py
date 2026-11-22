"""Exness Trader API client — only what the public docs confirm.

Confirmed (exness-api.com docs, via their published llms.txt):
  * Signing v1: headers EXN-API-KEY, EXN-IDEMPOTENCY-KEY, EXN-TIMESTAMP (ms), EXN-SIGN-VERSION=1,
    EXN-DATA (base64url JSON, no padding), EXN-SIGN (Ed25519 over the decoded payload, base64url).
    Payload fields: api_key, idempotency_key, timestamp, sign_version, method, path (+query), body_hash
    where body_hash = base64url(SHA-256(body bytes)).
  * GET  /v1/configuration/accounts/{account_id}/account   (currency, leverage, margin_call_level, stop_out_level)
  * GET  /v1/configuration/accounts/{account_id}/limits    (per-method rate limits)
  * POST /v1/trading/accounts/{account_id}/orders          (body schema NOT in the public excerpt)
  * GET  /v1/trading/access-point?account_id=...           (host discovery)
  * WS   /v1/server-events/accounts/{account_id}/ws/events  (transactions, instruments, hmr, ticks, account_state)

Anything not in that list is deliberately NOT implemented until the official schema is provided.
The private key stays on the machine running this code (EXN_PRIVATE_KEY_PATH). Never paste it in chat.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

EMPTY_BODY_HASH = "47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU"


def b64url_nopad(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def load_private_key(text: str) -> Ed25519PrivateKey:
    """Accepts what Key Management gives you: 128 hex chars (32-byte seed + 32-byte public
    key), or a PKCS#8 PEM. Checks that the public half matches, as the official example does."""
    t = text.strip()
    if t.startswith("-----BEGIN"):
        key = serialization.load_pem_private_key(t.encode(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("المفتاح ليس Ed25519")
        return key
    if len(t) != 128 or any(c not in "0123456789abcdefABCDEF" for c in t):
        raise ValueError("المفتاح السري يجب أن يكون 128 حرف hex أو PEM")
    raw = bytes.fromhex(t)
    key = Ed25519PrivateKey.from_private_bytes(raw[:32])
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if pub != raw[32:]:
        raise ValueError("النصف العام من المفتاح لا يطابق — تأكد من نسخه كاملاً")
    return key


def signed_headers(api_key: str, key: Ed25519PrivateKey, method: str, path_with_query: str,
                   body: bytes = b"", idempotency_key: str = "", ts_ms: int | None = None) -> dict:
    ts = ts_ms if ts_ms is not None else int(time.time() * 1000)
    payload = {
        "api_key": api_key,
        "idempotency_key": idempotency_key,
        "timestamp": ts,
        "sign_version": 1,
        "method": method.upper(),
        "path": path_with_query,
        "body_hash": b64url_nopad(hashlib.sha256(body).digest()),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return {
        "EXN-API-KEY": api_key,
        "EXN-IDEMPOTENCY-KEY": idempotency_key,
        "EXN-TIMESTAMP": str(ts),
        "EXN-SIGN-VERSION": "1",
        "EXN-DATA": b64url_nopad(raw),
        "EXN-SIGN": b64url_nopad(key.sign(raw)),
    }


class ExnessClient:
    def __init__(self, api_key: str, private_key: str, account_id: str, gateway_url: str,
                 timeout: float = 15.0) -> None:
        if not str(account_id).isdigit():
            raise ValueError("EXN_ACCOUNT_ID يجب أن يكون رقماً")
        if not gateway_url.startswith("https://"):
            raise ValueError("EXN_DISCOVERY_BASE_URL يجب أن يبدأ بـ https:// (من DNS widget في Key Management)")
        self.api_key, self.account_id = api_key, str(account_id)
        self.gateway = gateway_url.rstrip("/")
        self.key = load_private_key(private_key)
        # never follow redirects with signed headers (as in the official example)
        self.http = httpx.Client(timeout=timeout, follow_redirects=False)
        self.base_url: str | None = None  # Access Point, resolved once and cached

    def _request(self, method: str, path: str, body: dict | None = None, idempotent: bool = False,
                 host: str | None = None) -> dict:
        host = host or self.base_url or self.resolve_access_point()
        raw = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
        idem = uuid.uuid4().hex if idempotent else ""
        h = signed_headers(self.api_key, self.key, method, path, raw, idem)
        if body is not None:
            h["Content-Type"] = "application/json"
        r = self.http.request(method, host + path, content=raw or None, headers=h)
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text}
        if r.status_code >= 400:
            raise RuntimeError(f"Exness {r.status_code} {path}: {data}")
        return data

    # --- confirmed endpoints --------------------------------------------------
    def resolve_access_point(self) -> str:
        """Discovery is rate-limited: resolve once, then cache."""
        d = self._request("GET", f"/v1/trading/access-point?account_id={self.account_id}", host=self.gateway)
        ap = d.get("access_point")
        if not isinstance(ap, str) or not ap.startswith("https://"):
            raise RuntimeError("رد الاكتشاف لا يحتوي access_point صالح")
        self.base_url = ap.rstrip("/")
        return self.base_url

    def account_config(self) -> dict:
        return self._request("GET", f"/v1/configuration/accounts/{self.account_id}/account")

    def rate_limits(self) -> dict:
        return self._request("GET", f"/v1/configuration/accounts/{self.account_id}/limits")

    def ws_events_path(self) -> str:
        return f"/v1/server-events/accounts/{self.account_id}/ws/events"

    # --- awaiting official schema --------------------------------------------
    def open_position(self, **_):
        raise NotImplementedError("صيغة جسم أمر الفتح غير منشورة في المقتطف العام — بانتظار الوثيقة الرسمية")
