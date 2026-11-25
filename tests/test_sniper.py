import numpy as np
import pandas as pd

from app import sniper


def _df(n=1500, seed=5):
    rng = np.random.default_rng(seed)
    c = 90000 + np.cumsum(rng.normal(0, 40, n))
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) + rng.uniform(5, 60, n)
    l = np.minimum(o, c) - rng.uniform(5, 60, n)
    return pd.DataFrame({"time": np.arange(n) * 60.0, "open": o, "high": h, "low": l, "close": c,
                         "tick_volume": rng.uniform(50, 200, n)})


def test_signals_have_valid_levels_and_backtest_runs():
    d = sniper.prepare(_df())
    seen = 0
    for i in range(30, len(d)):
        s = sniper.signal_at(d, i, {}, 0.01)
        if s:
            seen += 1
            assert (s["trig"] - s["sl"]) * s["dir"] > 0 and (s["tp"] - s["trig"]) * s["dir"] > 0
    r = sniper.backtest(_df(), 0.01, 10.0, 0.01)
    assert "trades" in r


def test_retry_then_cooldown():
    st = {}
    sniper.on_closed(st, -1); sniper.on_closed(st, -1)
    assert st["retries"] == 2 and st["shadow"] == 8
    sniper.on_closed(st, -1)
    assert st["cooldown"] == 6 and st["shadow"] == 0
