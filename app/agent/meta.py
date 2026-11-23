"""Stage-2 meta-labeling model: a small L2-regularised logistic regression (numpy) trained on
triple-barrier labels of the stage-1 signals. It answers one question: given this context, what is
the probability p that this signal reaches its profit barrier before its stop?"""
from __future__ import annotations

import json
import time

import numpy as np

from ..config import settings
from . import features as F


class MetaModel:
    def __init__(self, w=None, mu=None, sd=None, n=0, base=0.0, trained=0.0, auc=None):
        self.w, self.mu, self.sd, self.n, self.base, self.trained, self.auc = w, mu, sd, n, base, trained, auc

    @property
    def ready(self) -> bool:
        return self.w is not None and self.n >= settings.meta_min_events

    def predict(self, x: np.ndarray) -> float:
        z = (x - self.mu) / self.sd
        return float(1 / (1 + np.exp(-(self.w[0] + z @ self.w[1:]))))

    def to_json(self) -> str:
        return json.dumps({"w": self.w.tolist(), "mu": self.mu.tolist(), "sd": self.sd.tolist(), "n": self.n,
                           "base": self.base, "trained": self.trained, "auc": self.auc})

    @classmethod
    def from_json(cls, s: str) -> "MetaModel":
        j = json.loads(s)
        return cls(np.array(j["w"]), np.array(j["mu"]), np.array(j["sd"]), j["n"], j["base"], j["trained"], j.get("auc"))


def _auc(y, p) -> float | None:
    pos, neg = p[y == 1], p[y == 0]
    if not len(pos) or not len(neg):
        return None
    return float((pos[:, None] > neg[None, :]).mean() + 0.5 * (pos[:, None] == neg[None, :]).mean())


def fit(X: np.ndarray, y: np.ndarray, l2: float = 25.0, iters: int = 600, lr: float = 0.1) -> MetaModel:
    mu, sd = X.mean(0), X.std(0)
    sd[sd == 0] = 1
    Z = (X - mu) / sd
    n, k = Z.shape
    w = np.zeros(k + 1)
    w[0] = np.log((y.mean() + 1e-3) / (1 - y.mean() + 1e-3))
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(w[0] + Z @ w[1:])))
        g0 = (p - y).mean()
        g = Z.T @ (p - y) / n + l2 * w[1:] / n
        w[0] -= lr * g0
        w[1:] -= lr * g
    m = MetaModel(w, mu, sd, n, float(y.mean()), time.time())
    m.auc = _auc(y, np.array([m.predict(x) for x in X]))
    return m


def dataset(d, idx: list[int]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    X, y, R = [], [], []
    for i in idx:
        side = int(d["signal"].iloc[i])
        lab, r = F.triple_barrier(d, i, side, settings.hybrid_sl_atr, settings.hybrid_tp1_r, settings.hybrid_horizon_bars)
        X.append(F.feature_row(d, i))
        y.append(lab)
        R.append(r)
    return np.array(X), np.array(y, dtype=float), np.array(R)


def train(df) -> MetaModel | None:
    d = F.build(df)
    # drop the last `horizon` bars: their labels would look into data we don't have yet
    idx = [i for i in F.events(d) if i < len(d) - settings.hybrid_horizon_bars]
    if len(idx) < 20:
        return None
    X, y, _ = dataset(d, idx)
    return fit(X, y)
