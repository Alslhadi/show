"""Rule-based parser for Arabic/English trading signals from Telegram channels.

Conservative by design: anything not clearly a trade instruction is INFO (logged, never acted on).
"""
from __future__ import annotations

import re

AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789.")
DIACRITICS = re.compile(r"[ً-ْـ]")


def normalize(t: str) -> str:
    t = (t or "").translate(AR_DIGITS)
    t = DIACRITICS.sub("", t)
    t = re.sub("[أإآ]", "ا", t).replace("ة", "ه").replace("ى", "ي")
    t = re.sub(r"(.)\1{2,}", r"\1", t)  # خروووووج -> خروج
    return t.lower()


SYMBOLS = [
    ("XAUUSD", r"ذهب|الذهب|gold|xau"),
    ("BTCUSD", r"بيتكوين|بتكوين|btc|bitcoin"),
    ("EURUSD", r"يورو|eurusd|eur/usd"),
    ("GBPUSD", r"باوند|استرليني|gbpusd|gbp/usd"),
]
CLOSE_ALL = r"خروج كامل|خروج من الصفقه|يفضل الخروج|اغلاق كامل|اغلق الصفقه|اغلقو|اغلقوا|هغلق|سكر الصفق|سكرو|close all|exit all"
CLOSE_ZONE = r"اغلق\s*منطقه\s*(\d{2,5})(?:\s*[|/و,]\s*(\d{2,5}))?"
CLOSE_HALF = r"نصف|نص الصفقه|جزئي|partial|close half"
BREAKEVEN = r"امن دخولك|امن صفقتك|امنو|امن الصفق|تامين|نقل الستوب للدخول|ستوب علي الدخول|breakeven|break even|\bbe\b"
SELL = r"بيع|بيعو|\bsell\b|🔴"
BUY = r"شراء|\bشرا\b|\bشري\b|اشتر|\bbuy\b|🟢"
PENDING = r"معلق|ليمت|limit|pending|انتظر امر"
ADD = r"تعزيز|عزز|عززو|\bدبل\b|\badd\b|re-?entry"
LIGHT = r"خفيف|صغير|بحذر|light|small"
# educational posts, questions, recaps, hype, future tense: never instructions
NOISE = (r"اذا كنت|هل انت|نتائج|اجمالي|الجوله|مبروك|ربحكم|ربحك|شاركني|ابعت|ابعتولي|طمنوني|المقال|نصيحتي|"
         r"للانضمام|اضيفك|كلمني|هدخل|رح ادخل|بعد افتتاح|جاهزين|حضرو|تفعلت|انتظار|لا تبيع|لا تشتر|لا تفوت|"
         r"تسالنيش|ما شاء الله|الحمد لله|عاشت|يسعد|كانت|صعد الي|نزل من")
NUM = r"(\d{3,6}(?:\.\d+)?)"


def _num_after(pattern: str, t: str) -> float | None:
    m = re.search("(?:" + pattern + r")[^\d]{0,12}" + NUM, t)
    return float(m.group(1)) if m else None


def _targets(t: str) -> list[float]:
    return [float(x) for x in re.findall(r"(?:هدف|الهدف|اهداف|تارجت|tp|target)(?:\s*\d\s*[:_.)-])?[^\d\n]{0,12}" + NUM, t)]


def _zones(t: str) -> list[tuple[float, float]]:
    """Entry zones: 'منطقه الدخول [الاولي|الثانيه]: 3548 – 3543' or a single price after 'منطقه الدخول'/'الان'."""
    out = []
    for m in re.finditer(r"(?:منطقه الدخول|منطقه دخول|نقطه الدخول|الدخول|الان|الأن)[^\d\n]{0,20}" + NUM + r"(?:\s*[-–_/~]\s*" + NUM + ")?", t):
        a = float(m.group(1))
        b = float(m.group(2)) if m.group(2) else a
        out.append((min(a, b), max(a, b)))
    return out


def parse(text: str) -> dict:
    t = normalize(text)
    out: dict = {"kind": "INFO"}
    if not t.strip():
        return out
    z = re.search(CLOSE_ZONE, t)
    if z:
        return {"kind": "CLOSE_ZONE", "zones": [x for x in z.groups() if x]}
    if re.search(CLOSE_ALL, t):
        return {"kind": "CLOSE_ALL"}
    if re.search(BREAKEVEN, t) and len(t) < 120:
        return {"kind": "BE"}
    if re.search(CLOSE_HALF, t) and re.search(r"اغلاق|اغلق|خروج|سكر|close", t):
        return {"kind": "CLOSE_HALF"}
    noisy = bool(re.search(NOISE, t)) or "?" in t or "؟" in t or len(t) > 400
    recap = bool(re.search(r"pips|نقطه ربح|نقطه 🔥|\+\d+ نقطه", t))
    zones = _zones(t)
    sl = _num_after(r"وقف الخساره|ستوب|وقف|sl|stop", t)
    tps = _targets(t)
    side = "SELL" if re.search(SELL, t) else "BUY" if re.search(BUY, t) else None
    light = bool(re.search(LIGHT, t))
    if noisy or (recap and not sl):
        return out
    if not side:
        if zones and sl:  # levels posted right after a side-only order ("بيع الأن") -> attach to it
            return {"kind": "LEVELS", "zones": zones, "sl": sl, "tp": tps[-1] if tps else None, "light": light}
        if re.search(ADD, t):
            return {"kind": "ADD", "light": light}
        if light and len(t) < 40:
            return {"kind": "LIGHT"}
        return out
    symbol = next((s for s, pat in SYMBOLS if re.search(pat, t)), "XAUUSD")  # these channels are gold-focused
    o = {"kind": "OPEN", "symbol": symbol, "side": side, "light": light, "zones": zones, "sl": sl,
         "tp": tps[-1] if tps else None, "targets": tps}
    if zones:
        o["entry_lo"], o["entry_hi"] = min(a for a, _ in zones), max(b for _, b in zones)
    o["pending"] = bool(re.search(PENDING, t)) and bool(zones)
    return o
