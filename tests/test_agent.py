import numpy as np
import pandas as pd

from app.agent import features as F, meta as M
from app.agent.backtest import walk_forward
from app.agent.hybrid import evaluate
from app.risk import SymbolSpec


def _series(n=3000, seed=3):
    rng = np.random.default_rng(seed)
    # regime-switching drift so trend + range events both appear
    drift = np.repeat(rng.choice([-0.4, 0.0, 0.4], size=n // 200 + 1), 200)[:n]
    close = 2000 + np.cumsum(drift + rng.normal(0, 1.2, n))
    o = np.concatenate([[close[0]], close[:-1]])
    hi = np.maximum(o, close) + rng.uniform(0.1, 1.0, n)
    lo = np.minimum(o, close) - rng.uniform(0.1, 1.0, n)
    return pd.DataFrame({"time": np.arange(n) * 900.0 + 1.7e9, "open": o, "high": hi, "low": lo, "close": close,
                         "tick_volume": 1.0})


GOLD = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 0.01, 100, 0.01)


def test_features_and_labels_are_point_in_time():
    df = _series()
    d1 = F.build(df)
    d2 = F.build(df.iloc[:2000])
    # features at bar 1500 must not change when future bars are removed
    assert np.allclose(F.feature_row(d1, 1500), F.feature_row(d2, 1500), equal_nan=True)
    ev = F.events(d1)
    assert len(ev) > 50
    lab, r = F.triple_barrier(d1, ev[0], int(d1["signal"].iloc[ev[0]]), 1.5, 1.5, 48)
    assert lab in (0, 1) and -1.0 <= r <= 1.5 + 1e-9 or lab == 0


def test_meta_model_trains_and_predicts():
    m = M.train(_series())
    assert m is not None and m.n >= 20
    p = m.predict(np.zeros(len(F.FEATURES)))
    assert 0 < p < 1
    m2 = M.MetaModel.from_json(m.to_json())
    assert abs(m2.predict(np.ones(len(F.FEATURES))) - m.predict(np.ones(len(F.FEATURES)))) < 1e-9


def test_hybrid_card_always_has_stop_and_respects_gates():
    df = _series()
    m = M.train(df)
    seen = 0
    for end in range(1200, 3000, 7):
        c = evaluate("XAUUSD", df.iloc[:end], GOLD, 0.2, None, 10000.0, m)
        if c["grade"] == "HYBRID":
            seen += 1
            sgn = 1 if c["direction"] == "BUY" else -1
            assert (c["entry"] - c["sl"]) * sgn > 0 and (c["tp1"] - c["entry"]) * sgn > 0
            assert c["risk_pct"] <= 1.5 + 1e-9
        else:
            assert c["gates_failed"]
    c = evaluate("XAUUSD", df.iloc[:1500], GOLD, 0.2, None, 10000.0, None)
    assert c["grade"] == "NO_TRADE"


def test_walk_forward_runs():
    r = walk_forward(_series(), spread=0.2)
    assert r["ok"] and "out_of_sample" in r and r["verdict"]
