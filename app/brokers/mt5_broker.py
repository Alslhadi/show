"""MetaTrader 5 adapter (runs only on Windows with the MT5 terminal installed and open).

Uses the official `MetaTrader5` Python package. Notes from the docs:
  * Windows-only package; it talks to a running terminal.
  * No event callbacks in Python, so we poll.
  * Success is reported ONLY when order_send returns TRADE_RETCODE_DONE (or PLACED).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from ..config import settings
from ..risk import AccountState, SymbolSpec
from .base import Broker

try:  # pragma: no cover - not importable on Linux
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover
    mt5 = None


def _tf(tf: str):
    return {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
            "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1}[tf]


class MT5Broker(Broker):
    name = "MT5"
    is_simulation = False

    def __init__(self) -> None:
        if mt5 is None:
            raise RuntimeError("حزمة MetaTrader5 غير مثبتة — تعمل على ويندوز فقط: pip install MetaTrader5")
        self._ok = False
        self._sent: dict[str, dict] = {}

    def sym(self, s: str) -> str:
        return s + settings.symbol_suffix

    # ---- connection -------------------------------------------------------
    def connect(self) -> bool:
        kw = {}
        if settings.mt5_path:
            kw["path"] = settings.mt5_path
        if settings.mt5_login:
            kw.update(login=int(settings.mt5_login), password=settings.mt5_password or "", server=settings.mt5_server or "")
        self._ok = bool(mt5.initialize(**kw))
        if self._ok:
            for s in settings.symbol_list:
                mt5.symbol_select(self.sym(s), True)
        return self._ok

    def connected(self) -> bool:
        info = mt5.terminal_info()
        self._ok = bool(info and info.connected)
        return self._ok

    def last_error(self) -> str:
        return str(mt5.last_error())

    def is_demo(self) -> bool:
        a = mt5.account_info()
        return bool(a and a.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO)

    # ---- data ---------------------------------------------------------------
    def account(self) -> AccountState:
        a = mt5.account_info()
        if a is None:
            raise RuntimeError(f"account_info فشل: {self.last_error()}")
        return AccountState(balance=a.balance, equity=a.equity, margin_free=a.margin_free, currency=a.currency)

    def spec(self, symbol: str) -> SymbolSpec:
        i = mt5.symbol_info(self.sym(symbol))
        if i is None:
            raise RuntimeError(f"الرمز {self.sym(symbol)} غير موجود — راجع SYMBOL_SUFFIX")
        return SymbolSpec(name=symbol, digits=i.digits, point=i.point, tick_size=i.trade_tick_size,
                          tick_value=i.trade_tick_value, volume_min=i.volume_min, volume_max=i.volume_max,
                          volume_step=i.volume_step, stops_level=i.trade_stops_level,
                          trade_allowed=i.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL)

    def tick(self, symbol: str) -> dict:
        t = mt5.symbol_info_tick(self.sym(symbol))
        if t is None:
            raise RuntimeError(f"لا يوجد سعر لـ {symbol}")
        return {"bid": t.bid, "ask": t.ask, "time": t.time}

    def candles(self, symbol: str, tf: str, n: int = 300) -> pd.DataFrame:
        # start_pos=1 skips the still-forming candle -> closed candles only
        r = mt5.copy_rates_from_pos(self.sym(symbol), _tf(tf), 1, n)
        if r is None or len(r) == 0:
            raise RuntimeError(f"لا توجد شموع {symbol} {tf}: {self.last_error()}")
        df = pd.DataFrame(r)[["time", "open", "high", "low", "close", "tick_volume"]]
        return df.astype(float)

    def positions(self) -> list[dict]:
        out = []
        for p in mt5.positions_get() or []:
            out.append({"ticket": p.ticket, "symbol": p.symbol.removesuffix(settings.symbol_suffix) if settings.symbol_suffix else p.symbol,
                        "direction": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL", "volume": p.volume,
                        "price_open": p.price_open, "price_current": p.price_current, "sl": p.sl, "tp": p.tp,
                        "profit": p.profit, "time_open": p.time, "comment": p.comment, "magic": p.magic})
        return out

    def history(self, n: int = 50) -> list[dict]:
        to = datetime.now(timezone.utc) + timedelta(days=1)
        deals = mt5.history_deals_get(to - timedelta(days=31), to) or []
        out = [{"ticket": d.position_id, "symbol": d.symbol, "direction": "BUY" if d.type == 0 else "SELL",
                "volume": d.volume, "price_close": d.price, "profit": d.profit, "time_close": d.time, "comment": d.comment}
               for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
        return out[-n:][::-1]

    # ---- trading ------------------------------------------------------------
    def _filling(self, symbol: str):
        fm = mt5.symbol_info(self.sym(symbol)).filling_mode
        if fm & 1:
            return mt5.ORDER_FILLING_FOK
        if fm & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def _send(self, req: dict) -> dict:
        chk = mt5.order_check(req)
        if chk is None or chk.retcode not in (0, mt5.TRADE_RETCODE_DONE):
            return {"ok": False, "ticket": None, "price": None, "retcode": getattr(chk, "retcode", None),
                    "message": f"order_check رفض: {getattr(chk, 'comment', self.last_error())}"}
        res = mt5.order_send(req)
        if res is None:
            return {"ok": False, "ticket": None, "price": None, "retcode": None, "message": f"order_send فشل: {self.last_error()}"}
        ok = res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED)
        return {"ok": ok, "ticket": res.order or None, "deal": res.deal, "price": res.price, "retcode": res.retcode,
                "message": "تم التأكيد من الوسيط" if ok else f"رفض الوسيط: {res.comment}"}

    def margin_required(self, symbol: str, direction: str, lots: float, price: float) -> float | None:
        t = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        return mt5.order_calc_margin(t, self.sym(symbol), lots, price)

    def market_order(self, symbol, direction, lots, sl, tp, comment, client_id) -> dict:
        if client_id in self._sent:
            return {**self._sent[client_id], "duplicate": True}
        t = self.tick(symbol)
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": self.sym(symbol), "volume": float(lots),
               "type": mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL,
               "price": t["ask"] if direction == "BUY" else t["bid"], "sl": float(sl), "tp": float(tp),
               "deviation": settings.max_deviation_points, "magic": settings.mt5_magic,
               "comment": f"AI {client_id}"[:31], "type_time": mt5.ORDER_TIME_GTC,
               "type_filling": self._filling(symbol)}
        res = self._send(req)
        if res["ok"]:
            self._sent[client_id] = res
        return res

    def modify(self, ticket, sl, tp) -> dict:
        p = (mt5.positions_get(ticket=ticket) or [None])[0]
        if p is None:
            return {"ok": False, "message": "الصفقة غير موجودة"}
        return self._send({"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "symbol": p.symbol,
                           "sl": float(sl if sl is not None else p.sl), "tp": float(tp if tp is not None else p.tp),
                           "magic": settings.mt5_magic})

    def close(self, ticket, lots=None, reason: str = "manual") -> dict:
        p = (mt5.positions_get(ticket=ticket) or [None])[0]
        if p is None:
            return {"ok": False, "message": "الصفقة غير موجودة"}
        t = mt5.symbol_info_tick(p.symbol)
        buy = p.type == mt5.POSITION_TYPE_BUY
        return self._send({"action": mt5.TRADE_ACTION_DEAL, "position": ticket, "symbol": p.symbol,
                           "volume": float(lots or p.volume), "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
                           "price": t.bid if buy else t.ask, "deviation": settings.max_deviation_points,
                           "magic": settings.mt5_magic, "comment": f"close {reason}"[:31],
                           "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(p.symbol.removesuffix(settings.symbol_suffix) if settings.symbol_suffix else p.symbol)})

    def shutdown(self) -> None:
        mt5.shutdown()
        time.sleep(0.1)
