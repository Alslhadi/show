import asyncio
import os

os.environ.pop("DATABASE_URL", None)

from app import storage
from app.brokers.paper import PaperBroker
from app.config import settings
from app.engine import Engine
from app.risk import risk_engine
from app.signal_parser import parse
from app.telegram_bridge import TelegramBridge


def _bridge():
    storage._mem_signals.clear()
    risk_engine.release()
    b = PaperBroker(["XAUUSD"], balance=1000.0)
    return TelegramBridge(Engine(b)), b


def test_parser_cases():
    assert parse("طيب بيع سريع وخفيف بسرعة")["side"] == "SELL"
    p = parse("أمر معلق شراء الذهب منطقة الدخول 3581 - 3586 ستوب 3570 الهدف 3600")
    assert p["pending"] and p["entry_lo"] == 3581 and p["sl"] == 3570 and p["tp"] == 3600
    assert parse("أمن دخولك 😅")["kind"] == "BE"
    assert parse("خروووووووج كامل من الصفقة")["kind"] == "CLOSE_ALL"
    assert parse("50pips+ 🤑")["kind"] == "INFO"
    assert parse("330pips+ وتسألنيش ليش")["kind"] == "INFO"


def test_track_mode_records_with_auto_sl_and_no_trade(monkeypatch):
    br, b = _bridge()
    monkeypatch.setattr(settings, "tg_mode", "track")
    asyncio.run(br.handle("قناة", 1, "بيع ذهب الان"))
    s = storage.list_signals()[0]
    assert s["side"] == "SELL" and s["sl"] > s["entry"] and s["sl_source"] == "auto" and not s["executed"]
    assert not b.positions()
    asyncio.run(br.handle("قناة", 1, "بيع ذهب الان"))  # duplicate message id ignored
    assert len(storage.list_signals()) == 1


def test_follow_mode_executes_with_stop(monkeypatch):
    br, b = _bridge()
    monkeypatch.setattr(settings, "tg_mode", "follow")
    asyncio.run(br.handle("قناة", 2, "شراء ذهب الان"))
    s = storage.list_signals()[0]
    assert s["executed"] and b.positions() and b.positions()[0]["sl"] == s["sl"]
    asyncio.run(br.handle("قناة", 3, "خروج كامل من الصفقة"))
    assert not b.positions()
    assert storage.list_signals(status=("closed",))


def test_tracker_resolves_outcome(monkeypatch):
    br, b = _bridge()
    monkeypatch.setattr(settings, "tg_mode", "track")
    asyncio.run(br.handle("قناة", 4, "شراء ذهب الان"))
    for _ in range(600):
        b.step()
        br.track_once()
        if storage.list_signals(status=("closed",)):
            break
    st = storage.channel_stats()["قناة"]
    assert st["signals"] == 1


def test_parse_add():
    from app.signal_parser import parse
    assert parse("عززوا الصفقة")["kind"] == "ADD"
    assert parse("تعزيز شراء ذهب ستوب 2300")["kind"] == "OPEN"
