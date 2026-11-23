"""Decision engine: rejection gates -> evidence score -> classification -> decision card.

Weights are initial, configurable values — NOT proven probabilities. The
historical success rate shown to the user must come from the research lab.
"""
from __future__ import annotations

import time
import uuid

import pandas as pd

from .config import settings
from .indicators import enrich
from .risk import AccountState, SymbolSpec, position_size
from .smc import analyze

WEIGHTS = {"trend": 20, "structure": 20, "liquidity": 15, "momentum": 15, "entry": 15, "risk": 15}
GRADES = [(80, "STRONG", "فرصة قوية"), (65, "MODERATE", "فرصة متوسطة"), (50, "WEAK", "فرصة ضعيفة")]
NO_TRADE = "NO TRADE — WAIT FOR CONFIRMATION"


def _dir_of(trend: str) -> str | None:
    return {"up": "BUY", "down": "SELL"}.get(trend)


def multi_timeframe(frames: dict[str, pd.DataFrame]) -> dict:
    out = {}
    for tf, df in frames.items():
        if len(df) < 60:
            continue
        snap = analyze(df)
        e = enrich(df).iloc[-1]
        snap.update({"rsi": float(e["rsi"]), "adx": float(e["adx"]), "macd_hist": float(e["macd_hist"]),
                     "ema9": float(e["ema9"]), "ema20": float(e["ema20"]), "ema50": float(e["ema50"]),
                     "ema200": float(e["ema200"])})
        out[tf] = snap
    return out


def market_condition(m15: dict) -> str:
    if m15["adx"] >= 25:
        return "trending"
    if m15["adx"] < 18:
        return "ranging"
    return "consolidating"


def evaluate(symbol: str, frames: dict[str, pd.DataFrame], spec: SymbolSpec, acc: AccountState,
             spread: float, news_blocked: str | None = None) -> dict:
    mtf = multi_timeframe(frames)
    card: dict = {"id": uuid.uuid4().hex[:12], "symbol": symbol, "created": time.time(),
                  "mtf": {tf: {k: v[k] for k in ("trend", "rsi", "adx", "last_event", "range")} for tf, v in mtf.items()}}
    D, C, S, E = settings.tf_dominant, settings.tf_confirm, settings.tf_setup, settings.tf_entry
    missing = [tf for tf in (D, C, S, E) if tf not in mtf]
    if missing:
        return {**card, "grade": "NO_TRADE", "headline": NO_TRADE, "gates_failed": [f"بيانات ناقصة: {', '.join(missing)}"]}

    # role names kept from the first version: h4=dominant, h1=confirm, m15=setup, m5=entry
    h4, h1, m15, m5 = mtf[D], mtf[C], mtf[S], mtf[E]
    cond = market_condition(m15)
    card["market_condition"] = cond
    card["dominant_trend"], card["short_trend"] = h4["trend"], m15["trend"]

    # candidate direction: dominant unless setup TF has a fresh CHoCH confirmed by the confirm TF
    direction = _dir_of(h4["trend"])
    ev = m15["last_event"]
    if ev and ev["kind"] == "CHOCH" and _dir_of(ev["direction"]) == _dir_of(h1["trend"]) and len(frames[S]) - ev["idx"] <= 12:
        direction = _dir_of(ev["direction"])
    if direction is None:
        direction = _dir_of(h1["trend"])
    card["direction"] = direction

    gates: list[str] = []
    if direction is None:
        gates.append(f"لا يوجد اتجاه واضح على {D} ولا {C}")
    if news_blocked:
        gates.append(f"نافذة خبر عالي التأثير: {news_blocked}")

    evidence: list[dict] = []
    scores = {k: 0.0 for k in WEIGHTS}
    up = direction == "BUY"
    if direction:
        # 1 trend alignment
        aligned = [tf for tf, s in ((D, h4), (C, h1), (S, m15)) if _dir_of(s["trend"]) == direction]
        scores["trend"] = WEIGHTS["trend"] * (len(aligned) / 3)
        evidence.append({"factor": "توافق الاتجاه", "detail": f"متوافق على: {', '.join(aligned) or 'لا شيء'}"})
        if _dir_of(h4["trend"]) not in (direction, None):
            gates.append(f"الصفقة عكس اتجاه {D} دون تأكيد كافٍ")
        # 2 structure
        if ev and _dir_of(ev["direction"]) == direction and len(frames[S]) - ev["idx"] <= 20:
            scores["structure"] = WEIGHTS["structure"] * (1.0 if ev["kind"] == "BOS" else 0.8)
            evidence.append({"factor": "هيكل السوق", "detail": f"{ev['kind']} على {S} عند {ev['level']:.5g}"})
        elif _dir_of(m15["trend"]) == direction:
            scores["structure"] = WEIGHTS["structure"] * 0.3
        # 3 liquidity
        sw = m5["sweep"] or m15["sweep"]
        if sw and (sw["direction"] == "bull") == up:
            scores["liquidity"] += 10
            evidence.append({"factor": "سحب سيولة", "detail": f"سويب عند {sw['level']:.5g}"})
        rng = h1["range"]
        if rng and ((rng["position"] == "discount") == up):
            scores["liquidity"] += 5
            evidence.append({"factor": "Premium/Discount", "detail": f"السعر في منطقة {rng['position']} على {C}"})
        # 4 momentum
        r = m15["rsi"]
        if (up and 50 <= r <= 70) or (not up and 30 <= r <= 50):
            scores["momentum"] += 8
        if (m15["macd_hist"] > 0) == up:
            scores["momentum"] += 7
        evidence.append({"factor": "الزخم", "detail": f"RSI {S} = {r:.1f}"})
        # 5 entry confirmation
        price = m5["price"]
        zones = [z for z in m5["fvg"] + m5["order_blocks"] + m15["fvg"] + m15["order_blocks"]
                 if (z["direction"] == "bull") == up and z["bottom"] <= price <= z["top"]]
        if zones:
            scores["entry"] = WEIGHTS["entry"]
            evidence.append({"factor": "تأكيد الدخول", "detail": f"السعر داخل {zones[0]['kind']}"})
        elif (m5["ema9"] > m5["ema20"]) == up:
            scores["entry"] = 7
        # 6 risk conditions
        if cond != "ranging":
            scores["risk"] += 10
        if not news_blocked:
            scores["risk"] += 5

    score = round(sum(min(scores[k], WEIGHTS[k]) for k in WEIGHTS), 1)
    card["score"], card["score_breakdown"], card["evidence"] = score, scores, evidence

    # levels from structure
    if direction and not gates:
        price, a = m5["price"], m15["atr"]
        sws = [s for s in m15["swings"] if s["kind"] == ("L" if up else "H")]
        if not sws:
            gates.append("لا توجد قمة/قاع مؤكد لوضع الوقف")
        else:
            base = sws[-1]["price"]
            sl = base - 0.2 * a - spread if up else base + 0.2 * a + spread
            min_d = settings.sl_min_atr * a + spread
            if abs(price - sl) < min_d and ((sl < price) if up else (sl > price)):
                sl = price - min_d if up else price + min_d
            dist = abs(price - sl)
            if not (settings.sl_min_atr * a <= dist <= settings.sl_max_atr * a + spread) or (up and sl >= price) or (not up and sl <= price):
                gates.append(f"مسافة الوقف خارج النطاق ({settings.sl_min_atr}–{settings.sl_max_atr} ATR)")
            else:
                sgn = 1 if up else -1
                tp1, tp2 = price + sgn * settings.tp1_r * dist, price + sgn * settings.tp2_r * dist
                pools = [p["price"] for p in h1["liquidity"] if (p["side"] == "buy") == up and (p["price"] - tp2) * sgn > 0]
                tp3 = min(pools, key=lambda x: abs(x - price)) if pools else price + sgn * settings.tp3_r * dist
                if spread / dist > settings.max_spread_to_sl_ratio:
                    gates.append("السبريد مرتفع مقارنة بمسافة الوقف")
                size = position_size(acc.equity, settings.risk_per_trade_pct, price, sl, spec)
                if size["lots"] <= 0:
                    gates.append(size["reason"])
                d = spec.digits
                card.update({
                    "entry": round(price, d), "sl": round(sl, d), "tp1": round(tp1, d), "tp2": round(tp2, d),
                    "tp3": round(tp3, d), "rr_tp1": settings.tp1_r, "rr_tp2": settings.tp2_r, "atr_setup": a, "rr_tp3": round(abs(tp3 - price) / dist, 2),
                    "lots": size["lots"], "risk": size,
                    "invalidation": f"إغلاق {S} {'تحت' if up else 'فوق'} {round(sl, d)} أو CHoCH معاكس على {S}",
                })

    if score < 50:
        gates.append(f"الدرجة {score} أقل من 50")
    card["gates_failed"] = gates
    card["auto_ok"] = not gates and score >= settings.auto_min_score
    if gates:
        card.update({"grade": "NO_TRADE", "headline": NO_TRADE})
    else:
        for th, code, ar in GRADES:
            if score >= th:
                card.update({"grade": code, "headline": f"{ar} — {'شراء' if up else 'بيع'} {symbol}"})
                break
    card["disclaimer"] = "الأوزان مبدئية وليست احتمالات مثبتة. النسبة التاريخية تُحسب من مختبر الاختبار."
    return card
