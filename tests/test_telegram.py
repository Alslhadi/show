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
    storage._mem_kv.pop("tg_textseen", None)
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
    assert s["side"] == "SELL" and s["sl"] > s["entry"] and s["sl_source"] .startswith("auto") and not s["executed"]
    assert not b.positions()
    n = len(storage.list_signals())
    assert n == settings.tg_legs  # quick + runner legs
    asyncio.run(br.handle("قناة", 1, "بيع ذهب الان"))  # duplicate message id ignored
    assert len(storage.list_signals()) == n


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


def test_parser_real_channel_styles():
    from app.signal_parser import parse
    assert parse("إذا كنت داخل صفقة بيع هل أنت متأكد إنك رح تحقق ربح؟")["kind"] == "INFO"
    assert parse("لا تبيعو مرة اخرى")["kind"] == "INFO"
    assert parse("80pips+ اغلق الصفقة 🔥")["kind"] == "CLOSE_ALL"
    assert parse("يلا اغلق منطقة 86 وابقى في 81")["zones"] == ["86"]
    assert parse("📍 منطقة الدخول: 3528 – 3533\n🛑 وقف الخسارة: 3542\n🎯 الهدف: 3520")["kind"] == "LEVELS"
    p = parse("شراء الذهب الآن 3521 - 3517\n1_الهدف 3525\n5_الهدف 3541\nوقف الخسارة 3509")
    assert p["kind"] == "OPEN" and p["tp"] == 3541 and p["sl"] == 3509 and p["zones"] == [(3517, 3521)]
    assert parse("ابدء بالشراء")["side"] == "BUY"
    assert parse("📊 نتائج تداول يوم 23/11 الجولة الأولى: بيع من 3573 إلى 3568 +50 نقطة")["kind"] == "INFO"


def test_issa_dialect():
    from app.signal_parser import parse_issa, expand_short
    p = parse_issa("فوق ٢٥ بدخل شراء تحت ٢٠ سكره")
    assert p["kind"] == "OPEN" and p["side"] == "BUY" and p["sl"] == 20
    assert parse_issa("ستوب دخول")["kind"] == "BE"
    assert parse_issa("تحت ٢٢ سكره")["kind"] == "SL"
    assert parse_issa("راح انام ماراح ادخل على الذهب")["kind"] == "INFO"
    assert expand_short(25, 3618.4) == 3625 and expand_short(620, 3612) == 3620 and expand_short(3610, 3612) == 3610


def test_repost_of_old_call_is_ignored(monkeypatch):
    br, b = _bridge()
    monkeypatch.setattr(settings, "tg_mode", "follow")
    asyncio.run(br.handle("قناة", 50, "شراء خفيف وسريع"))
    n = len(b.positions())
    asyncio.run(br.handle("قناة2", 77, "شراء خفيف وسريع"))  # recap / cross-post later
    assert len(b.positions()) == n


def test_levels_map_and_quiz_are_not_orders():
    from app.signal_parser import parse
    assert parse("📊 مناطقي الرئيسية علي الذهب\n🔴 مناطق البيع\n1️⃣ 3767\n🟢 مناطق الشراء\n1️⃣ 3558")["kind"] == "INFO"
    assert parse("علقت أمر تاني تتوقعو بيع ولا شراء")["kind"] == "INFO"
