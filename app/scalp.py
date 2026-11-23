"""Micro-scalp ("money factory") strategy: many small trades with a fixed money target.

Entry: trend-aligned pullback on M1 (M15 + M5 EMA20/EMA50 agree, M5 ADX shows a trend,
M1 candle dipped to EMA20 and closed back in trend direction, RSI not stretched).
Exit: fixed money TP (scalp_tp_usd) and fixed money SL (scalp_sl_usd); every trade has a stop.
Size is set so a stop-out costs ~scalp_sl_usd; if the minimum lot would risk far more, skip."""
from __future__ import annotations

import math
import time
import uuid

from .config import settings
from .indicators import enrich


def _trend(df) -> int:
    e = enrich(df)
    a, b = float(e["ema20"].iloc[-1]), float(e["ema50"].iloc[-1])
    return 1 if a > b else -1 if a < b else 0


def evaluate(symbol: str, frames: dict, spec, spread: float, news_blocked: str | None = None, equity: float = 0.0) -> dict:
    card = {"id": uuid.uuid4().hex[:12], "symbol": symbol, "created": time.time(), "strategy": "SCALP",
            "grade": "NO_TRADE", "direction": None, "score": 0, "gates_failed": [], "auto_ok": False,
            "headline": "لا صفقة"}
    g = card["gates_failed"]
    m15, m5, m1 = frames.get(settings.tf_confirm), frames.get(settings.tf_setup), frames.get(settings.tf_entry)
    if any(f is None or len(f) < 60 for f in (m15, m5, m1)):
        g.append("بيانات ناقصة")
        return card
    t15, t5 = _trend(m15), _trend(m5)
    e5, e1 = enrich(m5), enrich(m1)
    adx5 = float(e5["adx"].iloc[-1])
    c = e1.iloc[-1]
    a1 = float(c["atr"])
    up = t15 == t5 == 1
    dn = t15 == t5 == -1
    score = 40 + (20 if (up or dn) else 0) + (20 if adx5 >= 18 else 0)
    if not (up or dn):
        g.append("M15 و M5 ما يتفقون على الاتجاه")
    if adx5 < 18:
        g.append(f"السوق بدون اتجاه (ADX {adx5:.0f})")
    direction = "BUY" if up else "SELL" if dn else None
    if direction:
        ema20, rsi = float(c["ema20"]), float(c["rsi"])
        last3 = e1.iloc[-3:]
        if up:  # a dip to EMA20 within the last 3 candles, now closing back up
            pull = bool((last3["low"] <= last3["ema20"] + 0.3 * a1).any()) and c["close"] > ema20 and c["close"] > c["open"]
            rsi_ok = 40 <= rsi <= 75
        else:
            pull = bool((last3["high"] >= last3["ema20"] - 0.3 * a1).any()) and c["close"] < ema20 and c["close"] < c["open"]
            rsi_ok = 25 <= rsi <= 60
        if pull:
            score += 10
        else:
            g.append("ما في ارتداد على المتوسط في M1")
        if rsi_ok:
            score += 10
        else:
            g.append(f"RSI ممتد ({rsi:.0f})")
    if direction:
        from .votes import vote
        vt = vote(m5)
        agree = vt["buy"] if direction == "BUY" else vt["sell"]
        card["votes"] = vt
        card["votes_line"] = f"{agree}/{vt['total']} مؤشر موافق"
        if agree < settings.votes_min_agree:
            g.append(f"المؤشرات ما تتفق: {agree}/{vt['total']} (المطلوب {settings.votes_min_agree})")
        else:
            score = min(100, score + (agree - settings.votes_min_agree))
    if news_blocked:
        g.append(f"خبر قوي: {news_blocked}")
    card.update(direction=direction, score=score)
    if g or not direction:
        return card

    sl_usd = max(settings.scalp_sl_usd, equity * settings.scalp_risk_pct / 100)
    tp_usd = max(settings.scalp_tp_usd, sl_usd * settings.scalp_tp_mult)
    price = float(c["close"])
    sl_dist = max(1.5 * a1, 6 * spread, spec.stops_level * spec.point * 1.2)
    per_unit = spec.tick_value / spec.tick_size  # $ per 1.0 price move per 1 lot
    lots = sl_usd / (sl_dist * per_unit)
    lots = math.floor(lots / spec.volume_step + 1e-9) * spec.volume_step
    lots = max(lots, spec.volume_min)
    lots = min(lots, spec.volume_max)
    lots = round(lots, 8)
    sl_money = lots * sl_dist * per_unit
    cap = min(sl_usd * 2, equity * settings.max_risk_per_trade_pct / 100) if equity else sl_usd * 2
    if sl_money > cap:
        g.append(f"أقل لوت يخاطر {sl_money:.2f}$ — أكبر من المسموح")
        return card
    tp_dist = tp_usd / (lots * per_unit) + spread
    sgn = 1 if direction == "BUY" else -1
    d = spec.digits
    sl, tp = round(price - sgn * sl_dist, d), round(price + sgn * tp_dist, d)
    card.update({"grade": "SCALP", "auto_ok": True, "entry": round(price, d), "sl": sl, "tp1": tp, "tp2": tp, "tp3": tp,
                 "lots": lots, "atr_setup": a1, "risk": {"risk_money": round(sl_money, 2)},
                 "headline": f"سكالب {'شراء' if sgn > 0 else 'بيع'} {symbol} — هدف {tp_usd:.2f}$ / وقف {sl_money:.2f}$"})
    return card
