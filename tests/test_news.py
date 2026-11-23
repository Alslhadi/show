import time
from datetime import datetime, timezone, timedelta

from app.news import parse, NewsFeed


def test_parse_and_windows():
    t = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    raw = [{"title": "Non-Farm Employment Change", "country": "USD", "date": t, "impact": "High", "forecast": "180K", "previous": "150K"},
           {"title": "Something", "country": "JPY", "date": t, "impact": "High"},
           {"title": "Bank Holiday", "country": "EUR", "date": t, "impact": "Holiday"}]
    ev = parse(raw)
    assert len(ev) == 1 and "XAUUSD" in ev[0]["symbols"] and "NFP" in ev[0]["note"]
    n = NewsFeed(); n.events = ev
    w = n.windows()
    assert w[0]["auto"] and abs(w[0]["start"] - time.time() - 300) < 5


def test_engine_blocks_on_news():
    from app.engine import Engine
    from app.brokers.paper import PaperBroker
    e = Engine(PaperBroker(["XAUUSD", "EURUSD"], balance=1000.0))
    e.news_windows = [{"name": "GBP CPI", "start": time.time() + 60, "end": time.time() + 60, "symbols": ["GBPUSD"], "auto": True}]
    assert e.news_block("GBPUSD") and not e.news_block("XAUUSD")
