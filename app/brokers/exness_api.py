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
import re
import threading
import time
import uuid
from collections import deque

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


class RateLimiter:
    """Client-side sliding-window limiter fed by GET .../limits (the server returns configured limits,
    not remaining quota, so the client must count its own requests)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.global_rule: tuple[int, int] | None = None
        self.rules: list[tuple[str, re.Pattern, str, int, int]] = []  # (method, path_re, op_id, limit, window)
        self.hist: dict[str, deque] = {}

    def load(self, limits: dict) -> None:
        rest = (limits.get("limits") or limits).get("rest", {})
        g = rest.get("global_account_rate")
        if g:
            self.global_rule = (int(g["limit"]), int(g["window_seconds"]))
        for m in rest.get("methods", []):
            rl = m.get("rate_limit") or {}
            if not rl:
                continue
            pat = re.compile("^" + re.sub(r"\{[^}]+\}", r"[^/]+", m["path"]) + "$")
            self.rules.append((m["http_method"].upper(), pat, m["api_operation_id"], int(rl["limit"]), int(rl["window_seconds"])))

    def _wait_for(self, key: str, limit: int, window: int, now: float) -> float:
        q = self.hist.setdefault(key, deque())
        while q and now - q[0] >= window:
            q.popleft()
        # keep a 10% safety margin
        cap = max(1, int(limit * 0.9))
        return 0.0 if len(q) < cap else window - (now - q[0]) + 0.05

    def acquire(self, method: str, path: str) -> None:
        path = path.split("?", 1)[0]
        rule = next((r for r in self.rules if r[0] == method and r[1].match(path)), None)
        while True:
            with self.lock:
                now = time.time()
                waits = []
                if self.global_rule:
                    waits.append(self._wait_for("__global__", *self.global_rule, now))
                if rule:
                    waits.append(self._wait_for(rule[2], rule[3], rule[4], now))
                w = max(waits, default=0.0)
                if w <= 0:
                    if self.global_rule:
                        self.hist["__global__"].append(now)
                    if rule:
                        self.hist[rule[2]].append(now)
                    return
            time.sleep(min(w, 5.0))

    def summary(self) -> dict:
        return {"global": self.global_rule, "methods": {r[2]: f"{r[3]}/{r[4]}s" for r in self.rules}}


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
        self._discovery_blocked_until = 0.0
        self.limiter = RateLimiter()

    def _request(self, method: str, path: str, body: dict | None = None, idempotent: bool = False,
                 host: str | None = None) -> dict:
        host = host or self.base_url or self.resolve_access_point()
        if host != self.gateway:
            self.limiter.acquire(method.upper(), path)
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
            raise ExnessError(r.status_code, data.get("code") if isinstance(data, dict) else None, data)
        return data

    # --- confirmed endpoints --------------------------------------------------
    def resolve_access_point(self) -> str:
        """Discovery is rate-limited: resolve once, then cache. Honours retry_after on 429."""
        if self.base_url:
            return self.base_url
        wait = self._discovery_blocked_until - time.time()
        if wait > 0:
            raise RuntimeError(f"Discovery rate-limited — retry in {int(wait)}s")
        try:
            d = self._request("GET", f"/v1/trading/access-point?account_id={self.account_id}", host=self.gateway)
        except ExnessError as e:
            if e.status == 429:
                ra = (e.data or {}).get("retry_after") if isinstance(e.data, dict) else None
                try:
                    from datetime import datetime
                    until = datetime.fromisoformat(str(ra).replace("Z", "+00:00")).timestamp()
                except Exception:
                    until = time.time() + 60
                self._discovery_blocked_until = max(until, time.time() + 10)
            raise
        ap = d.get("access_point")
        if not isinstance(ap, str) or not ap.startswith("https://"):
            raise RuntimeError("رد الاكتشاف لا يحتوي access_point صالح")
        self.base_url = ap.rstrip("/")
        return self.base_url

    def account_config(self) -> dict:
        return self._request("GET", f"/v1/configuration/accounts/{self.account_id}/account")

    def rate_limits(self) -> dict:
        return self._request("GET", f"/v1/configuration/accounts/{self.account_id}/limits")

    def load_limits(self) -> dict:
        data = self.rate_limits()
        self.limiter.load(data)
        return self.limiter.summary()

    def ws_events_path(self) -> str:
        return f"/v1/server-events/accounts/{self.account_id}/ws/events"

    # --- trading (confirmed by "Place Your First Trade") ------------------------
    # Trading is ASYNCHRONOUS: the POST returns an ACK {"operation_id","client_request_id",
    # "status":"accepted"}; the final result arrives as a `transaction_event` on the server-events
    # WebSocket (or via operation status). An ACK is NOT a fill — never report success on it.
    def signed_json(self, method: str, path_with_query: str, body: dict | None, idempotency_key: str) -> dict:
        """Mutating signed request (idempotency key 5..128 chars). Returns the ACK."""
        if not 5 <= len(idempotency_key) <= 128:
            raise ValueError("idempotency key must be 5..128 chars")
        base = self.base_url or self.resolve_access_point()
        self.limiter.acquire(method.upper(), path_with_query)
        raw = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
        h = signed_headers(self.api_key, self.key, method, path_with_query, raw, idempotency_key)
        if body is not None:
            h["Content-Type"] = "application/json"
        r = self.http.request(method, base + path_with_query, content=raw or None, headers=h)
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            raise ExnessError(r.status_code, data.get("code"), data)
        return data  # ACK only — not a fill

    def open_position(self, instrument: str, side: str, volume: str, client_request_id: str,
                      stop_loss_price: str | None = None, take_profit_price: str | None = None,
                      comment: str | None = None) -> dict:
        side = side.lower()
        if side not in ("buy", "sell"):
            raise ValueError("side يجب أن يكون buy أو sell")
        body = {"instrument": instrument, "side": side, "volume": volume}
        if stop_loss_price:
            body["stop_loss_price"] = stop_loss_price
        if take_profit_price:
            body["take_profit_price"] = take_profit_price
        if comment:
            body["comment"] = comment
        return self.signed_json("POST", f"/v1/trading/accounts/{self.account_id}/positions", body, client_request_id)

    def close_position(self, position_id: str, client_request_id: str, volume: str | None = None) -> dict:
        q = f"?volume={volume}" if volume else ""
        return self.signed_json("DELETE", f"/v1/trading/accounts/{self.account_id}/positions/{position_id}{q}",
                                None, client_request_id)

    def snapshot(self) -> dict:
        """Active orders, open positions and account state."""
        return self._request("GET", f"/v1/trading/accounts/{self.account_id}/snapshot")

    def operation_status(self, operation_id: str) -> dict:
        """pending | confirmed | rejected | failed. Retained 24 h. Recovery path when the
        transaction_event is not received in time."""
        return self._request("GET", f"/v1/trading/accounts/{self.account_id}/operations/{operation_id}")

    def history_deals(self) -> dict:
        return self._request("GET", f"/v1/history/accounts/{self.account_id}/deals")



ERROR_HINTS = {
    4001: "MARKET_TRADE_DISABLED — التداول مغلق للحساب أو الأداة",
    4002: "MARKET_CLOSE_ONLY — مسموح الإغلاق فقط",
    5000: "TRADING_RULE_INVALID_VOLUME — الحجم خارج الحدود أو الخطوة",
    5005: "TRADING_RULE_INVALID_PRICE_LEVELS — مستويات الوقف/الهدف غير صالحة",
    6000: "EXECUTION_REQUOTE — إعادة تسعير، أعد المحاولة فقط والسوق مفتوح",
    6001: "EXECUTION_REJECTED — رُفض التنفيذ، انتظر سعراً جديداً",
}


class ExnessError(RuntimeError):
    def __init__(self, status: int, code, data: dict) -> None:
        self.status, self.code, self.data = status, code, data
        hint = ERROR_HINTS.get(int(code)) if str(code).isdigit() else None
        msg = hint or (data.get("error_message") if isinstance(data, dict) else None) or data
        super().__init__(f"Exness {status} {code}: {msg}")


def ticks_subscription(instruments: list[str], req_id: str) -> dict:
    """Confirmed payload from 'API Configuration and Limits'."""
    return {"id": req_id, "subscribe": {"event": "ticks", "instruments": instruments}}
