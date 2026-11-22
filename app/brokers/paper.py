"""Paper broker over a SYNTHETIC price simulator.

The prices here are generated, not market data. They exist so the full pipeline
(analysis -> decision -> risk -> execution -> dashboard) can be exercised before
an MT5 demo account is connected. The dashboard labels this mode clearly.
"""
from __future__ import annotations

import itertools
import time

import numpy as np
import pandas as pd

from ..risk import AccountState, SymbolSpec
from .base import TIMEFRAMES, Broker

SPECS = {
    "XAUUSD": dict(start=2000.0, vol=0.0009, spread=0.20, spec=SymbolSpec("XAUUSD", 2, 0.01, 0.01, 1.0, 0.01, 100, 0.01, 0)),
    "BTCUSD": dict(start=60000.0, vol=0.0016, spread=12.0, spec=SymbolSpec("BTCUSD", 2, 0.01, 0.01, 0.01, 0.01, 20, 0.01, 0)),
    "EURUSD": dict(start=1.1000, vol=0.0004, spread=0.00008, spec=SymbolSpec("EURUSD", 5, 0.00001, 0.00001, 1.0, 0.01, 100, 0.01, 0)),
    "GBPUSD": dict(start=1.2700, vol=0.0005, spread=0.00010, spec=SymbolSpec("GBPUSD", 5, 0.00001, 0.00001, 1.0, 0.01, 100, 0.01, 0)),
}
HISTORY_M1 = 240 * 300  # enough M1 bars to build 300 H4 candles


def _gen_m1(seed: int, start: float, vol: float, n: int, t0: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    # regime-switching drift so trends, ranges and reversals all appear
    regime_len = rng.integers(300, 3000, size=n // 300 + 2)
    drifts = rng.normal(0, vol * 0.08, size=len(regime_len))
    drift = np.repeat(drifts, regime_len)[:n]
    sig = vol * np.exp(rng.normal(0, 0.35, size=n))
    rets = drift + sig * rng.standard_t(4, size=n) / 1.4
    close = start * np.exp(np.cumsum(rets))
    open_ = np.r_[start, close[:-1]]
    wick = np.abs(rng.normal(0, vol * 0.6, size=n)) * close
    high = np.maximum(open_, close) + wick * rng.random(n)
    low = np.minimum(open_, close) - wick * rng.random(n)
    vol_ = rng.integers(20, 400, size=n)
    times = t0 + np.arange(n) * 60
    return pd.DataFrame({"time": times, "open": open_, "high": high, "low": low, "close": close, "tick_volume": vol_})


def _resample(m1: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if minutes == 1:
        return m1
    key = (m1["time"] // (minutes * 60)) * minutes * 60
    g = m1.groupby(key)
    out = pd.DataFrame({"time": g["time"].first().index, "open": g["open"].first().values,
                        "high": g["high"].max().values, "low": g["low"].min().values,
                        "close": g["close"].last().values, "tick_volume": g["tick_volume"].sum().values})
    # drop the still-forming last bucket
    last_complete = (m1["time"].iloc[-1] + 60) % (minutes * 60) == 0
    return out if last_complete else out.iloc[:-1]


class PaperBroker(Broker):
    name = "PAPER/SIMULATION"
    is_simulation = True

    def __init__(self, symbols: list[str], balance: float = 10_000.0, seed: int = 7) -> None:
        self.symbols = [s for s in symbols if s in SPECS]
        t0 = (int(time.time()) // 86400 - 60) * 86400
        self._future = {}
        for i, s in enumerate(self.symbols):
            cfg = SPECS[s]
            self._future[s] = _gen_m1(seed + i, cfg["start"], cfg["vol"], HISTORY_M1 + 50_000, t0)
        self._cursor = 0
        self.balance = balance
        self._positions: dict[int, dict] = {}
        self._history: list[dict] = []
        self._tickets = itertools.count(100001)
        self._seen_client_ids: dict[str, dict] = {}
        self._connected = True

    # ---- connection
    def connect(self) -> bool:
        return True

    def connected(self) -> bool:
        return self._connected

    # ---- market
    @property
    def m1(self) -> dict:
        end = HISTORY_M1 + min(self._cursor, 49_999)
        return {s: self._future[s].iloc[end - HISTORY_M1 : end] for s in self.symbols}

    def step(self) -> None:
        """Advance one synthetic M1 bar and process SL/TP."""
        self._cursor += 1
        self._check_stops()

    def spec(self, symbol: str) -> SymbolSpec:
        return SPECS[symbol]["spec"]

    def tick(self, symbol: str) -> dict:
        last = float(self.m1[symbol]["close"].iloc[-1])
        sp = SPECS[symbol]["spread"]
        return {"bid": last, "ask": last + sp, "time": int(self.m1[symbol]["time"].iloc[-1])}

    def candles(self, symbol: str, tf: str, n: int = 300) -> pd.DataFrame:
        df = _resample(self.m1[symbol], TIMEFRAMES[tf]).astype(float)
        return df.iloc[-n:].reset_index(drop=True)

    # ---- account
    def _pnl(self, p: dict) -> float:
        t = self.tick(p["symbol"])
        px = t["bid"] if p["direction"] == "BUY" else t["ask"]
        spec = self.spec(p["symbol"])
        diff = (px - p["price_open"]) if p["direction"] == "BUY" else (p["price_open"] - px)
        return diff / spec.tick_size * spec.tick_value * p["volume"]

    def account(self) -> AccountState:
        floating = sum(self._pnl(p) for p in self._positions.values())
        used = sum(p["volume"] * 100 for p in self._positions.values())  # rough simulated margin
        eq = self.balance + floating
        return AccountState(balance=round(self.balance, 2), equity=round(eq, 2), margin_free=round(eq - used, 2))

    def positions(self) -> list[dict]:
        return [{**p, "profit": round(self._pnl(p), 2), "price_current": self.tick(p["symbol"])["bid"]}
                for p in self._positions.values()]

    def history(self, n: int = 50) -> list[dict]:
        return self._history[-n:][::-1]

    # ---- trading
    def market_order(self, symbol, direction, lots, sl, tp, comment, client_id) -> dict:
        if client_id in self._seen_client_ids:  # idempotency
            return {**self._seen_client_ids[client_id], "duplicate": True}
        t = self.tick(symbol)
        price = t["ask"] if direction == "BUY" else t["bid"]
        ticket = next(self._tickets)
        self._positions[ticket] = {"ticket": ticket, "symbol": symbol, "direction": direction, "volume": lots,
                                   "price_open": price, "sl": sl, "tp": tp, "time_open": t["time"],
                                   "comment": comment}
        res = {"ok": True, "ticket": ticket, "price": price, "retcode": "PAPER_FILLED", "message": "تنفيذ ورقي (محاكاة)"}
        self._seen_client_ids[client_id] = res
        return res

    def modify(self, ticket, sl, tp) -> dict:
        p = self._positions.get(ticket)
        if not p:
            return {"ok": False, "message": "الصفقة غير موجودة"}
        if sl is not None:
            p["sl"] = sl
        if tp is not None:
            p["tp"] = tp
        return {"ok": True, "message": "تم التعديل"}

    def close(self, ticket, lots=None, reason: str = "manual") -> dict:
        p = self._positions.get(ticket)
        if not p:
            return {"ok": False, "message": "الصفقة غير موجودة"}
        vol = p["volume"] if lots is None else min(lots, p["volume"])
        frac = vol / p["volume"]
        profit = self._pnl(p) * frac
        self.balance += profit
        t = self.tick(p["symbol"])
        self._history.append({**p, "volume": vol, "profit": round(profit, 2), "reason": reason, "time_close": t["time"],
                              "price_close": t["bid"] if p["direction"] == "BUY" else t["ask"]})
        p["volume"] = round(p["volume"] - vol, 8)
        if p["volume"] <= 1e-9:
            del self._positions[ticket]
        return {"ok": True, "profit": round(profit, 2), "message": "تم الإغلاق"}

    def _check_stops(self) -> None:
        for tk, p in list(self._positions.items()):
            bar = self.m1[p["symbol"]].iloc[-1]
            hi, lo = float(bar["high"]), float(bar["low"])
            if p["direction"] == "BUY":
                hit_sl, hit_tp = p["sl"] and lo <= p["sl"], p["tp"] and hi >= p["tp"]
            else:
                hit_sl, hit_tp = p["sl"] and hi >= p["sl"], p["tp"] and lo <= p["tp"]
            if hit_sl or hit_tp:
                # pessimistic: if both touched in same bar, assume SL first
                exit_px = p["sl"] if hit_sl else p["tp"]
                spec = self.spec(p["symbol"])
                diff = (exit_px - p["price_open"]) if p["direction"] == "BUY" else (p["price_open"] - exit_px)
                profit = diff / spec.tick_size * spec.tick_value * p["volume"]
                self.balance += profit
                self._history.append({**p, "profit": round(profit, 2), "reason": "SL" if hit_sl else "TP",
                                      "time_close": int(bar["time"]), "price_close": exit_px})
                del self._positions[tk]
