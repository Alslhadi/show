"""Economic calendar (free ForexFactory weekly feed) -> automatic news blackout windows.

High-impact events block new entries on affected symbols for news_block_minutes before/after.
The "expected effect" text is a rule-of-thumb explanation, not a prediction."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

import httpx

FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
AFFECTS = {"USD": ["XAUUSD", "BTCUSD", "EURUSD", "GBPUSD"], "EUR": ["EURUSD"], "GBP": ["GBPUSD"]}
# indicators where a LOWER number is good for the currency
INVERSE = ("unemployment", "jobless", "claims", "trade balance")

KEY_EVENTS = {
    "non-farm": "الوظائف الأمريكية (NFP) — من أقوى الأخبار، حركة عنيفة على الذهب والدولار",
    "cpi": "التضخم — أرقام أعلى من المتوقع تقوّي العملة غالباً",
    "fomc": "الفيدرالي — قرار الفائدة/التصريحات، تذبذب قوي",
    "federal funds": "قرار الفائدة الأمريكية — تذبذب قوي",
    "interest rate": "قرار فائدة — تذبذب قوي على العملة",
    "gdp": "الناتج المحلي — قوة الاقتصاد",
    "pmi": "مؤشر مديري المشتريات — نشاط الاقتصاد",
    "retail sales": "مبيعات التجزئة — إنفاق المستهلك",
    "powell": "تصريحات رئيس الفيدرالي — ممكن تقلب مفاجئ",
    "lagarde": "تصريحات رئيسة المركزي الأوروبي",
    "bailey": "تصريحات محافظ بنك إنجلترا",
    "unemployment": "البطالة — أرقام أقل = عملة أقوى",
    "claims": "طلبات إعانة البطالة — أرقام أقل = دولار أقوى",
}


def explain(ev: dict) -> str:
    t = ev["title"].lower()
    base = next((v for k, v in KEY_EVENTS.items() if k in t), "خبر اقتصادي")
    if ev["country"] == "USD":
        base += " | دولار أقوى ← الذهب ينزل غالباً، EUR/GBP تنزل"
    elif ev["country"] in ("EUR", "GBP"):
        base += f" | {ev['country']} أقوى ← {ev['country']}USD يطلع"
    return base


def parse(raw: list[dict]) -> list[dict]:
    out = []
    for e in raw:
        c = e.get("country")
        if c not in AFFECTS or e.get("impact") not in ("High", "Medium"):
            continue
        try:
            ts = datetime.fromisoformat(e["date"]).timestamp()
        except Exception:
            continue
        ev = {"title": e.get("title", ""), "country": c, "impact": e["impact"], "time": ts,
              "forecast": e.get("forecast") or "", "previous": e.get("previous") or "",
              "symbols": AFFECTS[c]}
        ev["note"] = explain(ev)
        out.append(ev)
    return sorted(out, key=lambda x: x["time"])


class NewsFeed:
    def __init__(self):
        self.events: list[dict] = []
        self.updated: float = 0
        self.error: str = ""

    def windows(self) -> list[dict]:
        return [{"name": f"{e['country']} {e['title']}", "start": e["time"], "end": e["time"],
                 "symbols": e["symbols"], "auto": True} for e in self.events if e["impact"] == "High"]

    async def refresh(self) -> None:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(FEED)
            r.raise_for_status()
            self.events = parse(r.json())
            self.updated = time.time()
            self.error = ""

    def _load_cache(self) -> bool:
        try:
            from . import storage
            import json
            raw = storage.kv_get("news_cache")
            c = json.loads(raw) if raw else None
            if c and time.time() - c.get("updated", 0) < 6 * 3600:
                self.events, self.updated = c["events"], c["updated"]
                return True
        except Exception:
            pass
        return False

    def _save_cache(self) -> None:
        try:
            from . import storage
            import json
            storage.kv_set("news_cache", json.dumps({"events": self.events, "updated": self.updated}))
        except Exception:
            pass

    async def loop(self, engine) -> None:
        cached = await asyncio.to_thread(self._load_cache)
        while True:
            try:
                if cached:
                    cached = False
                else:
                    await self.refresh()
                    await asyncio.to_thread(self._save_cache)
                manual = [w for w in engine.news_windows if not w.get("auto")]
                engine.news_windows = manual + self.windows()
                print("NEWS", len(self.events), "events,", len(self.windows()), "high-impact", flush=True)
                await asyncio.sleep(3600)
            except Exception as e:
                self.error = str(e)[:200]
                print("NEWS_ERROR", self.error, flush=True)
                await asyncio.sleep(1200 if "429" in self.error else 600)

    def view(self) -> dict:
        now = time.time()
        upcoming = [e for e in self.events if e["time"] > now - 3 * 3600]
        return {"updated": self.updated, "error": self.error, "events": upcoming[:40]}


news = NewsFeed()
