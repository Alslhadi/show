"""Orchestrator: runs the loop, owns signals, enforces execution modes and live-trading gating."""
from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .brokers.base import Broker
from .config import settings
from .decision import evaluate
from .risk import risk_engine, round_volume

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
        self.cooldown_until: dict[str, float] = {}  # symbol -> epoch
        self.models: dict = {}  # symbol -> MetaModel (hybrid agent)
        self.backtests: dict = {}  # symbol -> latest walk-forward result (evidence gate)
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

    def _is_tg(self, p: dict) -> bool:
        plan = self.plans.get(p["ticket"]) or {}
        if str(plan.get("source", "")).startswith("TG") or str(p.get("comment", "")).startswith("TG"):
            return True
        try:
            from .agent.stats import ledger
            ledger.load()
            return str((ledger.open.get(str(p["ticket"])) or {}).get("source", "")).startswith("TG")
        except Exception:
            return False

    def profit_lock(self) -> str | None:
        """Step ratchet. When the day is up >= profit_lock_start_usd, a floor keeps profit_lock_keep_pct
        of the best gain. Falling under it pauses the bot's own entries for profit_lock_pause_min, then
        it resumes and measures new gains from there. The highest floor ever reached is a hard line:
        dropping one trade's risk below it ends bot trading for the day. Telegram is unaffected."""
        st = risk_engine.state
        try:
            eq = self.broker.account().equity
        except Exception:
            return None
        if st.day_start_equity is None:
            return None
        now, day = time.time(), datetime.now(timezone.utc).date()
        if getattr(self, "_lock_day", None) != day:
            self._lock_day, self._base, self._day_peak = day, st.day_start_equity, eq
            self._pause_until, self._hard_floor, self._day_stop = 0.0, None, False
        if self._day_stop:
            return "تم تأمين ربح اليوم — توقف لين بكرة"
        if now < self._pause_until:
            return "استراحة بعد تأمين الربح"
        if self._hard_floor and eq < self._hard_floor - eq * settings.scalp_risk_pct / 100:
            self._day_stop = True
            self.log("warn", f"تأمين الأرباح: نزل تحت الحد المحمي {self._hard_floor:.2f}$ — توقف لين بكرة")
            return "تم تأمين ربح اليوم — توقف لين بكرة"
        self._day_peak = max(self._day_peak, eq)
        best = self._day_peak - self._base
        self.lock_floor = None
        if best >= settings.profit_lock_start_usd:
            floor = self._base + best * settings.profit_lock_keep_pct / 100
            self.lock_floor = round(floor, 2)
            if eq < floor:
                self._hard_floor = max(self._hard_floor or 0, floor)
                self._pause_until = now + settings.profit_lock_pause_min * 60
                self._base, self._day_peak = eq, eq  # new gains measured from here after the pause
                self.log("warn", f"تأمين الأرباح: حافظنا على {floor - st.day_start_equity:.2f}$ — استراحة {settings.profit_lock_pause_min} دقيقة")
                return "استراحة بعد تأمين الربح"
        return None

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
        frames = {} if settings.strategy_mode.upper() == "HYBRID" else {tf: self.broker.candles(symbol, tf, 300) for tf in tfs}
        t = self.broker.tick(symbol)
        if settings.strategy_mode.upper() == "HYBRID":
            from .agent.hybrid import evaluate as hybrid_eval
            df = self.broker.candles(symbol, settings.hybrid_tf, 1000)
            card = hybrid_eval(symbol, df, self.broker.spec(symbol), t["ask"] - t["bid"], self.news_block(symbol),
                               self.broker.account().equity, self.models.get(symbol))
        elif settings.strategy_mode.upper() == "SCALP":
            from .scalp import evaluate as scalp_eval
            card = scalp_eval(symbol, frames, self.broker.spec(symbol), t["ask"] - t["bid"], self.news_block(symbol),
                              equity=self.broker.account().equity)
        else:
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
        card = self._maybe_boost(card, source)
        t = self.broker.tick(sym)
        entry = t["ask"] if d == "BUY" else t["bid"]
        spec, acc = self.broker.spec(sym), self.broker.account()
        margin = None
        if hasattr(self.broker, "margin_required"):
            margin = self.broker.margin_required(sym, d, card["lots"], entry)
        tg = source.startswith("TG")
        group = [p for p in self.broker.positions() if self._is_tg(p) == tg]  # Telegram and bot trades are separate books
        errs = risk_engine.validate(symbol=sym, direction=d, entry=entry, sl=card["sl"], tp=card["tp3"],
                                    lots=card["lots"], spec=spec, acc=acc, open_positions=group,
                                    spread=t["ask"] - t["bid"], margin_required=margin,
                                    per_symbol_limit=settings.tg_max_per_symbol if tg else None)
        if errs:
            self.log("warn", "محرك المخاطر رفض الأمر", symbol=sym, errors=errs)
            return {"ok": False, "message": "؛ ".join(errs)}
        client_id = card["id"]
        res = self.broker.market_order(sym, d, card["lots"], card["sl"], card["tp3"], f"{source}", client_id)
        audit("order", source=source, card_id=client_id, symbol=sym, direction=d, lots=card["lots"], result=res)
        if res.get("ok"):
            scalp = card.get("strategy") == "SCALP"
            cd = settings.scalp_cooldown_min if scalp else settings.reentry_cooldown_min
            self.cooldown_until[sym] = max(self.cooldown_until.get(sym, 0), time.time() + cd * 60)
            self.plans[res["ticket"]] = {"tp1": card["tp1"], "tp2": card["tp2"], "entry": entry, "sl": card["sl"],
                                         "atr": card.get("atr_setup", 0), "direction": d, "partial_done": False,
                                         "scalp": scalp, "source": source, "hybrid": card.get("strategy") == "HYBRID",
                                         "stage": 0, "orig_volume": card["lots"], "peak": entry}
            try:
                from .agent.stats import ledger
                rm = (card.get("risk") or {}).get("risk_money") or abs(entry - card["sl"]) / spec.tick_size * spec.tick_value * card["lots"]
                ledger.on_open(res["ticket"], sym, source, card.get("strategy") or card.get("grade"), rm, card.get("meta_p"))
            except Exception as e:
                self.log("warn", f"سجل الأداء: {e}")
            self.log("info", f"تم تنفيذ {d} {sym} {card['lots']} لوت — {res.get('message', '')}", ticket=res["ticket"])
        else:
            self.log("error", "فشل التنفيذ", symbol=sym, result=res)
        return res

    def _maybe_boost(self, card: dict, source: str) -> dict:
        """Anti-martingale: raise size only on a STRONG setup while the day is green
        and the last trade was not a loss. Never after losses."""
        st = risk_engine.state
        base = settings.risk_per_trade_pct
        boost = min(settings.boost_risk_pct, settings.max_risk_per_trade_pct)
        try:
            eq = self.broker.account().equity
        except Exception:
            return card
        green = st.day_start_equity is not None and eq > st.day_start_equity
        if (source == "telegram" or card.get("grade") != "STRONG" or not green
                or st.consecutive_losses > 0 or boost <= base):
            return card
        spec = self.broker.spec(card["symbol"])
        lots = round_volume(card["lots"] * boost / base, spec)
        if lots > card["lots"]:
            self.log("info", f"رفع اللوت (دخول قوي + اليوم رابح) {card['lots']} → {lots}", symbol=card["symbol"])
            card = {**card, "lots": lots}
        return card

    def manage_positions(self) -> None:
        """TP1: close 50% + break-even. After TP1: ATR trailing. Stale/weekend exits.
        A stop is only ever moved in the trade's favour, never widened."""
        now_utc = datetime.now(timezone.utc)
        weekend_close = settings.close_before_weekend and now_utc.weekday() == 4 and now_utc.hour >= 20
        for p in self.broker.positions():
            plan = self.plans.get(p["ticket"]) or self._adopt(p)
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
            if plan.get("hybrid"):
                self._manage_hybrid(p, plan, buy, px, mkt_time)
                continue
            if plan.get("scalp"):
                if mkt_time - p["time_open"] > settings.scalp_max_minutes * 60:
                    self.broker.close(p["ticket"], reason="scalp timeout")
                    self.log("info", "سكالب: انتهى الوقت — إغلاق", ticket=p["ticket"])
                    continue
                # 70% of the way to target -> lock break-even (+spread) so a winner can't turn into a loss
                tgt, ent = plan["tp1"], plan["entry"]
                if not plan["partial_done"] and abs(tgt - ent) > 0 and (px - ent) / (tgt - ent) >= 0.7:
                    lock = ent + 0.15 * (tgt - ent)
                    if self.broker.modify(p["ticket"], lock, None).get("ok"):
                        plan["partial_done"], plan["sl"] = True, lock
                        self.log("info", "سكالب: قفل الربح عند التعادل+", ticket=p["ticket"])
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

    def _manage_hybrid(self, p: dict, plan: dict, buy: bool, px: float, mkt_time: float) -> None:
        """Playbook exits: 40% at +1.5R & stop to break-even; 40% at +3R; runner on a chandelier trail;
        vertical time-stop if the trade never reached +1.5R. Stops only ever move in the trade's favour."""
        spec = self.broker.spec(p["symbol"])
        sgn = 1 if buy else -1
        orig = plan.get("orig_volume") or p["volume"]
        chunk = math.floor(orig * 0.4 / spec.volume_step + 1e-9) * spec.volume_step
        can_part = chunk >= spec.volume_min and p["volume"] - chunk >= spec.volume_min - 1e-9
        plan["peak"] = max(plan.get("peak", px), px) if buy else min(plan.get("peak", px), px)
        if plan["stage"] == 0:
            if mkt_time - p["time_open"] > settings.hybrid_horizon_bars * 15 * 60:
                self.broker.close(p["ticket"], reason="time stop")
                self.log("info", "الوكيل: وقف زمني — الصفقة ما تحركت", ticket=p["ticket"])
                return
            if (px - plan["tp1"]) * sgn >= 0:
                if can_part:
                    self.broker.close(p["ticket"], round(chunk, 8), reason="TP1 40%")
                if self.broker.modify(p["ticket"], plan["entry"], None).get("ok"):
                    plan["sl"] = plan["entry"]
                plan["stage"] = 1
                self.log("info", "الوكيل: +1.5R — إغلاق 40% ونقل الوقف للتعادل" if can_part
                         else "الوكيل: +1.5R — نقل الوقف للتعادل (اللوت صغير على التقسيم)", ticket=p["ticket"])
            return
        if plan["stage"] == 1 and (px - plan["tp2"]) * sgn >= 0:
            if can_part:
                self.broker.close(p["ticket"], round(chunk, 8), reason="TP2 40%")
            plan["stage"] = 2
            self.log("info", "الوكيل: +3R — إغلاق 40% إضافية، الباقي يمشي بوقف متحرك", ticket=p["ticket"])
        trail = settings.hybrid_trail_atr * (plan.get("atr") or 0)
        if trail > 0:
            new_sl = plan["peak"] - sgn * trail
            if (new_sl - plan["sl"]) * sgn > 0 and (px - new_sl) * sgn > 0:
                if self.broker.modify(p["ticket"], new_sl, None).get("ok"):
                    plan["sl"] = new_sl

    def train_models(self, symbols=None) -> dict:
        """Fit/refresh the meta-label model per symbol from the broker's own history. Persisted in kv."""
        from .agent import meta as M
        from . import storage
        out = {}
        for sym in symbols or settings.symbol_list:
            try:
                cur = self.models.get(sym)
                if cur is None:
                    raw = storage.kv_get(f"meta2:{sym}")
                    if raw:
                        cur = self.models[sym] = M.MetaModel.from_json(raw)
                if cur is not None and time.time() - cur.trained < settings.meta_retrain_hours * 3600:
                    out[sym] = {"n": cur.n, "base": round(cur.base, 3), "auc": cur.auc, "cached": True}
                    continue
                df = self.history_candles(sym, settings.hybrid_tf, settings.meta_train_bars)
                m = M.train(df)
                if m is None:
                    out[sym] = {"error": "أحداث قليلة"}
                    continue
                self.models[sym] = m
                storage.kv_set(f"meta2:{sym}", m.to_json())
                out[sym] = {"n": m.n, "base": round(m.base, 3), "auc": round(m.auc, 3) if m.auc else None}
                self.log("info", f"الوكيل: تدرّب نموذج {sym} على {m.n} حدث (نسبة النجاح الأساسية {m.base:.0%}، AUC {m.auc and round(m.auc, 2)})")
            except Exception as e:
                out[sym] = {"error": str(e)[:200]}
                self.log("error", f"تدريب نموذج {sym} فشل: {e}")
        return out

    def history_candles(self, symbol: str, tf: str, total: int):
        fn = getattr(self.broker, "candles_history", None)
        return fn(symbol, tf, total) if fn else self.broker.candles(symbol, tf, total)

    def _adopt(self, p: dict) -> dict | None:
        """Rebuild a management plan for a position opened before a restart, from the
        broker's own state (entry, SL). Never widens anything; if no SL, leave it alone."""
        sl, entry = p.get("sl"), p.get("price_open")
        if not sl or not entry:
            return None
        if settings.strategy_mode.upper() == "HYBRID" and not self._is_tg(p):
            buy = p["direction"] == "BUY"
            sgn = 1 if buy else -1
            risk = abs(entry - sl)
            be = (sl - entry) * sgn >= 0
            if be:  # stop already at/after entry: rebuild R from the TP if we can, else skip staging
                risk = abs((p.get("tp") or entry) - entry) / settings.hybrid_tp3_r or risk
            plan = {"tp1": entry + sgn * settings.hybrid_tp1_r * risk, "tp2": entry + sgn * settings.hybrid_tp2_r * risk,
                    "entry": entry, "sl": sl, "atr": risk / settings.hybrid_sl_atr, "direction": p["direction"],
                    "partial_done": be, "hybrid": True, "stage": 1 if be else 0, "orig_volume": p["volume"],
                    "peak": p["price_current"], "source": "AUTO"}
            self.plans[p["ticket"]] = plan
            return plan
        if settings.strategy_mode.upper() == "SCALP" and p.get("tp"):
            plan = {"tp1": p["tp"], "tp2": p["tp"], "entry": entry, "sl": sl, "atr": 0, "direction": p["direction"],
                    "partial_done": ((sl >= entry) if p["direction"] == "BUY" else (sl <= entry)), "scalp": True}
            self.plans[p["ticket"]] = plan
            return plan
        buy = p["direction"] == "BUY"
        risk = (entry - sl) if buy else (sl - entry)
        try:
            from .indicators import atr as atr_fn
            a = float(atr_fn(self.broker.candles(p["symbol"], settings.tf_setup, 100)).iloc[-1])
        except Exception:
            a = abs(risk) or 0
        be_done = risk <= 0  # SL already at/after entry -> TP1 stage already passed
        sgn = 1 if buy else -1
        plan = {"tp1": entry + sgn * settings.tp1_r * abs(risk), "tp2": entry + sgn * settings.tp2_r * abs(risk),
                "entry": entry, "sl": sl, "atr": a, "direction": p["direction"], "partial_done": be_done}
        self.plans[p["ticket"]] = plan
        self.log("info", "استلام إدارة صفقة مفتوحة بعد إعادة التشغيل", ticket=p["ticket"], symbol=p["symbol"],
                 stage="تتبع الوقف" if be_done else "بانتظار TP1")
        return plan

    def on_card(self, card: dict) -> None:
        prev = self.signals.get(card["symbol"])
        self.signals[card["symbol"]] = card
        if card["grade"] == "NO_TRADE":
            return
        scalp = card.get("strategy") in ("SCALP", "HYBRID")
        if self.profit_lock():
            return
        if card.get("strategy") == "HYBRID":
            bt = self.backtests.get(card["symbol"]) or {}
            oos = bt.get("out_of_sample") or {}
            if bt.get("ok") and (oos.get("expectancy_r") or 0) <= 0:
                card["gates_failed"] = card.get("gates_failed", []) + ["الاختبار خارج العينة سالب — الزوج موقوف للبوت"]
                card["auto_ok"] = False
                return
        if scalp:
            cap = settings.hybrid_max_open if card.get("strategy") == "HYBRID" else settings.scalp_max_open
            pos = [p for p in self.broker.positions() if not self._is_tg(p)]
            if any(p["symbol"] == card["symbol"] for p in pos) or len(pos) >= cap:
                return
        elif prev and prev.get("grade") == card["grade"] and prev.get("direction") == card["direction"]:
            return  # same setup still standing; avoid duplicate orders
        wait = self.cooldown_until.get(card["symbol"], 0) - time.time()
        if wait > 0:
            return  # cooling down: avoid churn / revenge re-entries
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
                await asyncio.to_thread(self._iteration)
            except Exception as e:
                self.status.update(loop_ok=False, error=str(e))
                self.log("error", f"خطأ في الحلقة: {e}")
                await asyncio.sleep(10)  # back off; never hammer the broker
            await self.broadcast()
            await asyncio.sleep(interval)

    def _iteration(self) -> None:
        self.broker.step()
        acc = self.broker.account()
        today = datetime.now(timezone.utc)
        risk_engine.update_equity(acc, new_day=False)
        closed_before = len(self.broker.history(1000))
        self.manage_positions()
        from .main_hours import fx_market_open
        always = {x.strip().upper() for x in settings.always_open_symbols.split(",") if x.strip()}
        fx_open = fx_market_open()
        for sym in settings.symbol_list:
            if sym not in always and not fx_open:
                continue  # gold/FX closed: crypto keeps trading
            try:
                etf = settings.hybrid_tf if settings.strategy_mode.upper() == "HYBRID" else settings.tf_entry
                m5 = self.broker.candles(sym, etf, 2)
                last = float(m5["time"].iloc[-1])
                if self.last_m5.get(sym) != last:  # evaluate once per closed entry-TF candle
                    self.last_m5[sym] = last
                    self.on_card(self.analyze_symbol(sym))
            except Exception as e:  # one bad symbol must not stop the loop
                self.log("error", f"تحليل {sym} فشل: {e}")
        new_hist = self.broker.history(1000)
        fresh = new_hist[: max(0, len(new_hist) - closed_before)]
        if fresh:
            try:
                from .agent.stats import ledger
                live = {str(p["ticket"]) for p in self.broker.positions()}
                for tk in {str(h.get("ticket")) for h in fresh}:
                    if tk not in live:
                        ledger.on_close(tk, sum(h.get("profit", 0) for h in new_hist if str(h.get("ticket")) == tk))
            except Exception as e:
                self.log("warn", f"سجل الأداء: {e}")
        for h in fresh:
            risk_engine.record_result(h.get("profit", 0))
            if h.get("profit", 0) < 0 and h.get("symbol"):
                lc = settings.scalp_loss_cooldown_min if settings.strategy_mode.upper() == "SCALP" else settings.loss_cooldown_min
                self.cooldown_until[h["symbol"]] = max(self.cooldown_until.get(h["symbol"], 0), time.time() + lc * 60)
        self.status.update(loop_ok=True, last_loop=today.timestamp(), error=None)
        if time.time() - getattr(self, "_hb", 0) > 300:  # heartbeat every 5 min
            self._hb = time.time()
            grades = {k: f"{v.get('grade')}:{v.get('score')}" for k, v in self.signals.items()}
            print("HEARTBEAT", json.dumps({"equity": acc.equity, "balance": acc.balance,
                  "open": len(self.broker.positions()), "permitted": self.trading_permitted(),
                  "mode": self.execution_mode, "signals": grades}, ensure_ascii=False, default=str), flush=True)

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
        msg = await asyncio.to_thread(lambda: json.dumps(self.snapshot(), ensure_ascii=False, default=str))
        for q in list(self.subscribers):
            if q.qsize() < 5:
                q.put_nowait(msg)

    def new_client_id(self) -> str:
        return uuid.uuid4().hex[:12]
