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
CLOSE_ALL = r"خروج كامل|خروج من الصفقه|اغلاق كامل|سكر الصفق|سكرو|اغلقوا الصفق|close all|exit all"
CLOSE_HALF = r"نصف|نص الصفقه|جزئي|partial|close half"
BREAKEVEN = r"امن دخولك|امنو|امن الصفق|تامين|نقل الستوب للدخول|ستوب علي الدخول|breakeven|break even|\bbe\b"
SELL = r"\bبيع\b|بيعو|\bsell\b"
BUY = r"\bشراء\b|\bشرا\b|\bشري\b|اشتر|\bbuy\b"
PENDING = r"معلق|ليمت|limit|pending|منطقه الدخول|منطقه دخول"
ADD = r"تعزيز|عزز|عززو|اضافه|اضف|ضيف|زود|زيد|دبل|add|re-?entry"
LIGHT = r"خفيف|صغير|بحذر|light|small"
NUM = r"(\d{3,6}(?:\.\d+)?)"


def _num_after(pattern: str, t: str) -> float | None:
    m = re.search("(?:" + pattern + r")[^\d]{0,12}" + NUM, t)
    return float(m.group(1)) if m else None


def parse(text: str) -> dict:
    t = normalize(text)
    out: dict = {"kind": "INFO"}
    if not t.strip():
        return out
    if re.search(CLOSE_ALL, t):
        return {"kind": "CLOSE_ALL"}
    if re.search(BREAKEVEN, t):
        return {"kind": "BE"}
    if re.search(CLOSE_HALF, t) and re.search(r"اغلاق|اغلق|خروج|سكر|close", t):
        return {"kind": "CLOSE_HALF"}

    side = "SELL" if re.search(SELL, t) else "BUY" if re.search(BUY, t) else None
    if not side:
        if re.search(ADD, t):  # "عززوا" without a side -> add to the channel's current trade
            return {"kind": "ADD", "light": bool(re.search(LIGHT, t))}
        return out
    # results/recaps like "+50pips بيع" are not new instructions
    if re.search(r"pips|نقطه ربح|مبروك|ربح", t) and not re.search(r"الان|دخول|ادخل|معلق|سريع", t):
        return out
    symbol = next((s for s, pat in SYMBOLS if re.search(pat, t)), "XAUUSD")  # these channels are gold-focused
    o = {"kind": "OPEN", "symbol": symbol, "side": side, "light": bool(re.search(LIGHT, t))}
    zone = re.search(NUM + r"\s*[-–_/~]\s*" + NUM, t)
    if zone:
        a, b = float(zone.group(1)), float(zone.group(2))
        o["entry_lo"], o["entry_hi"] = min(a, b), max(a, b)
    o["sl"] = _num_after(r"ستوب|وقف|sl|stop", t)
    o["tp"] = _num_after(r"هدف|الهدف|اهداف|تارجت|tp|target", t)
    o["pending"] = bool(re.search(PENDING, t)) and "entry_lo" in o
    return o
