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
import re
import json
import base64
import hashlib
import time
from datetime import datetime, timezone

from . import storage
from .config import settings
from .indicators import atr as atr_fn
from .risk import position_size
from .signal_parser import expand_short, parse, parse_issa
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
        self.groups: dict[str, dict] = {}

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
        if not await asyncio.to_thread(storage.kv_get, "tg_mode_v2"):  # one-time switch to follow mode (user request)
            await asyncio.to_thread(storage.kv_set, "tg_mode", "follow")
            await asyncio.to_thread(storage.kv_set, "tg_mode_v2", "1")
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

    async def _dump_history(self, per_channel: int = 25) -> None:
        """Log recent channel posts (read-only) so the analyst's style can be reviewed; parses each one."""
        await asyncio.sleep(5)
        for cid, name in self.channels.items():
            try:
                msgs = [m async for m in self.client.iter_messages(cid, limit=per_channel)]
                for m in reversed(msgs):
                    txt = (m.message or "").replace("\n", " ⏎ ")[:350]
                    if not txt.strip():
                        continue
                    kind = parse(m.message or "").get("kind")
                    print("TGHIST", json.dumps({"ch": name[:20], "t": m.date.strftime("%m-%d %H:%M"), "kind": kind, "txt": txt},
                                               ensure_ascii=False), flush=True)
            except Exception as e:
                print("TGHIST_ERROR", name, e, flush=True)

    async def _attach(self) -> None:
        from telethon import events
        self.me = await self.client.get_me()
        from .signal_parser import normalize
        wanted = [normalize(w.strip()) for w in (settings.tg_channels + "," + settings.tg_channels_extra).split(",") if w.strip()]
        self.channels = {}
        async for d in self.client.iter_dialogs():
            nm = normalize(d.name or "")
            if re.search(r"غزه|gaza|abn|صفقه|ذهب|gold|vip|سكالب|scalp", nm, re.I):
                print("TGDIALOG", json.dumps({"name": d.name, "channel": d.is_channel, "group": d.is_group,
                                              "last": d.date.strftime("%m-%d %H:%M") if d.date else None}, ensure_ascii=False), flush=True)
            if (d.is_channel or d.is_group) and any(w in nm for w in wanted):
                self.channels[d.id] = d.name
        if not self.channels:
            self.status = "مربوط — لم أجد القنوات المطلوبة في حسابك"
            return

        asyncio.create_task(self._dump_history())

        self._seen_ids: dict = {}

        async def _process(cid, mid, text):
            key = (cid, mid)
            if key in self._seen_ids:
                return
            self._seen_ids[key] = time.time()
            try:
                await self.handle(self.channels.get(cid, str(cid)), mid, text or "")
            except Exception as e:
                self.engine.log("error", f"تلجرام: خطأ في معالجة رسالة: {e}")

        @self.client.on(events.NewMessage(chats=list(self.channels)))
        async def _on_msg(ev):
            await _process(ev.chat_id, ev.message.id, ev.message.message)

        async def _poll():
            """Backup to push updates (Telegram sometimes stops delivering channel updates): check every 3s."""
            last = {}
            now = time.time()
            cutoff = now - 30 * 60  # catch up on the last 30 min after a restart (DB dedupes)
            for cid in self.channels:
                try:
                    ms = await self.client.get_messages(cid, limit=30)
                    # still-valid pending orders from the last 4h are picked up too
                    ordered = sorted(ms, key=lambda x: x.id)
                    cancel_ids = [m.id for m in ordered if parse(m.message or "").get("kind") == "CANCEL"]
                    for m in ordered:
                        ts = m.date.timestamp()
                        if any(c > m.id for c in cancel_ids):
                            continue  # the analyst cancelled after this order
                        if now - 4 * 3600 < ts < cutoff:
                            pp = parse(m.message or "")
                            if pp.get("kind") == "OPEN" and pp.get("pending"):
                                await _process(cid, m.id, m.message)
                    old = [m for m in ms if m.date.timestamp() < cutoff]
                    last[cid] = max((m.id for m in old), default=(min((m.id for m in ms), default=1) - 1))
                except Exception as e:
                    print("TGPOLL_ERROR", e, flush=True)
                    last[cid] = 0
            while True:
                for cid in list(self.channels):
                    try:
                        msgs = await self.client.get_messages(cid, limit=10, min_id=last.get(cid, 0))
                        for m in sorted(msgs, key=lambda x: x.id):
                            last[cid] = max(last.get(cid, 0), m.id)
                            await _process(cid, m.id, m.message)
                    except Exception as e:
                        print("TGPOLL_ERROR", e, flush=True)
                if len(self._seen_ids) > 5000:
                    cut = time.time() - 86400
                    self._seen_ids = {k: v for k, v in self._seen_ids.items() if v > cut}
                await asyncio.sleep(3)

        asyncio.create_task(_poll())

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
        from .signal_parser import normalize
        issa = "عيسي" in normalize(channel) or "عدواني" in normalize(channel)
        p = parse_issa(text) if issa else parse(text)
        print("TGMSG", json.dumps({"ch": channel[:20], "kind": p["kind"], "txt": text[:300]}, ensure_ascii=False), flush=True)
        if p.get("short_prices") and p["kind"] in ("OPEN", "SL"):
            t0 = await asyncio.to_thread(self.engine.broker.tick, p.get("symbol", "XAUUSD"))
            px = (t0["bid"] + t0["ask"]) / 2
            if p.get("zones"):
                p["zones"] = [(expand_short(a, px), expand_short(b, px)) for a, b in p["zones"]]
                p["entry_lo"], p["entry_hi"] = min(a for a, _ in p["zones"]), max(b for _, b in p["zones"])
            p["sl"] = expand_short(p.get("sl"), px)
        rec = {"channel": channel, "msg_id": msg_id, "text": text[:500], "kind": p["kind"]}
        self.recent = ([{"t": time.time(), "channel": channel, "text": text[:200], "parsed": p}] + self.recent)[:50]
        if p["kind"] == "INFO":
            return p
        if p["kind"] in ("OPEN", "LEVELS", "ADD") and await asyncio.to_thread(self._is_repost, channel, msg_id, text):
            self.engine.log("info", f"تلجرام [{channel}]: رسالة مكررة/إعادة نشر لصفقة قديمة — تجاهل")
            return {"kind": "INFO", "repost": True}
        if not hasattr(self, "intent"):
            self.intent, self.seen = {}, []
        if p["kind"] == "LIGHT":
            return p  # every Telegram entry is already the minimum fixed lot
        if p["kind"] in ("BE", "CLOSE_ALL", "CLOSE_HALF"):
            await asyncio.to_thread(self._manage, channel, p["kind"])
            await asyncio.to_thread(storage.insert_signal, {**rec, "status": "done"})
            self.engine.log("info", f"تلجرام [{channel}]: أمر إدارة {p['kind']}")
            return p
        if p["kind"] == "SL":
            ctx = (getattr(self, "intent", {}) or {}).get(channel)
            if ctx:
                await asyncio.to_thread(self._retarget, channel, ctx, p["sl"], None)
                for s in await asyncio.to_thread(storage.list_signals, ("pending",), channel, 10):
                    ok = (p["sl"] < s["entry_lo"]) if s["side"] == "BUY" else (p["sl"] > s["entry_hi"])
                    if ok:
                        await asyncio.to_thread(storage.update_signal, s["id"], sl=p["sl"], sl_source="analyst")
                self.engine.log("info", f"تلجرام [{channel}]: الوقف صار {p['sl']}")
            return p
        if p["kind"] == "CANCEL":
            n = 0
            for sg in await asyncio.to_thread(storage.list_signals, ("pending",), channel, 50):
                await asyncio.to_thread(storage.update_signal, sg["id"], status="expired", outcome="cancelled by analyst")
                n += 1
            self.engine.log("info", f"تلجرام [{channel}]: المحلل ألغى الأمر — إلغاء {n} أمر معلق")
            return p
        if p["kind"] == "CLOSE_ZONE":
            n = await asyncio.to_thread(self._close_zones, channel, p["zones"])
            await asyncio.to_thread(storage.insert_signal, {**rec, "status": "done"})
            self.engine.log("info", f"تلجرام [{channel}]: إغلاق منطقة {'/'.join(p['zones'])} — {n} صفقة")
            return p
        if p["kind"] == "ADD":
            last = await asyncio.to_thread(storage.list_signals, ("open",), channel, 1)
            if not last:
                return p
            l0 = last[0]
            p = {"kind": "OPEN", "symbol": l0["symbol"], "side": l0["side"], "sl": l0["sl"], "tp": l0["tp"], "zones": []}
            self.engine.log("info", f"تلجرام [{channel}]: تعزيز {l0['side']} {l0['symbol']}")
        if p["kind"] == "LEVELS":
            ctx = self.intent.get(channel)
            if not ctx or time.time() - ctx["t"] > settings.tg_context_min * 60:
                return p
            p = {**p, "kind": "OPEN", "side": ctx["side"], "symbol": ctx["symbol"], "attach": True}
            await asyncio.to_thread(self._retarget, channel, ctx, p.get("sl"), p.get("tp"))
        sym, side = p["symbol"], p["side"]
        self.intent[channel] = {"side": side, "symbol": sym, "t": time.time()}
        # the analyst cross-posts the same call in several channels: take it once
        key = (sym, side, tuple(sorted((round(a), round(b)) for a, b in p.get("zones") or [])), p.get("sl") and round(p["sl"]))
        now = time.time()
        self.seen = [x for x in self.seen if now - x[1] < 900]
        if any(x[0] == key for x in self.seen):
            return p
        self.seen.append((key, now))
        tick = await asyncio.to_thread(self.engine.broker.tick, sym)
        price = tick["ask"] if side == "BUY" else tick["bid"]
        zones = p.get("zones") or []
        tol = price * 0.0005
        entries = []  # (k, lo, hi, now?)
        if not zones:
            entries.append((0, price, price, True))
        for k, (lo, hi) in enumerate(zones):
            inside = lo - tol <= price <= hi + tol
            if p.get("attach") and inside:
                continue  # the side-only order already filled at market here
            entries.append((k, lo, hi, inside and not p.get("pending")))
        targets = [x for x in (p.get("targets") or []) if x]
        for k, lo, hi, now_ in entries:
            ref = price if now_ else (lo + hi) / 2
            sl, sl_src = p.get("sl"), "analyst"
            if not sl or (side == "BUY" and sl >= ref) or (side == "SELL" and sl <= ref):
                sl, sl_src = await asyncio.to_thread(self._auto_sl, sym, side, ref), "auto"
            risk = abs(ref - sl)
            sgn = 1 if side == "BUY" else -1
            good = [t for t in targets if (t - ref) * sgn > 0]
            final = p.get("tp") if p.get("tp") and (p["tp"] - ref) * sgn > 0 else (good[-1] if good else ref + sgn * 2.5 * risk)
            quick = good[0] if len(good) > 1 else ref + sgn * 1.0 * risk
            if (quick - ref) * sgn >= (final - ref) * sgn:
                quick = ref + sgn * 0.6 * abs(final - ref)
            gid = f"{channel}:{msg_id}:{k}"
            grp = self.groups.setdefault(gid, {"side": side, "sym": sym, "risk": risk, "tp": final, "q": [], "r": [],
                                                "be": False, "pyr": False, "channel": channel})
            legs = [("Q", quick), ("R", final)][: max(1, settings.tg_legs)] if settings.tg_legs > 1 else [("R", final)]
            to_run, stored_any = [], False
            zone_txt = f"{lo:g}" if lo == hi else f"{lo:g}–{hi:g}"
            for j, (leg, tp) in enumerate(legs):
                row = {**rec, "msg_id": msg_id * 100 + k * 10 + j, "symbol": sym, "side": side, "entry": price if now_ else None,
                       "entry_lo": lo, "entry_hi": hi, "sl": sl, "sl_source": f"{sl_src}|{leg}|{gid}", "tp": tp,
                       "status": "open" if now_ else "pending"}
                stored = await asyncio.to_thread(storage.insert_signal, row)
                if not stored:
                    continue
                grp["q" if leg == "Q" else "r"].append(stored["id"])
                stored_any = True
                if now_:
                    to_run.append(stored)
            if to_run:  # fire all legs at once: each Exness order waits for its own confirmation
                await asyncio.gather(*[asyncio.to_thread(self._maybe_execute, r_, False) for r_ in to_run])
            if not stored_any:
                continue  # already handled before (restart catch-up): stay quiet
            self.engine.log("info", f"تلجرام [{channel}]: {side} {sym} منطقة {zone_txt} — "
                                    f"{'دخول الحين' if now_ else 'معلق لين يوصلها'} · {len(legs)} صفقات (سريعة {round(quick, 2)} + ممتدة {round(final, 2)}) "
                                    f"· وقف {round(sl, 3)} ({'المحلل' if sl_src == 'analyst' else 'تلقائي'})")
        return p

    # ---- multi-leg management (fast loop) -------------------------------------------
    def _tg_risk_open(self) -> float:
        tot = 0.0
        for pos in self.engine.broker.positions():
            if not self.engine._is_tg(pos) or not pos.get("sl"):
                continue
            sgn = 1 if pos["direction"] == "BUY" else -1
            dist = (pos["price_open"] - pos["sl"]) * sgn
            if dist > 0:
                spec = self.engine.broker.spec(pos["symbol"])
                tot += dist / spec.tick_size * spec.tick_value * pos["volume"]
        return tot

    def manage_groups(self) -> None:
        if not settings.tg_execute:
            return
        if not self.groups:
            return
        pos = {str(p_["ticket"]): p_ for p_ in self.engine.broker.positions()}
        rows = {s_["id"]: s_ for s_ in storage.list_signals(limit=300)}
        for gid, g in list(self.groups.items()):
            q = [rows.get(i) for i in g["q"] if rows.get(i)]
            r = [rows.get(i) for i in g["r"] if rows.get(i)]
            live_r = [pos[str(x["ticket"])] for x in r if x.get("executed") and str(x.get("ticket")) in pos]
            if not live_r and all(x.get("status") in ("closed", "expired") for x in q + r):
                self.groups.pop(gid, None)
                continue
            sgn = 1 if g["side"] == "BUY" else -1
            # quick leg done in profit -> runner to break-even
            q_done = [x for x in q if x.get("executed") and str(x.get("ticket")) not in pos]
            if not g["be"] and q_done and live_r:
                px = live_r[0]["price_current"]
                if (px - live_r[0]["price_open"]) * sgn > 0:
                    for lp in live_r:
                        self.engine.broker.modify(lp["ticket"], lp["price_open"], None)
                        plan = self.engine.plans.get(lp["ticket"])
                        if plan:
                            plan["sl"] = lp["price_open"]
                    g["be"] = True
                    self.engine.log("info", f"تلجرام: الصفقة السريعة ضربت الهدف — الممتدة صارت على الدخول ({g['sym']})")
            # pyramid once the runner is +1R: one more leg, stop at the runner's entry
            if settings.tg_pyramid and not g["pyr"] and live_r:
                lp = live_r[0]
                px = lp["price_current"]
                if (px - lp["price_open"]) * sgn >= g["risk"]:
                    g["pyr"] = True
                    spec, acc = self.engine.broker.spec(g["sym"]), self.engine.broker.account()
                    lots = max(spec.volume_min, round(settings.tg_lots / spec.volume_step) * spec.volume_step)
                    new_risk = abs(px - lp["price_open"]) / spec.tick_size * spec.tick_value * lots
                    if acc.equity and (self._tg_risk_open() + new_risk) / acc.equity * 100 <= settings.tg_total_risk_pct:
                        card = {"id": f"tgpy{int(time.time())}", "symbol": g["sym"], "direction": g["side"], "grade": "TELEGRAM",
                                "lots": round(lots, 8), "sl": lp["price_open"], "tp1": px + sgn * g["risk"],
                                "tp2": px + sgn * 2 * g["risk"], "tp3": g["tp"], "atr_setup": g["risk"] / 1.5,
                                "risk": {"risk_money": round(new_risk, 2)}}
                        res = self.engine.execute(card, f"TG {g['channel'][:12]}")
                        self.engine.log("info", f"تلجرام: تعزيز بالربح {g['side']} {g['sym']} — {'تم' if res.get('ok') else res.get('message')}")

    async def groups_loop(self) -> None:
        if not hasattr(self, "groups"):
            self.groups = {}
        while True:
            try:
                await asyncio.to_thread(self.manage_groups)
            except Exception as e:
                self.engine.log("error", f"تلجرام: إدارة الصفقات: {e}")
            await asyncio.sleep(10)

    def _is_repost(self, channel: str, msg_id: int, text: str) -> bool:
        """The analyst re-posts old calls as recaps ('Before the deal') and cross-posts between channels.
        Identical entry text seen in the last 6h (any channel, other message) is not a new trade.
        Persisted in kv so it survives restarts."""
        from .signal_parser import normalize
        key = re.sub(r"[^\w\d.]+", "", normalize(text))[:200]
        if not key:
            return False
        try:
            seen = json.loads(storage.kv_get("tg_textseen") or "{}")
        except Exception:
            seen = {}
        now = time.time()
        seen = {k: v for k, v in seen.items() if now - v["t"] < 6 * 3600}
        me = f"{channel}:{msg_id}"
        prev = seen.get(key)
        dup = bool(prev and prev["m"] != me)
        if not prev:
            seen[key] = {"t": now, "m": me}
        try:
            storage.kv_set("tg_textseen", json.dumps(seen, ensure_ascii=False))
        except Exception:
            pass
        return dup

    def _retarget(self, channel: str, ctx: dict, sl, tp) -> None:
        """Levels arrived after a side-only market order: move that trade's SL/TP to the analyst's."""
        tickets = {s["ticket"] for s in storage.list_signals(status=("open",), channel=channel, limit=20)
                   if s["executed"] and s["ticket"] and s["side"] == ctx["side"]}
        for pos in self.engine.broker.positions():
            if str(pos["ticket"]) not in tickets:
                continue
            buy = pos["direction"] == "BUY"
            px = pos["price_current"]
            ok_sl = sl and ((sl < px) if buy else (sl > px))
            ok_tp = tp and ((tp > px) if buy else (tp < px))
            if ok_sl or ok_tp:
                self.engine.broker.modify(pos["ticket"], sl if ok_sl else None, tp if ok_tp else None)
                plan = self.engine.plans.get(pos["ticket"])
                if plan and ok_sl:
                    plan["sl"] = sl

    def _close_zones(self, channel: str, zones: list[str]) -> int:
        def match(s):
            vals = [v for v in (s.get("entry_lo"), s.get("entry_hi"), s.get("entry")) if v]
            return any(str(int(round(v))).endswith(z) for v in vals for z in zones)
        hit = [s for s in storage.list_signals(status=("open", "pending"), channel=channel, limit=100) if match(s)]
        tickets = {str(s["ticket"]) for s in hit if s["executed"] and s["ticket"]}
        n = 0
        for pos in self.engine.broker.positions():
            if str(pos["ticket"]) in tickets:
                self.engine.broker.close(pos["ticket"], reason="analyst zone exit")
                n += 1
        for s in hit:
            if s["status"] == "pending":
                storage.update_signal(s["id"], status="expired", outcome="zone cancelled")
            else:
                t = self.engine.broker.tick(s["symbol"])
                self._close_virtual(s, t["bid"] if s["side"] == "BUY" else t["ask"], "closed by analyst")
        return n

    def _maybe_execute(self, sig: dict, light: bool) -> None:
        if not settings.tg_execute:
            storage.update_signal(sig["id"], note="تنفيذ تلجرام موقوف مؤقتاً — تتبع فقط")
            return
        ok, why = self._should_execute(sig["channel"], sig)
        if not ok:
            storage.update_signal(sig["id"], note=why)
            return
        spec, acc = self.engine.broker.spec(sig["symbol"]), self.engine.broker.account()
        t = self.engine.broker.tick(sig["symbol"])
        entry = t["ask"] if sig["side"] == "BUY" else t["bid"]
        lots = max(spec.volume_min, round(settings.tg_lots / spec.volume_step) * spec.volume_step)
        loss = abs(entry - sig["sl"]) / spec.tick_size * spec.tick_value * lots
        if acc.equity and (self._tg_risk_open() + loss) / acc.equity * 100 > settings.tg_total_risk_pct:
            storage.update_signal(sig["id"], note="سقف مخاطرة تلجرام الإجمالي")
            self.engine.log("warn", f"تلجرام: تخطي صفقة إضافية {sig['symbol']} — وصلنا سقف المخاطرة الإجمالي {settings.tg_total_risk_pct}%")
            return
        if acc.equity and loss / acc.equity * 100 > settings.tg_minlot_max_risk_pct:
            storage.update_signal(sig["id"], note=f"الوقف بعيد: {lots} لوت يخاطر {loss / acc.equity * 100:.0f}%")
            self.engine.log("warn", f"تلجرام: رفض {sig['symbol']} — {lots} لوت يخاطر {loss:.2f}$")
            return
        r = abs(entry - sig["sl"])
        sgn = 1 if sig["side"] == "BUY" else -1
        card = {"id": f"tg{sig['id']}x{int(time.time())}", "symbol": sig["symbol"], "direction": sig["side"],
                "grade": "TELEGRAM", "lots": round(lots, 8), "sl": sig["sl"], "tp1": entry + sgn * settings.tp1_r * r,
                "tp2": entry + sgn * settings.tp2_r * r, "tp3": sig["tp"], "atr_setup": r / 1.5,
                "risk": {"risk_money": round(loss, 2)}}
        res = self.engine.execute(card, f"TG {sig['channel'][:12]}")
        storage.update_signal(sig["id"], executed=bool(res.get("ok")), ticket=str(res.get("ticket") or ""),
                              note=why if res.get("ok") else f"رفض: {res.get('message')}")

    def _manage(self, channel: str, kind: str) -> None:
        if not settings.tg_execute:
            return
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
