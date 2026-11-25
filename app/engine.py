"""Orchestrator: runs the loop, owns signals, enforces execution modes and live-trading gating."""
from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from datetime import datetime, timedelta, timezone
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


def bt_mode(bt: dict) -> str:
    """Let the walk-forward decide how this pair/TF trades: with the model filter, on raw signals, or minimal size."""
    if not bt.get("ok"):
        return "model"
    f = (bt.get("out_of_sample") or {}).get("expectancy_r")
    r = (bt.get("unfiltered") or {}).get("expectancy_r")
    if f is not None and f > 0 and (r is None or f >= r):
        return "model"
    if r is not None and r > 0:
        return "raw"
    return "min"


def P_RR() -> float:
    from .sniper import P
    return P["rr"]


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

    def daily_target_hit(self, acc=None) -> bool:
        """Net daily target on the UAE calendar day. On hit: close every bot/Telegram position and stop
        new entries until the next UAE day."""
        if settings.daily_target_usd <= 0:
            return False
        try:
            acc = acc or self.broker.account()
        except Exception:
            return False
        day = (datetime.now(timezone.utc) + timedelta(hours=4)).date()
        if getattr(self, "_tday", None) != day:
            self._tday, self._tday_start, self._tday_hit = day, acc.equity, False
        if self._tday_hit:
            return True
        got = acc.equity - self._tday_start
        if got >= settings.daily_target_usd:
            self._tday_hit = True
            for p in self.broker.positions():
                self.broker.close(p["ticket"], reason="daily target")
            self.log("info", f"🎯 تحقق هدف اليوم +{got:.2f}$ — إغلاق كل الصفقات ووقف التداول لين بكرة")
        return self._tday_hit

    def hourly_done(self) -> bool:
        """Hourly challenge: once equity is up hourly_target_usd since the top of the hour, stop opening new
        trades until the next hour (open trades keep being managed). Never raises risk to catch up."""
        if settings.hourly_target_usd <= 0:
            return False
        try:
            eq = self.broker.account().equity
        except Exception:
            return False
        hr = int(time.time() // 3600)
        if getattr(self, "_hr", None) != hr:
            prev = getattr(self, "_hr_start", None)
            if prev is not None:
                got = eq - prev
                self.hourly_log = (getattr(self, "hourly_log", []) + [{"hour": hr - 1, "pnl": round(got, 2)}])[-48:]
                self.log("info", f"نتيجة الساعة: {got:+.2f}$ من هدف {settings.hourly_target_usd}$ "
                                 f"({'✅ تحقق' if got >= settings.hourly_target_usd else '❌ ما تحقق'})")
            self._hr, self._hr_start, self._hr_hit = hr, eq, False
        if not self._hr_hit and eq - self._hr_start >= settings.hourly_target_usd:
            self._hr_hit = True
            self.log("info", f"🎯 هدف الساعة تحقق (+{eq - self._hr_start:.2f}$) — وقف دخول جديد لين الساعة الجاية")
        return self._hr_hit

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
    def analyze_symbol(self, symbol: str, tf: str | None = None, key: str | None = None) -> dict:
        tfs = (settings.tf_dominant, settings.tf_confirm, settings.tf_setup, settings.tf_entry)
        frames = {} if settings.strategy_mode.upper() == "HYBRID" else {tf: self.broker.candles(symbol, tf, 300) for tf in tfs}
        t = self.broker.tick(symbol)
        if settings.strategy_mode.upper() == "HYBRID":
            from .agent.hybrid import evaluate as hybrid_eval
            tf = tf or settings.hybrid_tf
            key = key or symbol
            df = self.broker.candles(symbol, tf, 1000)
            bt = self.backtests.get(key) or {}
            allowed = None
            if bt.get("by_type"):  # only entry types with a positive recent edge on this market/TF
                allowed = {k for k, m in bt["by_type"].items() if m.get("trades", 0) >= 15 and (m.get("expectancy_r") or 0) > 0}
            card = hybrid_eval(symbol, df, self.broker.spec(symbol), t["ask"] - t["bid"], self.news_block(symbol),
                               self.broker.account().equity, self.models.get(key), mode=bt_mode(bt), allowed=allowed)
            card["tf"], card["key"] = tf, key
            if tf != settings.hybrid_tf:
                card["headline"] = f"[{tf} طويل] " + card["headline"]
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
        if getattr(self, "_tday_hit", False):
            return {"ok": False, "message": "تحقق هدف اليوم — التداول موقوف لين بكرة"}
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
                                    per_symbol_limit=settings.tg_max_per_symbol if tg else None,
                                    max_risk_pct=settings.tg_minlot_max_risk_pct if tg else None)
        if errs:
            self.log("warn", "محرك المخاطر رفض الأمر", symbol=sym, errors=errs)
            return {"ok": False, "message": "؛ ".join(errs)}
        client_id = card["id"]
        res = self.broker.market_order(sym, d, card["lots"], card["sl"], card["tp3"], f"{source}", client_id)
        audit("order", source=source, card_id=client_id, symbol=sym, direction=d, lots=card["lots"], result=res)
        if res.get("ok"):
            scalp = card.get("strategy") == "SCALP"
            cd = settings.scalp_cooldown_min if scalp else settings.reentry_cooldown_min
            ck = card.get("key") or sym
            self.cooldown_until[ck] = max(self.cooldown_until.get(ck, 0), time.time() + cd * 60)
            self.plans[res["ticket"]] = {"tp1": card["tp1"], "tp2": card["tp2"], "entry": entry, "sl": card["sl"],
                                         "atr": card.get("atr_setup", 0), "direction": d, "partial_done": False,
                                         "scalp": scalp, "source": source, "hybrid": card.get("strategy") == "HYBRID",
                                         "stage": 0, "orig_volume": card["lots"], "peak": entry, "tf": card.get("tf")}
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
        self._basket_take()
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
            if not plan.get("hybrid") and mkt_time - p["time_open"] > settings.max_hold_minutes * 60:
                self.broker.close(p["ticket"], reason="max hold")
                self.log("info", "إغلاق: تجاوزت أقصى مدة", ticket=p["ticket"])
                continue
            if plan.get("sniper"):
                self._manage_sniper(p, plan, buy, px)
                continue
            if plan.get("hybrid"):
                self._manage_hybrid(p, plan, buy, px, mkt_time)
                continue
            if plan.get("scalp"):
                spec = self.broker.spec(p["symbol"])
                sgn = 1 if buy else -1
                per = spec.tick_value / spec.tick_size * p["volume"]  # $ per 1.0 price move for this position
                ent = plan["entry"]
                pnl = (px - ent) * sgn * per
                plan["peak_usd"] = max(plan.get("peak_usd", 0.0), pnl)
                peak = plan["peak_usd"]
                if pnl <= 0 and mkt_time - p["time_open"] > settings.scalp_max_minutes * 60:
                    self.broker.close(p["ticket"], reason="scalp timeout")
                    self.log("info", "سكالب: ما تحركت — إغلاق", ticket=p["ticket"])
                    continue
                # profit ratchet: once +$0.40, never let it close red; keep 60% of the best open profit
                if peak >= settings.scalp_lock_start_usd:
                    keep = max(0.05, peak * settings.scalp_lock_keep)
                    lock = ent + sgn * keep / per
                    if (lock - plan["sl"]) * sgn > 0 and (px - lock) * sgn > 0:
                        if self.broker.modify(p["ticket"], round(lock, spec.digits), None).get("ok"):
                            first = not plan.get("partial_done")
                            plan["sl"], plan["partial_done"] = lock, True
                            if first:
                                self.log("info", f"سكالب: تأمين الربح +{keep:.2f}$", ticket=p["ticket"])
                    elif (px - lock) * sgn <= 0 and pnl > 0:
                        # price already gave back below the lock level: bank what is left now
                        self.broker.close(p["ticket"], reason="ratchet")
                        self.log("info", f"سكالب: حجز الربح +{pnl:.2f}$", ticket=p["ticket"])
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
            from .brokers.base import TIMEFRAMES
            if mkt_time - p["time_open"] > settings.hybrid_horizon_bars * TIMEFRAMES.get(plan.get("tf") or settings.hybrid_tf, 15) * 60:
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

    # ---- "BTC Smart Re-Entry Sniper" (user's Pine strategy) ---------------------------------------
    def _sniper_step(self, sym: str) -> None:
        from . import sniper as SN
        st = self.__dict__.setdefault("sn_state", {}).setdefault(sym, {})
        pend = self.__dict__.setdefault("sn_pending", {})
        spec = self.broker.spec(sym)
        t = self.broker.tick(sym)
        mine = [p for p in self.broker.positions() if p["symbol"] == sym and (self.plans.get(p["ticket"]) or {}).get("sniper")]
        # 1) a pending stop entry from the last closed bar: fire if price confirms, else expire after one bar
        pd_ = pend.get(sym)
        if pd_ and not mine:
            px = t["ask"] if pd_["dir"] > 0 else t["bid"]
            if time.time() > pd_["expires"]:
                pend.pop(sym, None)
            elif (px - pd_["trig"]) * pd_["dir"] >= 0:
                pend.pop(sym, None)
                self._sniper_fire(sym, pd_, px, spec)
        # 2) on each closed bar: tick the bar counters and look for a new setup
        df = self.broker.candles(sym, settings.sniper_tf, 300)
        last = float(df["time"].iloc[-1])
        k = f"sn:{sym}"
        if self.last_m5.get(k) == last:
            return
        self.last_m5[k] = last
        for c in ("shadow", "cooldown"):
            if st.get(c, 0) > 0:
                st[c] -= 1
        if mine or self.daily_target_hit():
            return
        d = SN.prepare(df)
        sig = SN.signal_at(d, len(d) - 1, st, spec.tick_size)
        card = {"symbol": sym, "strategy": "SNIPER", "grade": "SNIPER" if sig else "NO_TRADE", "score": 100 if sig else 0,
                "direction": ("BUY" if sig["dir"] > 0 else "SELL") if sig else None, "created": time.time(),
                "headline": (f"{sig['why']} — أمر إيقاف عند {sig['trig']:.2f}") if sig else "القناص: يترصد",
                "gates_failed": [] if sig else ["ما في سحب سيولة بفوليوم"]}
        self.signals[sym] = card
        if sig:
            pend[sym] = {**sig, "expires": time.time() + 2 * 60 * {"M1": 1, "M5": 5, "M15": 15}.get(settings.sniper_tf, 1)}
            self.log("info", f"القناص {sym}: {sig['why']} — ينتظر التأكيد عند {sig['trig']:.2f}")

    def _sniper_fire(self, sym: str, sg: dict, px: float, spec) -> None:
        per = spec.tick_value / spec.tick_size
        sgn = sg["dir"]
        risk = abs(px - sg["sl"])
        if (px - sg["sl"]) * sgn <= 0:
            return
        budget = settings.scalp_sl_usd
        lots = math.floor(budget / (risk * per) / spec.volume_step + 1e-9) * spec.volume_step
        lots = round(max(lots, spec.volume_min), 8)
        loss = lots * risk * per
        if loss > budget * settings.scalp_sl_cap_mult:
            self.log("info", f"القناص {sym}: الستوب يخاطر {loss:.2f}$ — أكبر من حدك، تخطي")
            return
        tp = px + sgn * P_RR() * risk
        card = {"id": f"sn{int(time.time())}", "symbol": sym, "direction": "BUY" if sgn > 0 else "SELL", "grade": "SNIPER",
                "strategy": "SNIPER", "lots": lots, "sl": round(sg["sl"], spec.digits), "tp1": px + sgn * 0.6 * risk,
                "tp2": tp, "tp3": round(tp, spec.digits), "atr_setup": sg["atr"], "risk": {"risk_money": round(loss, 2)}}
        res = self.execute(card, "SNIPER")
        if res.get("ok"):
            plan = self.plans.get(res["ticket"])
            if plan is not None:
                plan.update(sniper=True, scalp=False, risk=risk, be=False)
            self.sn_state.setdefault(sym, {})["last_dir"] = sgn
            self.log("info", f"القناص {sym}: دخل {card['direction']} {lots} — وقف {loss:.2f}$، هدف 1.8R")

    def _manage_sniper(self, p: dict, plan: dict, buy: bool, px: float) -> None:
        sgn = 1 if buy else -1
        ent, risk = plan["entry"], plan.get("risk") or abs(plan["entry"] - plan["sl"])
        if not plan.get("be") and (px - ent) * sgn >= 0.6 * risk:
            be = ent + sgn * 0.1 * risk
            if self.broker.modify(p["ticket"], be, None).get("ok"):
                plan.update(sl=be, be=True)
                self.log("info", "القناص: تأمين الدخول عند +0.6R", ticket=p["ticket"])
        elif plan.get("be"):
            tr = px - sgn * 1.2 * (plan.get("atr") or 0)
            if plan.get("atr") and (tr - plan["sl"]) * sgn > 0 and (px - tr) * sgn > 0:
                if self.broker.modify(p["ticket"], tr, None).get("ok"):
                    plan["sl"] = tr

    def _pnl_usd(self, p: dict) -> float:
        spec = self.broker.spec(p["symbol"])
        sgn = 1 if p["direction"] == "BUY" else -1
        return (p["price_current"] - p["price_open"]) * sgn * spec.tick_value / spec.tick_size * p["volume"]

    def _basket_take(self) -> None:
        """User rule: bank small sums fast. If the bot's open scalps together are up >= basket_tp_usd, close them all."""
        if settings.basket_tp_usd <= 0:
            return
        pos = [p for p in self.broker.positions() if (self.plans.get(p["ticket"]) or {}).get("scalp") and not self._is_tg(p)]
        if len(pos) < 2:
            return
        tot = sum(self._pnl_usd(p) for p in pos)
        if tot >= settings.basket_tp_usd:
            for p in pos:
                self.broker.close(p["ticket"], reason="basket take")
            self.log("info", f"💰 حجز جماعي: {len(pos)} صفقات مجموعها +{tot:.2f}$ — إغلاق وفتح فرص جديدة")

    def train_models(self, symbols=None) -> dict:
        """Fit/refresh the meta-label model per symbol from the broker's own history. Persisted in kv."""
        from .agent import meta as M
        from . import storage
        out = {}
        for key, sym, tf in settings.agent_keys:
            if symbols and sym not in symbols:
                continue
            try:
                cur = self.models.get(key)
                if cur is None:
                    raw = storage.kv_get(f"meta3:{key}")
                    if raw:
                        cur = M.MetaModel.from_json(raw)
                        from .agent.features import FEATURES
                        cur = self.models[key] = cur if len(cur.mu) == len(FEATURES) else None
                if cur is not None and time.time() - cur.trained < settings.meta_retrain_hours * 3600:
                    out[key] = {"n": cur.n, "base": round(cur.base, 3), "auc": cur.auc, "cached": True}
                    continue
                df = self.history_candles(sym, tf, settings.meta_train_bars)
                m = M.train(df)
                if m is None:
                    out[key] = {"error": "أحداث قليلة"}
                    continue
                self.models[key] = m
                storage.kv_set(f"meta3:{key}", m.to_json())
                out[key] = {"n": m.n, "base": round(m.base, 3), "auc": round(m.auc, 3) if m.auc else None}
                self.log("info", f"الوكيل: تدرّب نموذج {key} على {m.n} حدث (نسبة النجاح الأساسية {m.base:.0%}، AUC {m.auc and round(m.auc, 2)})")
            except Exception as e:
                out[key] = {"error": str(e)[:200]}
                self.log("error", f"تدريب نموذج {key} فشل: {e}")
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
        key = card.get("key") or card["symbol"]
        prev = self.signals.get(key)
        self.signals[key] = card
        if card["grade"] == "NO_TRADE":
            return
        scalp = card.get("strategy") in ("SCALP", "HYBRID")
        if settings.profit_lock_enabled and self.profit_lock():
            return
        if self.hourly_done() or self.daily_target_hit():
            return
        if card.get("strategy") == "HYBRID":
            bt = self.backtests.get(key) or {}
            oos = bt.get("out_of_sample") or {}
            if settings.evidence_gate and bt.get("ok") and (oos.get("expectancy_r") or 0) <= 0:
                card["gates_failed"] = card.get("gates_failed", []) + ["الاختبار خارج العينة سالب — الزوج موقوف للبوت"]
                card["auto_ok"] = False
                return
        if scalp:
            cap = settings.hybrid_max_open if card.get("strategy") == "HYBRID" else settings.scalp_max_open
            pos = [p for p in self.broker.positions() if not self._is_tg(p)]
            def ptf(p):
                return (self.plans.get(p["ticket"]) or {}).get("tf") or settings.hybrid_tf
            same = [p for p in pos if p["symbol"] == card["symbol"] and ptf(p) == (card.get("tf") or ptf(p))]
            per_sym = settings.scalp_per_symbol if card.get("strategy") == "SCALP" else 1
            if len(same) >= per_sym or len(pos) >= cap:
                return
            if card.get("strategy") == "SCALP":
                if card["symbol"] != "BTCUSD":
                    a_, b_ = (int(x) for x in settings.fx_session_utc.split("-"))
                    if not (a_ <= datetime.now(timezone.utc).hour < b_):
                        return  # thin hours: FX/gold spreads eat scalps
                if card["symbol"] in ("EURUSD", "GBPUSD"):
                    usd_dir = lambda sym, d: (1 if d == "BUY" else -1)  # both quote USD: BUY = short USD
                    mine = usd_dir(card["symbol"], card["direction"])
                    grp = [p for p in pos if p["symbol"] in ("EURUSD", "GBPUSD") and usd_dir(p["symbol"], p["direction"]) == mine]
                    if len(grp) >= settings.usd_group_max:
                        return
        elif prev and prev.get("grade") == card["grade"] and prev.get("direction") == card["direction"]:
            return  # same setup still standing; avoid duplicate orders
        wait = self.cooldown_until.get(key, 0) - time.time()
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
        closed_before = len(self.broker.history(1000))
        # deposit / withdrawal / demo reset: balance moves with no new closed deal -> rebase, not a loss
        lb, ln = getattr(self, "_last_bal", None), getattr(self, "_last_hist_n", None)
        if acc.equity > 0 and lb and ln == closed_before and abs(acc.balance - lb) > max(1.0, 0.02 * lb):
            st = risk_engine.state
            st.day_start_equity = st.week_start_equity = st.peak_equity = acc.equity
            if st.kill_switch and any(w in st.kill_reason for w in ("تراجع", "اليومي", "الأسبوعي")):
                risk_engine.release()
            self.log("info", f"تغيّر الرصيد بدون صفقات ({lb:.2f}$ ← {acc.balance:.2f}$) — إيداع/سحب/إعادة ضبط، إعادة احتساب الحدود")
        self._last_bal, self._last_hist_n = acc.balance, closed_before
        self.hourly_done()  # keeps the hourly scoreboard ticking
        self.daily_target_hit(acc)
        st = risk_engine.state
        if (st.kill_switch and settings.auto_resume_min > 0 and "يدوي" not in st.kill_reason
                and any(w in st.kill_reason for w in ("تراجع", "اليومي", "الأسبوعي", "متتالية", "خسائر"))
                and time.time() - st.kill_time > settings.auto_resume_min * 60 and acc.equity > 0):
            why = st.kill_reason
            st.day_start_equity = st.week_start_equity = st.peak_equity = acc.equity
            st.consecutive_losses = 0
            risk_engine.release()
            self.log("warn", f"استئناف تلقائي بعد استراحة {settings.auto_resume_min} دقيقة ({why}) — الحدود من الرصيد الحالي {acc.equity:.2f}$")
        if acc.equity > 0:  # ignore transient zero/invalid equity reads
            risk_engine.update_equity(acc, new_day=False)
        self.manage_positions()
        from .main_hours import fx_market_open
        always = {x.strip().upper() for x in settings.always_open_symbols.split(",") if x.strip()}
        fx_open = fx_market_open()
        hybrid = settings.strategy_mode.upper() == "HYBRID"
        jobs = settings.agent_keys if hybrid else [(s_, s_, settings.tf_entry) for s_ in settings.symbol_list]
        snipe = {x.strip().upper() for x in settings.sniper_symbols.split(",") if x.strip()} \
            if settings.strategy_mode.upper() == "SCALP" else set()
        for sym in snipe:
            try:
                self._sniper_step(sym)
            except Exception as e:
                self.log("error", f"القناص {sym} فشل: {e}")
        for key, sym, etf in jobs:
            if sym not in always and not fx_open:
                continue  # gold/FX closed: crypto keeps trading
            if sym in snipe:
                continue
            try:
                m5 = self.broker.candles(sym, etf, 2)
                last = float(m5["time"].iloc[-1])
                if self.last_m5.get(key) != last:  # evaluate once per closed entry-TF candle
                    self.last_m5[key] = last
                    self.on_card(self.analyze_symbol(sym, etf, key) if hybrid else self.analyze_symbol(sym))
            except Exception as e:  # one bad symbol must not stop the loop
                self.log("error", f"تحليل {key} فشل: {e}")
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
            sym_h = h.get("symbol")
            if sym_h and sym_h in getattr(self, "sn_state", {}):
                from . import sniper as _SN
                _SN.on_closed(self.sn_state[sym_h], h.get("profit", 0))
            if sym_h:
                st_ = getattr(self, "sym_streak", {})
                st_[sym_h] = st_.get(sym_h, 0) + 1 if h.get("profit", 0) < 0 else 0
                self.sym_streak = st_
            if h.get("profit", 0) < 0 and h.get("symbol"):
                lc = settings.scalp_loss_cooldown_min if settings.strategy_mode.upper() == "SCALP" else settings.loss_cooldown_min
                if self.sym_streak.get(h["symbol"], 0) >= 2:  # market is going against the read: step back properly
                    lc = max(lc, 30)
                    self.log("warn", f"{h['symbol']}: خسارتين ورا بعض — استراحة 30 دقيقة على هالزوج")
                for ck in [k for k, sy, _ in settings.agent_keys if sy == h["symbol"]] + [h["symbol"]]:
                    self.cooldown_until[ck] = max(self.cooldown_until.get(ck, 0), time.time() + lc * 60)
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
