import numpy as np
import pandas as pd

from app import research


def _df(n=1500, seed=2):
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.choice([-20, 0, 20], size=n // 150 + 1), 150)[:n]
    c = 90000 + np.cumsum(drift + rng.normal(0, 60, n))
    o = np.concatenate([[c[0]], c[:-1]])
    return pd.DataFrame({"time": np.arange(n) * 300.0, "open": o, "high": np.maximum(o, c) + rng.uniform(5, 80, n),
                         "low": np.minimum(o, c) - rng.uniform(5, 80, n), "close": c, "tick_volume": rng.uniform(50, 300, n)})


def test_research_ranks_configs():
    rows = research.run({"M5": _df()}, spread=15.0)
    assert rows and {"strat", "pf", "n", "robust", "plateau"} <= set(rows[0])
    assert {r["strat"] for r in rows} == set(research.STRATS)


def test_btc_mode_gate_and_step(monkeypatch):
    from app.config import settings
    from app.engine import Engine
    from app.brokers.paper import PaperBroker
    monkeypatch.setattr(settings, "bot_paused", False)
    monkeypatch.setattr(settings, "btc_min_equity", 0.0)
    eng = Engine(PaperBroker(["BTCUSD"]))
    assert eng._btc_gate() is None
    eng.btc_record(-1.0); eng.btc_record(-1.0)
    assert "تبريد" in eng._btc_gate()
    eng.btc_ledger.clear(); eng.btc_record(-13.0)
    assert "قاطع" in eng._btc_gate()
    eng.btc_ledger.clear()
    eng._btc_step("M5:meanrev:lo=5,k=2.5,rr=1.0")   # must not raise on paper data
