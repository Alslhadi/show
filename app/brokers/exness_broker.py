"""Exness Trader API broker adapter, built from the official OpenAPI spec (trading.public.yaml v1.0.2).

Key rules from the spec:
  * Every account-scoped call goes to the Access Point resolved via the Gateway (cached).
  * Open position = POST .../positions with optional stop_loss_price / take_profit_price.
  * Trading is async: ACK first, final result via operation status / transaction_event.
    We only report success when the operation status is `confirmed`.
  * Prices/volumes are decimal STRINGS; prices use at most `point_digits` decimals;
    volume must satisfy volume_min / volume_max / volume_step.
  * Candles: GET /v1/market-data/accounts/{id}/candles (from + count, count may be negative).
  * Live ticks: separate WebSocket /v1/server-events/accounts/{id}/ws/ticks (handshake signed as GET).
"""
from __future__ import annotations

import json
import math
import threading
import time
import uuid
from datetime import datetime, timezone

import pandas as pd

from ..config import settings
from ..risk import AccountState, SymbolSpec
from .base import TIMEFRAMES, Broker
from .exness_api import ExnessClient, ExnessError, signed_headers, ticks_subscription


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _dec(x) -> float:
    return float(x) if x not in (None, "") else 0.0


class ExnessBroker(Broker):
    name = "EXNESS"
    is_simulation = False

    def __init__(self) -> None:
        missing = [k for k in ("exn_api_key", "exn_private_key", "exn_account_id", "exn_discovery_base_url")
                   if not getattr(settings, k)]
        if missing:
            raise RuntimeError("إعدادات Exness ناقصة: " + ", ".join(m.upper() for m in missing))
        self.c = ExnessClient(settings.exn_api_key, settings.exn_private_key, settings.exn_account_id,
                              settings.exn_discovery_base_url)
        self._ok = False
        self._details: dict = {}
        self._specs: dict[str, SymbolSpec] = {}
        self._digits: dict[str, int] = {}
        self._contract: dict[str, float] = {}
        self._ticks: dict[str, dict] = {}
        self._candles: dict[tuple, pd.DataFrame] = {}
        self._snap: dict = {}
        self._snap_t = 0.0
        self._sent: dict[str, dict] = {}
        self._hist: list | None = None
        self._hist_t = 0.0
        self._ws_thread: threading.Thread | None = None
        self.last_error = ""

    # ---- connection -----------------------------------------------------------
    def connect(self) -> bool:
        try:
            self.c.resolve_access_point()
            if not self.c.limiter.rules:
                try:
                    print("EXNESS_LIMITS", json.dumps(self.c.load_limits(), ensure_ascii=False), flush=True)
                except Exception as e:  # keep working with conservative pacing
                    print("EXNESS_LIMITS_UNAVAILABLE", e, flush=True)
                    self.c.limiter.global_rule = (60, 60)
            self._details = self.c.account_config()
            self._ok = True
            self._start_ticks()
        except Exception as e:
            self._ok, self.last_error = False, str(e)
        return self._ok

    def connected(self) -> bool:
        return self._ok

    def account_details(self) -> dict:
        return self._details

    def is_demo(self) -> bool:
        name = str(self._details.get("account_name", "")).lower()
        return "demo" in name

    # ---- ticks over WebSocket ---------------------------------------------------
    def _start_ticks(self) -> None:
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._ws_thread = threading.Thread(target=self._ticks_loop, daemon=True)
        self._ws_thread.start()

    def _ticks_loop(self) -> None:
        from websockets.sync.client import connect as ws_connect
        path = f"/v1/server-events/accounts/{self.c.account_id}/ws/ticks"
        while True:
            try:
                base = self.c.base_url or self.c.resolve_access_point()
                url = base.replace("https://", "wss://", 1) + path
                h = signed_headers(self.c.api_key, self.c.key, "GET", path, b"", "")
                with ws_connect(url, additional_headers=h, open_timeout=15) as ws:
                    ws.send(json.dumps(ticks_subscription(settings.symbol_list, "ticks-" + uuid.uuid4().hex[:8])))
                    for raw in ws:
                        m = json.loads(raw)
                        if m.get("instrument") and m.get("bid") is not None:
                            self._ticks[m["instrument"]] = {"bid": float(m["bid"]), "ask": float(m["ask"]),
                                                            "time": _epoch(m["timestamp"]) if m.get("timestamp") else time.time()}
            except Exception as e:
                self.last_error = f"ticks ws: {e}"
                time.sleep(5)

    # ---- data ----------------------------------------------------------------
    def _snapshot(self, max_age: float = 3.0) -> dict:
        if time.time() - self._snap_t > max_age:
            self._snap = self.c.snapshot()
            self._snap_t = time.time()
        return self._snap

    def account(self) -> AccountState:
        st = self._snapshot()["account_state"]
        eq, used = _dec(st["equity"]), _dec(st["used_margin"])
        cur = (self._details.get("settings") or {}).get("currency", "USD")
        return AccountState(balance=_dec(st["balance"]), equity=eq, margin_free=eq - used, currency=cur)

    def spec(self, symbol: str) -> SymbolSpec:
        if symbol not in self._specs:
            cnd = self.c._request("GET", f"/v1/configuration/accounts/{self.c.account_id}/instruments/{symbol}/conditions")
            d = int(cnd.get("point_digits", 5))
            contract = _dec(cnd.get("contract_size"))
            cur = (self._details.get("settings") or {}).get("currency", "USD")
            if cnd.get("quote_currency") and cnd["quote_currency"] != cur:
                raise RuntimeError(f"{symbol}: عملة التسعير {cnd['quote_currency']} تختلف عن عملة الحساب — حساب اللوت غير مدعوم")
            tick = 10 ** -d
            self._digits[symbol], self._contract[symbol] = d, contract
            self._specs[symbol] = SymbolSpec(
                name=symbol, digits=d, point=tick, tick_size=tick, tick_value=tick * contract,
                volume_min=_dec(cnd.get("volume_min")) or 0.01, volume_max=_dec(cnd.get("volume_max")) or 100,
                volume_step=_dec(cnd.get("volume_step")) or 0.01, stops_level=0,
                trade_allowed=(self._details.get("settings") or {}).get("trade_mode", "enabled") == "enabled")
        return self._specs[symbol]

    def tick(self, symbol: str) -> dict:
        t = self._ticks.get(symbol)
        if t and time.time() - t["time"] < 120:
            return t
        # fallback: last closed S1 candle with bid+ask
        r = self.c._request("GET", f"/v1/market-data/accounts/{self.c.account_id}/candles?instrument={symbol}"
                                   f"&timeframe=S5&price_type=both&from={_iso(time.time())}&count=-1")
        c = (r.get("candles") or [None])[-1]
        if not c:
            raise RuntimeError(f"لا يوجد سعر لـ {symbol}")
        return {"bid": float(c["bid"]["close"]), "ask": float(c["ask"]["close"]), "time": _epoch(c["time"])}

    def candles(self, symbol: str, tf: str, n: int = 300) -> pd.DataFrame:
        sec = TIMEFRAMES[tf] * 60
        key = (symbol, tf)
        cached = self._candles.get(key)
        now = time.time()
        if cached is not None and len(cached) >= n and now < cached["time"].iloc[-1] + 2 * sec:
            return cached.iloc[-n:].reset_index(drop=True)
        r = self.c._request("GET", f"/v1/market-data/accounts/{self.c.account_id}/candles?instrument={symbol}"
                                   f"&timeframe={tf}&from={_iso(now)}&count=-{min(n + 1, 1000)}")
        rows = [{"time": _epoch(c["time"]), "open": c["open"], "high": c["high"], "low": c["low"],
                 "close": c["close"], "tick_volume": c.get("volume", 0)} for c in r.get("candles", [])]
        df = pd.DataFrame(rows).astype(float)
        df = df[df["time"] + sec <= now].reset_index(drop=True)  # closed candles only
        self._candles[key] = df
        return df.iloc[-n:].reset_index(drop=True)

    def candles_history(self, symbol: str, tf: str, total: int = 3000) -> pd.DataFrame:
        """Page backwards through closed candles (1000 per request) for model training / backtests."""
        sec = TIMEFRAMES[tf] * 60
        end, frames = time.time(), []
        while sum(len(f) for f in frames) < total:
            r = self.c._request("GET", f"/v1/market-data/accounts/{self.c.account_id}/candles?instrument={symbol}"
                                       f"&timeframe={tf}&from={_iso(end)}&count=-1000")
            rows = [{"time": _epoch(c["time"]), "open": c["open"], "high": c["high"], "low": c["low"],
                     "close": c["close"], "tick_volume": c.get("volume", 0)} for c in r.get("candles", [])]
            if not rows:
                break
            df = pd.DataFrame(rows).astype(float)
            frames.append(df)
            first = float(df["time"].min())
            if first >= end - sec:
                break
            end = first - 1
        if not frames:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "tick_volume"])
        out = pd.concat(frames).drop_duplicates("time").sort_values("time")
        out = out[out["time"] + sec <= time.time()].reset_index(drop=True)
        return out.iloc[-total:].reset_index(drop=True)

    def positions(self) -> list[dict]:
        snap = self._snapshot()
        sltp = {o.get("position_id"): o for o in snap.get("orders", []) if o.get("order_type") == "sltp"}
        out = []
        for p in snap.get("positions", []):
            sym, buy, vol = p["instrument"], p["direction"] == "buy", _dec(p["volume"])
            t = self.tick(sym)
            px = t["bid"] if buy else t["ask"]
            open_px = _dec(p.get("open_price"))
            contract = self._contract.get(sym) or (self.spec(sym) and self._contract[sym])
            pnl = (px - open_px if buy else open_px - px) * contract * vol + _dec(p.get("swap")) + _dec(p.get("commission"))
            o = sltp.get(p["position_id"], {})
            out.append({"ticket": p["position_id"], "symbol": sym, "direction": "BUY" if buy else "SELL",
                        "volume": vol, "price_open": open_px, "price_current": px,
                        "sl": _dec(o.get("stop_loss_price")) or None, "tp": _dec(o.get("take_profit_price")) or None,
                        "sltp_order_id": o.get("order_id"), "profit": round(pnl, 2),
                        "time_open": _epoch(p["create_time"])})
        return out

    def history(self, n: int = 50) -> list[dict]:
        # cached: deal history changes rarely and REST quota is limited
        if getattr(self, "_hist", None) is not None and time.time() - self._hist_t < 20:
            return self._hist[:n]
        r = self.c._request("GET", f"/v1/history/accounts/{self.c.account_id}/deals?from={_iso(time.time() - 7 * 86400)}"
                                   f"&deal_type=close&limit={min(n, 200)}")
        out = []
        for it in r.get("items", []):
            d = it["deal"]
            prof = d.get("profit") or {}
            out.append({"ticket": d.get("position_id"), "symbol": d.get("instrument"),
                        "direction": "BUY" if d.get("direction") == "sell" else "SELL",  # close deal is opposite side
                        "volume": _dec(d.get("volume")), "price_close": _dec(d.get("price")),
                        "profit": _dec(prof.get("amount") if isinstance(prof, dict) else prof),
                        "time_close": _epoch(d["create_time"]), "reason": ""})
        self._hist, self._hist_t = out[::-1], time.time()
        return self._hist[:n]

    # ---- trading -----------------------------------------------------------------
    def _fmt_px(self, symbol: str, x: float) -> str:
        d = self._digits.get(symbol) or self.spec(symbol).digits
        return f"{round(x, d):.{d}f}"

    def _fmt_vol(self, symbol: str, v: float) -> str:
        step = self.spec(symbol).volume_step
        dec = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
        return f"{v:.{dec}f}"

    def _await(self, ack: dict, timeout: float = 15.0) -> dict:
        op = ack.get("operation_id")
        if not op:
            return {"ok": False, "status": "unknown", "message": "لم يُرجع الوسيط operation_id — سيتم التسوية من snapshot"}
        delay, end = 0.3, time.time() + timeout
        while time.time() < end:
            st = self.c.operation_status(op)
            s = st.get("status")
            if s == "confirmed":
                self._snap_t = 0  # force fresh snapshot
                self._hist = None
                return {"ok": True, "status": s, "operation_id": op}
            if s in ("rejected", "failed"):
                return {"ok": False, "status": s, "operation_id": op,
                        "message": f"{st.get('error_message') or st.get('error_code')}"}
            time.sleep(delay)
            delay = min(delay * 1.6, 2.0)
        return {"ok": False, "status": "pending", "operation_id": op, "message": "لم يصل التأكيد خلال المهلة — تحقق من الصفقات"}

    def market_order(self, symbol, direction, lots, sl, tp, comment, client_id) -> dict:
        if client_id in self._sent:
            return {**self._sent[client_id], "duplicate": True}
        before = {p["ticket"] for p in self.positions()}
        body_kw = dict(instrument=symbol, side=direction.lower(), volume=self._fmt_vol(symbol, lots),
                       client_request_id=f"ai-{client_id}")
        try:
            ack = self.c.open_position(**body_kw, stop_loss_price=self._fmt_px(symbol, sl),
                                       take_profit_price=self._fmt_px(symbol, tp), comment=comment[:30])
        except ExnessError as e:
            return {"ok": False, "ticket": None, "price": None, "retcode": e.code, "message": str(e)}
        res = self._await(ack)
        if not res["ok"]:
            return {"ok": False, "ticket": None, "price": None, "retcode": res["status"], "message": res.get("message", "")}
        new = [p for p in self.positions() if p["ticket"] not in before and p["symbol"] == symbol]
        pos = new[0] if new else None
        out = {"ok": True, "ticket": pos["ticket"] if pos else None, "price": pos["price_open"] if pos else None,
               "retcode": "confirmed", "message": "تم التأكيد من Exness"}
        self._sent[client_id] = out
        return out

    def modify(self, ticket, sl, tp) -> dict:
        p = next((x for x in self.positions() if x["ticket"] == ticket), None)
        if not p:
            return {"ok": False, "message": "الصفقة غير موجودة"}
        cid = "ai-mod-" + uuid.uuid4().hex[:10]
        try:
            if p.get("sltp_order_id"):
                body = {}
                if sl is not None:
                    body["stop_loss_price"] = self._fmt_px(p["symbol"], sl)
                if tp is not None:
                    body["take_profit_price"] = self._fmt_px(p["symbol"], tp)
                ack = self.c.signed_json("PATCH", f"/v1/trading/accounts/{self.c.account_id}/orders/{p['sltp_order_id']}",
                                         body, cid)
            else:
                body = {"type": "sltp", "position_id": str(ticket)}
                if sl is not None:
                    body["stop_loss_price"] = self._fmt_px(p["symbol"], sl)
                if tp is not None:
                    body["take_profit_price"] = self._fmt_px(p["symbol"], tp)
                ack = self.c.signed_json("POST", f"/v1/trading/accounts/{self.c.account_id}/orders", body, cid)
        except ExnessError as e:
            return {"ok": False, "message": str(e)}
        r = self._await(ack)
        return {"ok": r["ok"], "message": r.get("message", "تم التعديل")}

    def close(self, ticket, lots=None, reason: str = "manual") -> dict:
        p = next((x for x in self.positions() if x["ticket"] == ticket), None)
        if not p:
            return {"ok": False, "message": "الصفقة غير موجودة"}
        q = f"?volume={self._fmt_vol(p['symbol'], lots)}" if lots else ""
        try:
            ack = self.c.signed_json("DELETE", f"/v1/trading/accounts/{self.c.account_id}/positions/{ticket}{q}",
                                     None, "ai-close-" + uuid.uuid4().hex[:10])
        except ExnessError as e:
            return {"ok": False, "message": str(e)}
        r = self._await(ack)
        return {"ok": r["ok"], "message": r.get("message", "تم الإغلاق")}
