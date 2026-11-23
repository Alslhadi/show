"""Telegram channel follower: reads analyst channels in real time (user session via Telethon),
records every signal, tracks its outcome against real prices, and — depending on mode — executes it.

Modes (TG_MODE):
  track  : record + measure only, never trade
  smart  : trade when the channel has a proven record (>= TG_MIN_CLOSED closed signals, win rate
           >= TG_MIN_WINRATE and avg R > 0) OR when our own analysis agrees with the direction
  follow : trade every valid signal (still through the risk engine)
Every executed trade always has a stop loss: the analyst's, else one placed from market structure.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from datetime import datetime, timezone

from . import storage
from .config import settings
from .indicators import atr as atr_fn
from .risk import position_size
from .signal_parser import parse
from .smc import find_swings


def _fernet():
    from cryptography.fernet import Fernet
    secret = (settings.tg_session_key or settings.dashboard_token).encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret).digest()))


class TelegramBridge:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.client = None
        self.status = "غير مربوط"
        self.me = None
        self.channels: dict[int, str] = {}
        self._login: dict = {}
        self.recent: list[dict] = []

    # ---- session ---------------------------------------------------------------
    def configured(self) -> bool:
        return bool(settings.tg_api_id and settings.tg_api_hash)

    def _new_client(self, session_str: str = ""):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        return TelegramClient(StringSession(session_str), int(settings.tg_api_id), settings.tg_api_hash,
                              device_model="aitrader", app_version="1.0")

    async def start(self) -> None:
        if not self.configured():
            self.status = "مفاتيح تلجرام غير موجودة"
            return
        saved_mode = await asyncio.to_thread(storage.kv_get, "tg_mode")
        if saved_mode in ("track", "smart", "follow"):
            settings.tg_mode = saved_mode
        enc = await asyncio.to_thread(storage.kv_get, "tg_session")
        if not enc:
            self.status = "غير مربوط — سجّل الدخول من اللوحة"
            return
        try:
            s = _fernet().decrypt(enc.encode()).decode()
        except Exception:
            self.status = "تعذّر فك الجلسة — أعد تسجيل الدخول"
            return
        self.client = self._new_client(s)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            self.status = "انتهت الجلسة — أعد تسجيل الدخول"
            return
        await self._attach()

    async def _attach(self) -> None:
        from telethon import events
        self.me = await self.client.get_me()
        wanted = [w.strip() for w in settings.tg_channels.split(",") if w.strip()]
        self.channels = {}
        async for d in self.client.iter_dialogs():
            if d.is_channel and any(w.lower() in (d.name or "").lower() for w in wanted):
                self.channels[d.id] = d.name
        if not self.channels:
            self.status = "مربوط — لم أجد القنوات المطلوبة في حسابك"
            return

        @self.client.on(events.NewMessage(chats=list(self.channels)))
        async def _on_msg(ev):
            try:
                await self.handle(self.channels.get(ev.chat_id, str(ev.chat_id)), ev.message.id, ev.message.message or "")
            except Exception as e:
                self.engine.log("error", f"تلجرام: خطأ في معالجة رسالة: {e}")

        self.status = "مربوط ويستمع"
        self.engine.log("info", f"تلجرام مربوط — القنوات: {', '.join(self.channels.values())}")

    async def send_code(self, phone: str) -> dict:
        if not self.configured():
            return {"ok": False, "message": "أضف TG_API_ID و TG_API_HASH"}
        c = self._new_client()
        await c.connect()
        sent = await c.send_code_request(phone)
        self._login = {"client": c, "phone": phone, "hash": sent.phone_code_hash, "t": time.time()}
        return {"ok": True, "message": "تم إرسال الكود إلى تطبيق تلجرام"}

    async def sign_in(self, code: str, password: str | None = None) -> dict:
        from telethon.errors import SessionPasswordNeededError
        L = self._login
        if not L or time.time() - L["t"] > 600:
            return {"ok": False, "message": "اطلب كوداً جديداً"}
        c = L["client"]
        try:
            if password:
                await c.sign_in(password=password)
            else:
                await c.sign_in(L["phone"], code, phone_code_hash=L["hash"])
        except SessionPasswordNeededError:
            return {"ok": False, "need_password": True, "message": "حسابك عليه تحقق بخطوتين — أدخل كلمة السر"}
        s = c.session.save()
        await asyncio.to_thread(storage.kv_set, "tg_session", _fernet().encrypt(s.encode()).decode())
        self._login = {}
        if self.client:
            await self.client.disconnect()
        self.client = c
        await self._attach()
        return {"ok": True, "message": self.status}

    async def logout(self) -> None:
        await asyncio.to_thread(storage.kv_del, "tg_session")
        if self.client:
            await self.client.disconnect()
        self.client, self.status, self.channels = None, "غير مربوط", {}

    # ---- signals ---------------------------------------------------------------
    def _auto_sl(self, symbol: str, side: str, price: float) -> float:
        df = self.engine.broker.candles(symbol, settings.tf_setup, 150)
        a = float(atr_fn(df).iloc[-1])
        swings = [s for s in find_swings(df, 3) if s.kind == ("L" if side == "BUY" else "H")]
        if swings:
            sl = swings[-1].price - 0.2 * a if side == "BUY" else swings[-1].price + 0.2 * a
            d = abs(price - sl)
            if 0.5 * a <= d <= 2.5 * a and ((sl < price) if side == "BUY" else (sl > price)):
                return sl
        return price - 1.5 * a if side == "BUY" else price + 1.5 * a

    def _should_execute(self, channel: str, sig: dict) -> tuple[bool, str]:
        mode = settings.tg_mode.lower()
        if mode == "track":
            return False, "وضع التسجيل فقط"
        if mode == "follow":
            return True, "وضع المتابعة الكاملة"
        st = storage.channel_stats().get(channel, {})
        if (st.get("closed", 0) >= settings.tg_min_closed and (st.get("win_rate") or 0) >= settings.tg_min_winrate
                and (st.get("avg_r") or 0) > 0):
            return True, f"سجل القناة مثبت: فوز {st['win_rate']}% ومتوسط {st['avg_r']}R"
        card = self.engine.signals.get(sig["symbol"]) or {}
        if card.get("direction") == sig["side"] and (card.get("score") or 0) >= settings.tg_agree_score:
            return True, f"تحليل النظام يوافق ({card.get('score')})"
        return False, "سجل القناة غير كافٍ وتحليل النظام لا يوافق — تسجيل فقط"

    async def handle(self, channel: str, msg_id: int, text: str) -> dict:
        p = parse(text)
        rec = {"channel": channel, "msg_id": msg_id, "text": text[:500], "kind": p["kind"]}
        self.recent = ([{"t": time.time(), "channel": channel, "text": text[:200], "parsed": p}] + self.recent)[:50]
        if p["kind"] == "INFO":
            return p
        if p["kind"] in ("BE", "CLOSE_ALL", "CLOSE_HALF"):
            await asyncio.to_thread(self._manage, channel, p["kind"])
            await asyncio.to_thread(storage.insert_signal, {**rec, "status": "done"})
            self.engine.log("info", f"تلجرام [{channel}]: أمر إدارة {p['kind']}")
            return p
        if p["kind"] == "ADD":
            last = await asyncio.to_thread(storage.list_signals, ("open",), channel, 1)
            if not last:
                return p
            l0 = last[0]
            p = {"kind": "OPEN", "symbol": l0["symbol"], "side": l0["side"], "sl": l0["sl"], "tp": l0["tp"],
                 "light": p.get("light", False)}
            self.engine.log("info", f"تلجرام [{channel}]: تعزيز {l0['side']} {l0['symbol']}")
        sym, side = p["symbol"], p["side"]
        tick = await asyncio.to_thread(self.engine.broker.tick, sym)
        price = tick["ask"] if side == "BUY" else tick["bid"]
        sl, sl_src = p.get("sl"), "analyst"
        if not sl or (side == "BUY" and sl >= price) or (side == "SELL" and sl <= price):
            sl, sl_src = await asyncio.to_thread(self._auto_sl, sym, side, price), "auto"
        risk = abs(price - sl)
        tp = p.get("tp") or (price + 2 * risk if side == "BUY" else price - 2 * risk)
        pending = p.get("pending") and not (p["entry_lo"] <= price <= p["entry_hi"])
        row = {**rec, "symbol": sym, "side": side, "entry": None if pending else price,
               "entry_lo": p.get("entry_lo"), "entry_hi": p.get("entry_hi"), "sl": sl, "sl_source": sl_src,
               "tp": tp, "status": "pending" if pending else "open"}
        stored = await asyncio.to_thread(storage.insert_signal, row)
        if not stored:
            return p
        self.engine.log("info", f"تلجرام [{channel}]: توصية {side} {sym} — وقف {round(sl, 3)} ({'المحلل' if sl_src == 'analyst' else 'تلقائي'})")
        if pending:
            return p  # tracker activates it when price reaches the zone
        await asyncio.to_thread(self._maybe_execute, stored, p.get("light", False))
        return p

    def _maybe_execute(self, sig: dict, light: bool) -> None:
        ok, why = self._should_execute(sig["channel"], sig)
        if not ok:
            storage.update_signal(sig["id"], note=why)
            return
        spec, acc = self.engine.broker.spec(sig["symbol"]), self.engine.broker.account()
        risk_pct = settings.tg_risk_pct * (0.5 if light else 1.0)
        size = position_size(acc.equity, risk_pct, sig["entry"], sig["sl"], spec)
        if size["lots"] <= 0:
            storage.update_signal(sig["id"], note=size["reason"])
            return
        r = abs(sig["entry"] - sig["sl"])
        sgn = 1 if sig["side"] == "BUY" else -1
        card = {"id": f"tg{sig['id']}x{int(time.time())}", "symbol": sig["symbol"], "direction": sig["side"],
                "grade": "TELEGRAM", "lots": size["lots"], "sl": sig["sl"], "tp1": sig["entry"] + sgn * settings.tp1_r * r,
                "tp2": sig["entry"] + sgn * settings.tp2_r * r, "tp3": sig["tp"], "atr_setup": r / 1.5}
        res = self.engine.execute(card, f"TG {sig['channel'][:12]}")
        storage.update_signal(sig["id"], executed=bool(res.get("ok")), ticket=str(res.get("ticket") or ""),
                              note=why if res.get("ok") else f"رفض: {res.get('message')}")

    def _manage(self, channel: str, kind: str) -> None:
        # virtual (tracked) signals of this channel
        price_cache: dict[str, dict] = {}
        for s in storage.list_signals(status=("open",), channel=channel):
            if s["kind"] != "OPEN" or s["entry"] is None:
                continue
            t = price_cache.setdefault(s["symbol"], self.engine.broker.tick(s["symbol"]))
            px = t["bid"] if s["side"] == "BUY" else t["ask"]
            if kind == "BE":
                if "+be" not in (s["sl_source"] or ""):
                    storage.update_signal(s["id"], sl_source=(s["sl_source"] or "") + "+be", note="أمّن المحلل الدخول")
            else:
                self._close_virtual(s, px, "closed by analyst")
        # real positions opened from this channel
        tickets = {s["ticket"] for s in storage.list_signals(channel=channel, limit=300) if s["executed"] and s["ticket"]}
        for p in self.engine.broker.positions():
            if str(p["ticket"]) not in tickets:
                continue
            if kind == "BE":
                self.engine.broker.modify(p["ticket"], p["price_open"], None)
            elif kind == "CLOSE_ALL":
                self.engine.broker.close(p["ticket"], reason="analyst exit")
            elif kind == "CLOSE_HALF":
                spec = self.engine.broker.spec(p["symbol"])
                half = round(p["volume"] / 2 / spec.volume_step) * spec.volume_step
                if half >= spec.volume_min and p["volume"] - half >= spec.volume_min:
                    self.engine.broker.close(p["ticket"], half, reason="analyst half")

    # ---- outcome tracking ----------------------------------------------------------
    @staticmethod
    def _r(s: dict, exit_px: float) -> float:
        risk = abs(s["entry"] - s["sl"])  # sl column always keeps the ORIGINAL stop
        if not risk:
            return 0.0
        move = (exit_px - s["entry"]) if s["side"] == "BUY" else (s["entry"] - exit_px)
        return round(move / risk, 2)

    def _close_virtual(self, s: dict, px: float, outcome: str) -> None:
        r = self._r(s, px)
        storage.update_signal(s["id"], status="closed", outcome=outcome, r_multiple=r,
                              closed_at=datetime.now(timezone.utc))

    def track_once(self) -> None:
        now = time.time()
        for s in storage.list_signals(status=("open", "pending"), limit=200):
            if s["kind"] != "OPEN":
                continue
            try:
                df = self.engine.broker.candles(s["symbol"], "M1", 300)
            except Exception:
                continue
            since = s["received_at"].timestamp() if hasattr(s["received_at"], "timestamp") else now
            bars = df[df["time"] >= since - 60]
            if s["status"] == "pending":
                hit = bars[(bars["low"] <= s["entry_hi"]) & (bars["high"] >= s["entry_lo"])]
                if len(hit):
                    entry = (s["entry_lo"] + s["entry_hi"]) / 2
                    storage.update_signal(s["id"], status="open", entry=entry)
                    s.update(status="open", entry=entry)
                    self._maybe_execute(s, False)
                elif now - since > 86400:
                    storage.update_signal(s["id"], status="expired", outcome="not triggered")
                continue
            buy = s["side"] == "BUY"
            eff_sl = s["entry"] if "+be" in (s["sl_source"] or "") else s["sl"]
            for _, b in bars.iterrows():
                hit_sl = b["low"] <= eff_sl if buy else b["high"] >= eff_sl
                hit_tp = b["high"] >= s["tp"] if buy else b["low"] <= s["tp"]
                if hit_sl:  # pessimistic: SL first when both in one bar
                    self._close_virtual(s, eff_sl, "SL" if eff_sl == s["sl"] else "BE")
                    break
                if hit_tp:
                    self._close_virtual(s, s["tp"], "TP")
                    break
            else:
                if now - since > 86400:
                    self._close_virtual(s, float(bars["close"].iloc[-1]) if len(bars) else s["entry"], "timeout 24h")

    async def tracker_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.track_once)
            except Exception as e:
                self.engine.log("error", f"تلجرام: خطأ في تتبع النتائج: {e}")
            await asyncio.sleep(60)

    def snapshot(self) -> dict:
        return {"status": self.status, "channels": list(self.channels.values()), "mode": settings.tg_mode,
                "me": getattr(self.me, "first_name", None), "awaiting_code": bool(self._login),
                "stats": storage.channel_stats(), "signals": storage.list_signals(limit=40), "recent": self.recent[:20]}
