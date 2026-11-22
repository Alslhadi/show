"""Orchestrator: runs the loop, owns signals, enforces execution modes and live-trading gating."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .brokers.base import Broker
from .config import settings
from .decision import evaluate
from .risk import risk_engine

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
MODES = ("MANUAL", "ASSISTED", "SEMI_AUTO", "AUTO")


def audit(event: str, **data) -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **data}
    with open(LOG_DIR / "audit.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


class Engine:
    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        self.execution_mode = settings.execution_mode if settings.execution_mode in MODES else "ASSISTED"
        self.live_confirmed = False  # second, separate confirmation (dashboard) for real money
        self.signals: dict[str, dict] = {}  # latest card per symbol
        self.pending: dict[str, dict] = {}  # SEMI_AUTO approvals
        self.plans: dict[int, dict] = {}  # ticket -> management plan
        self.news_windows: list[dict] = []  # [{"name","start","end","symbols":[...]}] (UTC epoch)
        self.last_m5: dict[str, float] = {}
        self.status = {"started": time.time(), "loop_ok": False, "last_loop": None, "error": None}
        self.subscribers: set[asyncio.Queue] = set()
        self.logs: list[dict] = []

    # ---- helpers -----------------------------------------------------------
    def log(self, level: str, msg: str, **kw) -> None:
        rec = {"t": time.time(), "level": level, "msg": msg, **kw}
        self.logs = (self.logs + [rec])[-300:]
        audit("log", level=level, msg=msg, **kw)
        print(f"ENGINE [{level}] {msg} {json.dumps(kw, ensure_ascii=False, default=str) if kw else ''}", flush=True)

    def account_is_real(self) -> bool:
        if self.broker.is_simulation:
            return False
        is_demo = getattr(self.broker, "is_demo", lambda: False)()
        return not is_demo

    def _preconfirmed(self) -> bool:
        acc = getattr(self.broker, "account_details", lambda: {})() or {}
        return bool(settings.live_trading_enabled and settings.live_confirmed_account
                    and str(acc.get("account_id")) == str(settings.live_confirmed_account))

    def trading_permitted(self) -> tuple[bool, str]:
        if risk_engine.state.kill_switch:
            return False, f"KILL SWITCH: {risk_engine.state.kill_reason}"
        if not self.broker.connected():
            return False, "الوسيط غير متصل"
        if self.account_is_real() and not (settings.live_trading_enabled and (self.live_confirmed or self._preconfirmed())):
            return False, "حساب حقيقي — التداول الحي غير مفعّل (يتطلب LIVE_TRADING_ENABLED وتأكيداً منفصلاً)"
        return True, ""

    def news_block(self, symbol: str) -> str | None:
        now = time.time()
        for w in self.news_windows:
            pad = settings.news_block_minutes * 60
            if w["start"] - pad <= now <= w["end"] + pad and (not w.get("symbols") or symbol in w["symbols"]):
                return w["name"]
        return None

    # ---- analysis ------------------------------------------------------------
    def analyze_symbol(self, symbol: str) -> dict:
        tfs = (settings.tf_dominant, settings.tf_confirm, settings.tf_setup, settings.tf_entry)
        frames = {tf: self.broker.candles(symbol, tf, 300) for tf in tfs}
        t = self.broker.tick(symbol)
        card = evaluate(symbol, frames, self.broker.spec(symbol), self.broker.account(),
                        spread=t["ask"] - t["bid"], news_blocked=self.news_block(symbol))
        card["spread"] = t["ask"] - t["bid"]
        card["data_source"] = "SIMULATION" if self.broker.is_simulation else "MT5"
        return card

    # ---- execution -----------------------------------------------------------
    def execute(self, card: dict, source: str) -> dict:
        ok, why = self.trading_permitted()
        if not ok:
            self.log("warn", "رفض التنفيذ", reason=why, symbol=card.get("symbol"))
            return {"ok": False, "message": why}
        if card.get("grade") in (None, "NO_TRADE") or not card.get("lots"):
            return {"ok": False, "message": "لا توجد صفقة صالحة"}
        sym, d = card["symbol"], card["direction"]
        t = self.broker.tick(sym)
        entry = t["ask"] if d == "BUY" else t["bid"]
        spec, acc = self.broker.spec(sym), self.broker.account()
        margin = None
        if hasattr(self.broker, "margin_required"):
            margin = self.broker.margin_required(sym, d, card["lots"], entry)
        errs = risk_engine.validate(symbol=sym, direction=d, entry=entry, sl=card["sl"], tp=card["tp3"],
                                    lots=card["lots"], spec=spec, acc=acc, open_positions=self.broker.positions(),
                                    spread=t["ask"] - t["bid"], margin_required=margin)
        if errs:
            self.log("warn", "محرك المخاطر رفض الأمر", symbol=sym, errors=errs)
            return {"ok": False, "message": "؛ ".join(errs)}
        client_id = card["id"]
        res = self.broker.market_order(sym, d, card["lots"], card["sl"], card["tp3"], f"{source}", client_id)
        audit("order", source=source, card_id=client_id, symbol=sym, direction=d, lots=card["lots"], result=res)
        if res.get("ok"):
            self.plans[res["ticket"]] = {"tp1": card["tp1"], "tp2": card["tp2"], "entry": entry, "sl": card["sl"],
                                         "atr": card.get("atr_setup", 0), "direction": d, "partial_done": False}
            self.log("info", f"تم تنفيذ {d} {sym} {card['lots']} لوت — {res.get('message', '')}", ticket=res["ticket"])
        else:
            self.log("error", "فشل التنفيذ", symbol=sym, result=res)
        return res

    def manage_positions(self) -> None:
        """TP1: close 50% + break-even. After TP1: ATR trailing. Stale/weekend exits.
        A stop is only ever moved in the trade's favour, never widened."""
        now_utc = datetime.now(timezone.utc)
        weekend_close = settings.close_before_weekend and now_utc.weekday() == 4 and now_utc.hour >= 20
        for p in self.broker.positions():
            plan = self.plans.get(p["ticket"])
            if not plan:
                continue
            buy = p["direction"] == "BUY"
            px = p["price_current"]
            mkt_time = self.broker.tick(p["symbol"])["time"]
            if weekend_close and p["symbol"] != "BTCUSD":
                self.broker.close(p["ticket"], reason="weekend")
                self.log("info", "إغلاق قبل نهاية الأسبوع", ticket=p["ticket"])
                continue
            if mkt_time - p["time_open"] > settings.max_hold_minutes * 60:
                self.broker.close(p["ticket"], reason="max hold")
                self.log("info", "إغلاق: تجاوزت أقصى مدة", ticket=p["ticket"])
                continue
            if not plan["partial_done"]:
                if (px >= plan["tp1"]) if buy else (px <= plan["tp1"]):
                    spec = self.broker.spec(p["symbol"])
                    half = round(p["volume"] / 2 / spec.volume_step) * spec.volume_step
                    partial = half >= spec.volume_min and p["volume"] - half >= spec.volume_min
                    if partial:
                        self.broker.close(p["ticket"], half, reason="TP1 partial")
                    self.broker.modify(p["ticket"], plan["entry"], None)
                    plan["partial_done"], plan["sl"] = True, plan["entry"]
                    self.log("info", "TP1: إغلاق 50% ونقل الوقف للتعادل" if partial
                             else "TP1: الحجم 0.01 لا يقبل التقسيم — نقل الوقف للتعادل فقط", ticket=p["ticket"])
                continue
            trail = settings.trailing_atr * plan["atr"]
            new_sl = px - trail if buy else px + trail
            if (new_sl > plan["sl"]) if buy else (new_sl < plan["sl"]):
                if self.broker.modify(p["ticket"], new_sl, None).get("ok"):
                    plan["sl"] = new_sl

    def on_card(self, card: dict) -> None:
        prev = self.signals.get(card["symbol"])
        self.signals[card["symbol"]] = card
        if card["grade"] == "NO_TRADE":
            return
        if prev and prev.get("grade") == card["grade"] and prev.get("direction") == card["direction"]:
            return  # same setup still standing; avoid duplicate orders
        if self.execution_mode == "SEMI_AUTO":
            self.pending[card["id"]] = card
            self.log("info", f"فرصة بانتظار موافقتك: {card['headline']}", card_id=card["id"])
        elif self.execution_mode == "AUTO" and card.get("auto_ok"):
            self.execute(card, "AUTO")

    # ---- loop ----------------------------------------------------------------
    async def run(self, interval: float = 2.0) -> None:
        next_connect = 0.0
        while True:
            if not self.broker.connected():
                if time.time() >= next_connect:
                    ok = await asyncio.to_thread(self.broker.connect)
                    if ok:
                        self.log("info", f"متصل بالوسيط {self.broker.name}")
                    else:
                        err = getattr(self.broker, "last_error", "")
                        self.log("error", f"فشل الاتصال بالوسيط: {err} — إعادة المحاولة بعد 60 ثانية")
                        next_connect = time.time() + 60
                if not self.broker.connected():
                    self.status.update(loop_ok=False, error="الوسيط غير متصل")
                    await self.broadcast()
                    await asyncio.sleep(5)
                    continue
            try:
                self.broker.step()
                acc = self.broker.account()
                today = datetime.now(timezone.utc)
                risk_engine.update_equity(acc, new_day=False)
                closed_before = len(self.broker.history(1000))
                self.manage_positions()
                for sym in settings.symbol_list:
                    try:
                        m5 = self.broker.candles(sym, settings.tf_entry, 2)
                        last = float(m5["time"].iloc[-1])
                        if self.last_m5.get(sym) != last:  # evaluate once per closed entry-TF candle
                            self.last_m5[sym] = last
                            self.on_card(self.analyze_symbol(sym))
                    except Exception as e:  # one bad symbol must not stop the loop
                        self.log("error", f"تحليل {sym} فشل: {e}")
                new_hist = self.broker.history(1000)
                for h in new_hist[: max(0, len(new_hist) - closed_before)]:
                    risk_engine.record_result(h.get("profit", 0))
                self.status.update(loop_ok=True, last_loop=today.timestamp(), error=None)
                if time.time() - getattr(self, "_hb", 0) > 300:  # heartbeat every 5 min
                    self._hb = time.time()
                    grades = {k: f"{v.get('grade')}:{v.get('score')}" for k, v in self.signals.items()}
                    print("HEARTBEAT", json.dumps({"equity": acc.equity, "balance": acc.balance,
                          "open": len(self.broker.positions()), "permitted": self.trading_permitted(),
                          "mode": self.execution_mode, "signals": grades}, ensure_ascii=False, default=str), flush=True)
            except Exception as e:
                self.status.update(loop_ok=False, error=str(e))
                self.log("error", f"خطأ في الحلقة: {e}")
                await asyncio.sleep(10)  # back off; never hammer the broker
            await self.broadcast()
            await asyncio.sleep(interval)

    def snapshot(self) -> dict:
        try:
            acc = self.broker.account() if self.broker.connected() else None
        except Exception:
            acc = None
        hist = self.broker.history(200)
        wins = [h for h in hist if h.get("profit", 0) > 0]
        losses = [h for h in hist if h.get("profit", 0) < 0]
        gp, gl = sum(h["profit"] for h in wins), -sum(h["profit"] for h in losses)
        ok, why = self.trading_permitted()
        return {
            "server_time": time.time(),
            "broker": self.broker.name, "simulation": self.broker.is_simulation,
            "connected": self.broker.connected(), "account_real": self.account_is_real(),
            "trading_mode": "LIVE" if (self.account_is_real() and (self.live_confirmed or self._preconfirmed())) else "DEMO",
            "execution_mode": self.execution_mode, "trading_permitted": ok, "blocked_reason": why,
            "account": acc.__dict__ if acc else None,
            "positions": self.broker.positions(),
            "history": hist[:50],
            "stats": {"trades": len(hist), "win_rate": round(len(wins) / len(hist) * 100, 1) if hist else None,
                      "profit_factor": round(gp / gl, 2) if gl else None, "net": round(gp - gl, 2)},
            "risk": {"kill_switch": risk_engine.state.kill_switch, "kill_reason": risk_engine.state.kill_reason,
                     "consecutive_losses": risk_engine.state.consecutive_losses,
                     "day_start_equity": risk_engine.state.day_start_equity, "peak_equity": risk_engine.state.peak_equity,
                     "limits": {k: getattr(settings, k) for k in ("risk_per_trade_pct", "daily_max_loss_pct",
                                "weekly_max_loss_pct", "max_drawdown_pct", "max_open_trades", "max_consecutive_losses")}},
            "signals": list(self.signals.values()),
            "pending": list(self.pending.values()),
            "status": self.status,
            "logs": self.logs[-60:][::-1],
        }

    async def broadcast(self) -> None:
        if not self.subscribers:
            return
        msg = json.dumps(self.snapshot(), ensure_ascii=False, default=str)
        for q in list(self.subscribers):
            if q.qsize() < 5:
                q.put_nowait(msg)

    def new_client_id(self) -> str:
        return uuid.uuid4().hex[:12]
