"""Hybrid agent decision (fast deterministic path), per the research playbook:
Stage 1 regime-conditioned primary signal -> Stage 2 meta-label probability p -> Stage 3 risk gate
(news calendar, spread/ATR, circuit breaker in the risk engine). Size = min(ATR risk cap, fractional Kelly)."""
from __future__ import annotations

import time
import uuid

from ..config import settings
from ..risk import position_size
from . import features as F

REG_AR = {"TREND_UP": "اتجاه صاعد", "TREND_DOWN": "اتجاه هابط", "RANGE": "عرضي", "CRISIS": "تقلب حاد (أزمة)"}


def evaluate(symbol: str, df, spec, spread: float, news_blocked: str | None, equity: float, model, mode: str = "model") -> dict:
    card = {"id": uuid.uuid4().hex[:12], "symbol": symbol, "created": time.time(), "strategy": "HYBRID",
            "grade": "NO_TRADE", "direction": None, "score": 0, "gates_failed": [], "auto_ok": False, "headline": "لا صفقة"}
    g = card["gates_failed"]
    if df is None or len(df) < 300:
        g.append("بيانات ناقصة")
        return card
    d = F.build(df)
    i = len(d) - 1
    r = d.iloc[i]
    reg = str(r["regime"])
    card["regime"] = reg
    card["regime_ar"] = REG_AR.get(reg, reg)
    sig = int(r["signal"])
    if reg == "CRISIS":
        g.append("النظام السوقي: تقلب حاد — لا دخول")
    if not sig:
        g.append(f"ما في إشارة أولية ({REG_AR.get(reg, reg)})")
        return card
    direction = "BUY" if sig > 0 else "SELL"
    card["direction"] = direction
    R = settings.hybrid_tp1_r
    card["mode"] = mode
    if mode == "model":
        if model is None or not model.ready:
            g.append("نموذج الاحتمال لم يتدرب بعد على بيانات كافية")
            return card
        p = model.predict(F.feature_row(d, i))
        kelly = p - (1 - p) / R
        card.update(meta_p=round(p, 3), kelly=round(kelly, 3), score=int(round(p * 100)))
        if p < settings.meta_p_min:
            g.append(f"احتمال النجاح {p:.0%} أقل من {settings.meta_p_min:.0%}")
        if kelly <= 0:
            g.append("كيلي سالب — الصفقة بدون أفضلية رياضية")
    else:  # walk-forward showed raw signals beat the filter (or nothing has an edge yet -> minimum size)
        p = model.predict(F.feature_row(d, i)) if (model is not None and model.ready) else None
        kelly = 0.04 if mode == "raw" else 0.0  # -> 1% risk for raw; min lot for "min"
        card.update(meta_p=round(p, 3) if p is not None else None, kelly=kelly, score=int(round((p or 0.5) * 100)))
    if news_blocked:
        g.append(f"خبر قوي: {news_blocked}")
    a = float(r["atr"])
    price = float(r["close"])
    sgn = 1 if sig > 0 else -1
    dist = settings.hybrid_sl_atr * a + spread
    if spread / dist > settings.max_spread_to_sl_ratio:
        g.append("السبريد مرتفع مقارنة بالوقف")
    if g:
        return card
    risk_pct = min(settings.hybrid_risk_max_pct, settings.kelly_fraction * kelly * 100) if kelly > 0 else 0.0
    sl = price - sgn * dist
    size = position_size(equity, risk_pct, price, sl, spec) if risk_pct > 0 else {"lots": 0.0, "loss_per_lot": abs(price - sl) / spec.tick_size * spec.tick_value}
    lots = size["lots"]
    if lots <= 0:
        loss_min = size.get("loss_per_lot", 0) * spec.volume_min
        if equity and loss_min / equity * 100 <= settings.hybrid_minlot_max_risk_pct:
            lots, size = spec.volume_min, {**size, "lots": spec.volume_min, "risk_money": round(loss_min, 2),
                                            "risk_pct_actual": round(loss_min / equity * 100, 2)}
        else:
            g.append(f"الرصيد صغير على {symbol}: أقل لوت يخاطر {loss_min / equity * 100:.1f}%")
            return card
    dg = spec.digits
    tp1, tp2, tp3 = price + sgn * R * dist, price + sgn * settings.hybrid_tp2_r * dist, price + sgn * settings.hybrid_tp3_r * dist
    card.update({"grade": "HYBRID", "auto_ok": True, "entry": round(price, dg), "sl": round(sl, dg), "tp1": round(tp1, dg),
                 "tp2": round(tp2, dg), "tp3": round(tp3, dg), "lots": lots, "atr_setup": a, "risk": size,
                 "risk_pct": round(risk_pct, 2),
                 "headline": f"{'شراء' if sgn > 0 else 'بيع'} {symbol} — {('احتمال ' + format(p, '.0%')) if p is not None else 'إشارة خام'} · {REG_AR.get(reg, reg)} · مخاطرة {size.get('risk_pct_actual', risk_pct)}%"})
    return card
