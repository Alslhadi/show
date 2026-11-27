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
