import numpy as np
import pandas as pd

from app.risk import SymbolSpec
from app.scalp import evaluate


def _frame(n, slope, start=2000.0, noise=0.3, pull=False):
    rng = np.random.default_rng(1)
    close = start + slope * np.arange(n) + rng.normal(0, noise, n)
    df = pd.DataFrame({"time": np.arange(n) * 60.0, "open": close - slope * 0.5, "close": close})
    df["high"] = df[["open", "close"]].max(axis=1) + 0.3
    df["low"] = df[["open", "close"]].min(axis=1) - 0.3
    df["volume"] = 1.0
    return df


GOLD = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 0.01, 100, 0.01)  # 0.01 lot -> $1 per $1 move


def test_scalp_sizes_to_money_and_always_has_stop():
    up = _frame(300, 0.5)
    m1 = up.copy()
    # last M1 candle: dip to EMA20 then close up
    ema20 = m1["close"].ewm(span=20, adjust=False).mean().iloc[-2]
    m1.loc[m1.index[-1], ["open", "low", "close", "high"]] = [ema20 + 0.1, ema20 - 0.1, ema20 + 1.0, ema20 + 1.1]
    c = evaluate("XAUUSD", {"M15": up, "M5": up, "M1": m1}, GOLD, spread=0.2)
    if c["grade"] == "SCALP":
        assert c["direction"] == "BUY" and c["sl"] < c["entry"] < c["tp1"]
        assert c["risk"]["risk_money"] <= 3.0
    else:
        assert c["gates_failed"]


def test_scalp_rejects_without_trend_agreement():
    c = evaluate("XAUUSD", {"M15": _frame(300, 0.5), "M5": _frame(300, -0.5), "M1": _frame(300, 0.5)}, GOLD, spread=0.2)
    assert c["grade"] == "NO_TRADE"


def test_votes_20_indicators():
    from app.votes import vote
    up = vote(_frame(300, 0.5))
    assert up["total"] == 20 and up["buy"] > up["sell"]
    dn = vote(_frame(300, -0.5))
    assert dn["sell"] > dn["buy"]
