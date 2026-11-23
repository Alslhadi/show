import os

os.environ.setdefault("DASHBOARD_TOKEN", "t")
os.environ.setdefault("WEBHOOK_SECRET", "s")

import numpy as np
import pandas as pd
import pytest

from app.brokers.paper import PaperBroker
from app.decision import evaluate
from app.risk import AccountState, RiskEngine, SymbolSpec, position_size
from app.smc import find_swings, market_structure, fair_value_gaps
from app.indicators import atr
from app.config import settings

TFS = (settings.tf_dominant, settings.tf_confirm, settings.tf_setup, settings.tf_entry)

GOLD = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 1.0, 0.01, 100, 0.01, 0)


def test_lot_size_gold():
    # 10,000 equity, 1% risk = 100 USD; SL 5.00 away = 500 ticks * 1 USD = 500/lot -> 0.20 lots
    r = position_size(10_000, 1.0, 2000.0, 1995.0, GOLD)
    assert r["lots"] == pytest.approx(0.20)
    assert r["risk_money"] == pytest.approx(100.0)


def test_lot_size_rounds_down_never_up():
    r = position_size(1_000, 0.5, 2000.0, 1990.0, GOLD)  # 5 USD / 1000 per lot = 0.005 -> below min
    assert r["lots"] == 0.0 and r["reason"]


def test_risk_capped_at_absolute_max():
    r = position_size(10_000, 50.0, 2000.0, 1995.0, GOLD)  # asks 50%, capped at the absolute max (5%)
    assert r["risk_money"] <= 500.0 + 1e-6


def test_kill_switch_blocks_and_consecutive_losses():
    re = RiskEngine()
    for _ in range(5):
        re.record_result(-10)
    assert re.state.kill_switch
    errs = re.validate(symbol="XAUUSD", direction="BUY", entry=2000, sl=1995, tp=2010, lots=0.1, spec=GOLD,
                       acc=AccountState(10_000, 10_000, 9_000), open_positions=[], spread=0.2)
    assert any("KILL" in e for e in errs)


def test_daily_loss_triggers_kill():
    re = RiskEngine()
    re.update_equity(AccountState(10_000, 10_000, 10_000))
    re.update_equity(AccountState(10_000, 8_400, 8_300))  # -16% > 15% daily limit
    assert re.state.kill_switch


def test_validate_rejects_bad_sl_tp_order():
    re = RiskEngine()
    errs = re.validate(symbol="XAUUSD", direction="BUY", entry=2000, sl=2005, tp=2010, lots=0.1, spec=GOLD,
                       acc=AccountState(10_000, 10_000, 9_000), open_positions=[], spread=0.2)
    assert any("SL/TP" in e for e in errs)


def _zigzag():
    # clean up-trend: higher highs / higher lows, then a break
    pts = [100, 105, 102, 108, 104, 111, 107, 114]
    closes = np.concatenate([np.linspace(pts[i], pts[i + 1], 8) for i in range(len(pts) - 1)])
    df = pd.DataFrame({"close": closes})
    df["open"] = df["close"].shift(1).fillna(closes[0])
    df["high"] = df[["open", "close"]].max(axis=1) + 0.1
    df["low"] = df[["open", "close"]].min(axis=1) - 0.1
    df["time"] = np.arange(len(df)) * 60
    return df


def test_swings_and_labels():
    sw = find_swings(_zigzag(), 3)
    labels = [s.label for s in sw if s.label]
    assert "HH" in labels and "HL" in labels
    assert all(s.confirm_idx == s.idx + 3 for s in sw)  # no look-ahead


def test_structure_up_trend():
    df = _zigzag()
    trend, events = market_structure(df, find_swings(df, 3), atr(df))
    assert trend == "up"
    assert any(e.kind == "BOS" and e.direction == "up" for e in events)


def test_fvg_detection():
    df = pd.DataFrame({"open": [10, 10.5, 11.0, 13.0, 13.2], "close": [10.4, 10.9, 13.0, 13.2, 13.3],
                       "high": [10.5, 11.0, 13.1, 13.4, 13.5], "low": [9.9, 10.4, 10.9, 12.0, 13.0]})
    z = fair_value_gaps(df, pd.Series([1.0] * 5), min_atr=0.3)
    assert any(f.direction == "bull" and f.bottom == 11.0 and f.top == 12.0 for f in z)


def test_decision_card_shape_on_paper_data():
    b = PaperBroker(["XAUUSD"])
    frames = {tf: b.candles("XAUUSD", tf, 300) for tf in TFS}
    card = evaluate("XAUUSD", frames, b.spec("XAUUSD"), b.account(), spread=0.2)
    assert card["grade"] in ("STRONG", "MODERATE", "WEAK", "NO_TRADE")
    assert "score" in card and "gates_failed" in card
    if card["grade"] == "NO_TRADE":
        assert card["headline"].startswith("NO TRADE")
    else:
        assert card["lots"] > 0 and card["sl"] and card["tp1"]


def test_news_window_forces_no_trade():
    b = PaperBroker(["XAUUSD"])
    frames = {tf: b.candles("XAUUSD", tf, 300) for tf in TFS}
    card = evaluate("XAUUSD", frames, b.spec("XAUUSD"), b.account(), spread=0.2, news_blocked="CPI")
    assert card["grade"] == "NO_TRADE"


def test_paper_idempotency_and_close():
    b = PaperBroker(["XAUUSD"])
    px = b.tick("XAUUSD")["ask"]
    r1 = b.market_order("XAUUSD", "BUY", 0.1, px - 5, px + 10, "t", "abc")
    r2 = b.market_order("XAUUSD", "BUY", 0.1, px - 5, px + 10, "t", "abc")
    assert r1["ok"] and r2.get("duplicate") and len(b.positions()) == 1
    assert b.close(r1["ticket"])["ok"] and not b.positions()


def test_closed_candles_only():
    b = PaperBroker(["EURUSD"])
    h1 = b.candles("EURUSD", "H1", 5)
    assert (h1["time"] % 3600 == 0).all()


def test_100_dollar_account_gold_sizing():
    # $100, 3% = $3 risk. Gold SL 2.00 away -> $200/lot -> 0.015 -> rounds DOWN to 0.01 ($2 risk)
    r = position_size(100, 3.0, 2000.0, 1998.0, GOLD)
    assert r["lots"] == 0.01 and r["risk_money"] == 2.0
    # SL 5.00 away needs $5 = 5% -> above 3% target -> rejected, never rounded up
    assert position_size(100, 3.0, 2000.0, 1995.0, GOLD)["lots"] == 0.0


def test_engine_auto_executes_and_manages():
    from app.engine import Engine
    from app.risk import risk_engine
    risk_engine.release()
    b = PaperBroker(["XAUUSD", "EURUSD"], balance=100.0)
    e = Engine(b)
    e.execution_mode = "AUTO"
    opened = 0
    for i in range(1500):
        b.step()
        e.manage_positions()
        if i % 3 == 0:
            for s in b.symbols:
                c = e.analyze_symbol(s)
                before = len(b.positions())
                e.on_card(c)
                opened += len(b.positions()) > before
                if c.get("auto_ok"):
                    assert c["lots"] >= 0.01 and c["sl"] and c["tp3"]
    acc = b.account()
    assert acc.equity > 0
    assert all(p["volume"] >= 0.01 for p in b.positions())


def test_engine_adopts_positions_after_restart(monkeypatch):
    monkeypatch.setattr(settings, "strategy_mode", "SMC")
    from app.engine import Engine
    b = PaperBroker(["XAUUSD"], balance=1000.0)
    px = b.tick("XAUUSD")["ask"]
    b.market_order("XAUUSD", "BUY", 0.1, px - 5, px + 15, "t", "x9")
    e = Engine(b)  # fresh engine = no plans (like a restart)
    e.manage_positions()
    tk = b.positions()[0]["ticket"]
    assert tk in e.plans and e.plans[tk]["partial_done"] is False
    assert abs(e.plans[tk]["tp1"] - (b.positions()[0]["price_open"] + 5 * settings.tp1_r)) < 1e-6


def test_cooldown_blocks_reentry():
    import time as _t
    from app.engine import Engine
    from app.risk import risk_engine
    risk_engine.release()
    b = PaperBroker(["XAUUSD"], balance=1000.0)
    e = Engine(b)
    e.execution_mode = "AUTO"
    e.cooldown_until["XAUUSD"] = _t.time() + 600
    card = {"symbol": "XAUUSD", "grade": "STRONG", "direction": "BUY", "auto_ok": True, "id": "c1"}
    e.on_card(card)
    assert not b.positions()


def test_boost_only_when_green_and_strong():
    from app.engine import Engine
    from app.risk import risk_engine
    risk_engine.release()
    b = PaperBroker(["XAUUSD"], balance=1000.0)
    e = Engine(b)
    st = risk_engine.state
    st.consecutive_losses = 0
    card = {"symbol": "XAUUSD", "grade": "STRONG", "lots": 0.30}
    st.day_start_equity = 2000.0  # day is red
    assert e._maybe_boost(card, "auto")["lots"] == 0.30
    st.day_start_equity = 900.0  # day is green
    assert e._maybe_boost(card, "auto")["lots"] > 0.30
    assert e._maybe_boost({**card, "grade": "MODERATE"}, "auto")["lots"] == 0.30
    st.consecutive_losses = 1
    assert e._maybe_boost(card, "auto")["lots"] == 0.30
    st.consecutive_losses = 0


def test_profit_lock_pauses_after_giveback():
    from app.engine import Engine
    from app.risk import risk_engine
    b = PaperBroker(["XAUUSD"], balance=100.0)
    e = Engine(b)
    risk_engine.state.day_start_equity = 90.0  # day up $10 at peak
    assert e.profit_lock() is None
    risk_engine.state.day_start_equity = 98.0  # simulate: peak 100 recorded, now treat as gave back
    e._day_peak = 110.0  # best gain 12 -> floor 104 > equity 100
    assert e.profit_lock()
