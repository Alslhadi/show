"""BTC strategy research on the broker's own candles, run server-side (the only place with Exness data).

Method follows the backtest-expert skill: rules with zero discretion, pessimistic costs (spread x1.5), stress
(costs x2.5), time-split robustness (4 sequential windows must mostly be positive), parameter plateaus (a config
only counts if its neighbours are also profitable), and a minimum sample (>= 30 trades).
Every strategy is a pure function of closed bars, shared with the live engine (no train/serve skew)."""
from __future__ import annotations

import itertools
import math

import numpy as np
import pandas as pd

from .indicators import atr as _atr, ema as _ema, rsi as _rsi


# ---------- signal generators: return list of (i, dir, entry_px, sl, tp, max_bars) -------------------
def _base(df: pd.DataFrame) -> pd.DataFrame:
    d = df.reset_index(drop=True).copy()
    d["atr"] = _atr(d, 14)
    for n in (5, 20, 50, 200):
        d[f"e{n}"] = _ema(d["close"], n)
    d["rsi2"] = _rsi(d["close"], 2)
    v = d["tick_volume"] if "tick_volume" in d and float(d["tick_volume"].abs().sum()) > 0 else (d["high"] - d["low"])
    d["act"], d["act_avg"] = v.astype(float), v.astype(float).rolling(20).mean()
    return d


def sig_sniper(d, p):
    L = p["look"]
    pl, ph = d["low"].rolling(L).min().shift(1), d["high"].rolling(L).max().shift(1)
    spike = d["act"] > d["act_avg"] * p["vm"]
    out = []
    for i in np.flatnonzero(((d["low"] < pl) & (d["close"] > pl) & (d["close"] > d["open"]) & spike).values):
        trig, sl = d["high"].iat[i], d["low"].iat[i] - 0.3 * d["atr"].iat[i]
        out.append((i, 1, trig, sl, trig + p["rr"] * (trig - sl), "stop"))
    for i in np.flatnonzero(((d["high"] > ph) & (d["close"] < ph) & (d["close"] < d["open"]) & spike).values):
        trig, sl = d["low"].iat[i], d["high"].iat[i] + 0.3 * d["atr"].iat[i]
        out.append((i, -1, trig, sl, trig - p["rr"] * (sl - trig), "stop"))
    return out


def sig_pullback(d, p):
    up = (d["e50"] > d["e200"]) & (d["close"] > d["e200"])
    dn = (d["e50"] < d["e200"]) & (d["close"] < d["e200"])
    tol = p["tol"] * d["atr"]
    bull = up & (d["low"] <= d["e20"] + tol) & (d["close"] > d["e20"]) & (d["close"] > d["open"])
    bear = dn & (d["high"] >= d["e20"] - tol) & (d["close"] < d["e20"]) & (d["close"] < d["open"])
    sw_lo, sw_hi = d["low"].rolling(p["sw"]).min(), d["high"].rolling(p["sw"]).max()
    out = []
    for i in np.flatnonzero(bull.values):
        e, sl = d["close"].iat[i], sw_lo.iat[i] - 0.2 * d["atr"].iat[i]
        if e - sl > 0.3 * d["atr"].iat[i]:
            out.append((i, 1, e, sl, e + p["rr"] * (e - sl), "mkt"))
    for i in np.flatnonzero(bear.values):
        e, sl = d["close"].iat[i], sw_hi.iat[i] + 0.2 * d["atr"].iat[i]
        if sl - e > 0.3 * d["atr"].iat[i]:
            out.append((i, -1, e, sl, e - p["rr"] * (sl - e), "mkt"))
    return out


def sig_breakout(d, p):
    n = p["n"]
    hi, lo = d["high"].rolling(n).max().shift(1), d["low"].rolling(n).min().shift(1)
    up = (d["close"] > hi) & (d["close"] > d["e200"])
    dn = (d["close"] < lo) & (d["close"] < d["e200"])
    out = []
    for i in np.flatnonzero(up.values):
        e = d["close"].iat[i]; sl = e - p["k"] * d["atr"].iat[i]
        out.append((i, 1, e, sl, e + p["rr"] * (e - sl), "mkt"))
    for i in np.flatnonzero(dn.values):
        e = d["close"].iat[i]; sl = e + p["k"] * d["atr"].iat[i]
        out.append((i, -1, e, sl, e - p["rr"] * (sl - e), "mkt"))
    return out


def sig_meanrev(d, p):
    """Connors-style RSI(2) dip inside an uptrend (and the mirror for downtrends)."""
    up = d["close"] > d["e200"]
    out = []
    for i in np.flatnonzero((up & (d["rsi2"] < p["lo"])).values):
        e = d["close"].iat[i]; sl = e - p["k"] * d["atr"].iat[i]
        out.append((i, 1, e, sl, e + p["rr"] * (e - sl), "mkt"))
    for i in np.flatnonzero((~up & (d["rsi2"] > 100 - p["lo"])).values):
        e = d["close"].iat[i]; sl = e + p["k"] * d["atr"].iat[i]
        out.append((i, -1, e, sl, e - p["rr"] * (sl - e), "mkt"))
    return out


STRATS = {
    "sniper": (sig_sniper, {"look": [20, 30], "vm": [1.3, 1.6, 2.0], "rr": [1.5, 1.8, 2.2]}),
    "pullback": (sig_pullback, {"tol": [0.1, 0.3], "sw": [5, 10], "rr": [1.5, 2.0, 2.5]}),
    "breakout": (sig_breakout, {"n": [20, 40], "k": [1.0, 1.5, 2.0], "rr": [2.0, 3.0, 4.0]}),
    "meanrev": (sig_meanrev, {"lo": [5, 10], "k": [1.5, 2.5], "rr": [1.0, 1.5, 2.0]}),
}


RISKS: list = []


def simulate(d, sigs, cost, be_r=0.6, max_bars=96):
    """One position at a time; entry next bar (mkt at open; stop must trade through the trigger on the next bar).
    Same-bar SL/TP: assume SL first. BE: at +be_r R move stop to +0.1R. R is net of `cost` (price units)."""
    sigs = sorted(sigs, key=lambda x: x[0])
    H, L, O = d["high"].values, d["low"].values, d["open"].values
    global RISKS
    RISKS = []
    R, times, busy_until = [], [], -1
    n = len(d)
    for i, s, e, sl, tp, kind in sigs:
        j = i + 1
        if j >= n or i <= busy_until:
            continue
        if kind == "stop":
            if not ((H[j] >= e) if s > 0 else (L[j] <= e)):
                continue
            entry = e
        else:
            entry = O[j]
            # keep the planned R multiple relative to the actual fill
            risk0 = abs(e - sl); sl = entry - s * risk0; tp = entry + s * abs(tp - e)
        risk = abs(entry - sl)
        if risk <= 0 or not math.isfinite(risk):
            continue
        stop, be = sl, False
        res, k = None, j
        for k in range(j, min(n, j + max_bars)):
            hit_sl = (L[k] <= stop) if s > 0 else (H[k] >= stop)
            hit_tp = (H[k] >= tp) if s > 0 else (L[k] <= tp)
            if hit_sl:
                res = (stop - entry) * s; break
            if hit_tp:
                res = (tp - entry) * s; break
            fav = (H[k] - entry) if s > 0 else (entry - L[k])
            if not be and fav >= be_r * risk:
                stop, be = entry + s * 0.1 * risk, True
        if res is None:
            res = (d["close"].iat[k] - entry) * s
        R.append((res - cost) / risk)
        RISKS.append(risk)
        times.append(i)
        busy_until = k
    return np.array(R), np.array(times)


def metrics(R):
    if not len(R):
        return {"n": 0, "pf": 0.0, "exp": 0.0, "win": 0.0, "dd": 0.0}
    gp, gl = R[R > 0].sum(), -R[R < 0].sum()
    eq = np.cumsum(R)
    dd = float((np.maximum.accumulate(np.concatenate([[0], eq])) - np.concatenate([[0], eq])).max())
    return {"n": int(len(R)), "pf": round(float(gp / gl), 2) if gl else 9.99, "exp": round(float(R.mean()), 3),
            "win": round(float((R > 0).mean()), 2), "dd": round(dd, 1)}


def run(frames: dict, spread: float) -> list[dict]:
    """frames: tf -> DataFrame. Returns ranked robust configs (best first)."""
    rows = []
    for tf, df in frames.items():
        if df is None or len(df) < 600:
            continue
        d = _base(df)
        n = len(d)
        for name, (fn, grid) in STRATS.items():
            keys = list(grid)
            res = {}
            for combo in itertools.product(*(grid[k] for k in keys)):
                p = dict(zip(keys, combo))
                sigs = [x for x in fn(d, p) if x[0] > 200]
                R, t = simulate(d, sigs, cost=1.5 * spread)
                avg_risk = float(np.mean(RISKS)) if RISKS else 0.0
                R2, _ = simulate(d, sigs, cost=2.5 * spread)
                m, m2 = metrics(R), metrics(R2)
                w = [R[(t >= 200 + q * (n - 200) // 4) & (t < 200 + (q + 1) * (n - 200) // 4)] for q in range(4)]
                pos_w = sum(1 for x in w if len(x) and x.sum() > 0)
                res[combo] = dict(tf=tf, strat=name, params=p, **m, pf_stress=m2["pf"], exp_stress=m2["exp"], win_q=pos_w, stop_px=round(avg_risk, 1),
                                   days=round((d["time"].iat[-1] - d["time"].iat[200]) / 86400, 1))
            # plateau: neighbours (one param one step away) must also be profitable
            for combo, r in res.items():
                nb = []
                for ki, k in enumerate(keys):
                    vals = grid[k]; idx = vals.index(combo[ki])
                    for dj in (-1, 1):
                        if 0 <= idx + dj < len(vals):
                            c2 = list(combo); c2[ki] = vals[idx + dj]
                            nb.append(res[tuple(c2)]["pf"])
                r["plateau"] = round(float(np.mean(nb)), 2) if nb else 0.0
                r["robust"] = (r["n"] >= 30 and r["pf"] >= 1.25 and r["pf_stress"] >= 1.05 and r["win_q"] >= 3
                               and r["plateau"] >= 1.1)
                rows.append(r)
    rows.sort(key=lambda r: (r["robust"], r["exp_stress"] * math.sqrt(max(r["n"], 1))), reverse=True)
    return rows
