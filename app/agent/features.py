"""Shared M15 feature/signal engine for the hybrid agent (used identically live and in backtests,
so there is no train/serve skew). Higher-timeframe context comes from long EMAs on the same series
(EMA80/EMA200 on M15 ~ EMA20/EMA50 on H1/H4), which keeps every feature point-in-time."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import adx, atr, ema, rsi

REGIMES = ("TREND_UP", "TREND_DOWN", "RANGE", "CRISIS")
FEATURES = ["adx", "atr_pct", "rsi", "dist_ema50", "htf_agree", "htf2_agree", "z", "body", "hour_sin", "hour_cos",
            "sig_trend", "mom10"]


def build(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy().reset_index(drop=True)
    c = d["close"]
    d["ema20"], d["ema50"], d["ema80"], d["ema200"], d["ema800"] = ema(c, 20), ema(c, 50), ema(c, 80), ema(c, 200), ema(c, 800)
    d["atr"] = atr(d, 14)
    d["adx"] = adx(d, 14)
    d["rsi"] = rsi(c, 14)
    d["atr_pct"] = d["atr"].rolling(500, min_periods=100).rank(pct=True)
    sma, sd = c.rolling(20).mean(), c.rolling(20).std()
    d["z"] = (c - sma) / sd.replace(0, np.nan)
    d["don_hi"], d["don_lo"] = d["high"].rolling(20).max().shift(1), d["low"].rolling(20).min().shift(1)
    d["htf"] = np.sign(d["ema80"] - d["ema200"])
    d["htf2"] = np.sign(d["ema200"] - d["ema800"])
    d["dist50"] = (c - d["ema50"]) / d["atr"]
    d["body_r"] = (c - d["open"]) / d["atr"]
    d["mom"] = (c - c.shift(10)) / d["atr"]
    hrs = (pd.to_datetime(d["time"], unit="s").dt.hour + pd.to_datetime(d["time"], unit="s").dt.minute / 60)
    d["hour_sin"], d["hour_cos"] = np.sin(2 * np.pi * hrs / 24), np.cos(2 * np.pi * hrs / 24)

    # regime (rule-based stand-in for an HMM: volatility extreme -> CRISIS, trending ADX + HTF slope -> TREND)
    reg = np.where(d["atr_pct"] > 0.97, "CRISIS",
                   np.where((d["adx"] >= 20) & (d["htf"] > 0), "TREND_UP",
                            np.where((d["adx"] >= 20) & (d["htf"] < 0), "TREND_DOWN", "RANGE")))
    d["regime"] = reg

    # stage-1 primary signals (+1 buy / -1 sell / 0), regime-conditioned
    up_pull = (d["low"] <= d["ema20"] + 0.2 * d["atr"]) & (c > d["ema20"]) & (c > d["open"])
    dn_pull = (d["high"] >= d["ema20"] - 0.2 * d["atr"]) & (c < d["ema20"]) & (c < d["open"])
    up_brk, dn_brk = c > d["don_hi"], c < d["don_lo"]
    t_up = (d["regime"] == "TREND_UP") & (c > d["ema50"]) & (up_pull | up_brk) & (d["rsi"] < 75)
    t_dn = (d["regime"] == "TREND_DOWN") & (c < d["ema50"]) & (dn_pull | dn_brk) & (d["rsi"] > 25)
    zp = d["z"].shift(1)
    r_up = (d["regime"] == "RANGE") & (zp < -2) & (d["z"] > zp) & (d["rsi"] < 40)
    r_dn = (d["regime"] == "RANGE") & (zp > 2) & (d["z"] < zp) & (d["rsi"] > 60)
    d["signal"] = np.where(t_up | r_up, 1, np.where(t_dn | r_dn, -1, 0))
    d["sig_trend"] = np.where(t_up | t_dn, 1.0, 0.0)
    return d


def feature_row(d: pd.DataFrame, i: int) -> np.ndarray:
    r = d.iloc[i]
    s = float(r["signal"]) or 1.0
    return np.array([
        r["adx"] / 50, r["atr_pct"], (r["rsi"] - 50) / 25 * s, r["dist50"] * s,
        r["htf"] * s, r["htf2"] * s, -r["z"] * s if r["sig_trend"] == 0 else r["z"] * s / 2,
        r["body_r"] * s, r["hour_sin"], r["hour_cos"], r["sig_trend"], r["mom"] * s / 3,
    ], dtype=float)


def triple_barrier(d: pd.DataFrame, i: int, side: int, sl_atr: float, tp_r: float, horizon: int) -> tuple[int, float]:
    """Label an event at bar i: first barrier touched in the next `horizon` bars.
    Returns (label 1 if profit barrier first, realised R incl. timeout mark-to-market)."""
    a = float(d["atr"].iloc[i])
    entry = float(d["close"].iloc[i])
    if not a or np.isnan(a):
        return 0, 0.0
    risk = sl_atr * a
    up_b, dn_b = (entry + tp_r * risk, entry - risk) if side > 0 else (entry - tp_r * risk, entry + risk)
    end = min(len(d) - 1, i + horizon)
    for j in range(i + 1, end + 1):
        hi, lo = float(d["high"].iloc[j]), float(d["low"].iloc[j])
        if side > 0:
            if lo <= dn_b:
                return 0, -1.0
            if hi >= up_b:
                return 1, tp_r
        else:
            if hi >= dn_b:
                return 0, -1.0
            if lo <= up_b:
                return 1, tp_r
    last = float(d["close"].iloc[end])
    return 0, (last - entry) * side / risk


def events(d: pd.DataFrame, start: int = 250) -> list[int]:
    idx = np.flatnonzero(d["signal"].values != 0)
    return [int(i) for i in idx if i >= start and not np.isnan(d["atr_pct"].iloc[i])]
