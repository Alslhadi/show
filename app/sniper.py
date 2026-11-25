"""Port of the user's TradingView strategy "BTC Smart Re-Entry Sniper" (Pine v5).

Primary entry: liquidity sweep of the prior `lookback` low/high that closes back inside with a strong candle and a
volume spike (if the broker sends no volume, candle range vs its 20-bar average is used as the activity proxy).
Entry is a STOP order 2 ticks beyond the signal candle (valid for the next bar only), so a trade only exists if
price confirms. SL = candle extreme +/- 0.3 ATR, TP = 1.8R, break-even at +0.6R, then ATR trail (1.2 ATR).
Re-entry ("shadow" mode): after a loss, up to 2 retries in the same direction within 8 bars on a strong
continuation candle; after that, a 6-bar cooldown. All of it is evaluated on closed bars only."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .indicators import atr as _atr

P = dict(lookback=20, vol_mult=1.6, rr=1.8, be_r=0.6, atr_buf=0.3, max_retries=2, shadow_bars=8, cooldown_bars=6,
         trail_atr=1.2, re_vol=1.1)


def _activity(df: pd.DataFrame) -> pd.Series:
    v = df["tick_volume"] if "tick_volume" in df else df.get("volume")
    if v is not None and float(pd.Series(v).fillna(0).abs().sum()) > 0:
        return pd.Series(v, index=df.index).astype(float)
    return (df["high"] - df["low"]).astype(float)  # no volume from broker -> range as activity


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    d = df.reset_index(drop=True).copy()
    L = P["lookback"]
    d["prev_low"] = d["low"].rolling(L).min().shift(1)
    d["prev_high"] = d["high"].rolling(L).max().shift(1)
    act = _activity(d)
    d["act"], d["act_avg"] = act, act.rolling(20).mean()
    d["atr"] = _atr(d, 14)
    spike = d["act"] > d["act_avg"] * P["vol_mult"]
    d["bull_sweep"] = (d["low"] < d["prev_low"]) & (d["close"] > d["prev_low"]) & (d["close"] > d["open"]) & spike
    d["bear_sweep"] = (d["high"] > d["prev_high"]) & (d["close"] < d["prev_high"]) & (d["close"] < d["open"]) & spike
    return d


def signal_at(d: pd.DataFrame, i: int, state: dict, tick: float) -> dict | None:
    """state: {'last_dir', 'shadow', 'cooldown'} in bars (caller decrements). Returns a pending stop order or None."""
    r, p = d.iloc[i], d.iloc[i - 1]
    if state.get("cooldown", 0) > 0 or np.isnan(r["atr"]):
        return None
    strong = r["act"] > r["act_avg"] * P["re_vol"]
    re_bull = state.get("shadow", 0) > 0 and state.get("last_dir") == 1 and r["close"] > r["open"] and strong and r["close"] > p["high"]
    re_bear = state.get("shadow", 0) > 0 and state.get("last_dir") == -1 and r["close"] < r["open"] and strong and r["close"] < p["low"]
    if r["bull_sweep"] or re_bull:
        trig = float(r["high"]) + 2 * tick
        sl = float(r["low"]) - P["atr_buf"] * float(r["atr"])
        risk = trig - sl
        if risk > 0:
            return {"dir": 1, "trig": trig, "sl": sl, "tp": trig + P["rr"] * risk, "risk": risk,
                    "atr": float(r["atr"]), "why": "إعادة دخول شراء" if re_bull and not r["bull_sweep"] else "قنص سيولة شراء"}
    elif r["bear_sweep"] or re_bear:
        trig = float(r["low"]) - 2 * tick
        sl = float(r["high"]) + P["atr_buf"] * float(r["atr"])
        risk = sl - trig
        if risk > 0:
            return {"dir": -1, "trig": trig, "sl": sl, "tp": trig - P["rr"] * risk, "risk": risk,
                    "atr": float(r["atr"]), "why": "إعادة دخول بيع" if re_bear and not r["bear_sweep"] else "قنص سيولة بيع"}
    return None


def on_closed(state: dict, profit: float) -> None:
    if profit < 0:
        if state.get("retries", 0) < P["max_retries"]:
            state["retries"] = state.get("retries", 0) + 1
            state["shadow"] = P["shadow_bars"]
        else:
            state.update(retries=0, shadow=0, cooldown=P["cooldown_bars"])
    else:
        state.update(retries=0, shadow=0)


def backtest(df: pd.DataFrame, tick: float, spread: float, usd_per_unit: float) -> dict:
    """Bar-by-bar replay of the same rules (stop entry next bar, SL/TP/BE/trail on highs/lows; SL first if both)."""
    d = prepare(df)
    state, pend, pos, R = {}, None, None, []
    for i in range(30, len(d)):
        b = d.iloc[i]
        for k in ("shadow", "cooldown"):
            if state.get(k, 0) > 0:
                state[k] -= 1
        if pos:
            s = pos["dir"]
            hit_sl = (b["low"] <= pos["sl"]) if s > 0 else (b["high"] >= pos["sl"])
            hit_tp = (b["high"] >= pos["tp"]) if s > 0 else (b["low"] <= pos["tp"])
            exit_px = pos["sl"] if hit_sl else pos["tp"] if hit_tp else None
            if exit_px is not None:
                r = ((exit_px - pos["entry"]) * s - spread) / pos["risk"]
                R.append(r)
                on_closed(state, r)
                pos = None
            else:
                fav = (b["high"] - pos["entry"]) if s > 0 else (pos["entry"] - b["low"])
                if not pos["be"] and fav >= P["be_r"] * pos["risk"]:
                    pos["sl"], pos["be"] = pos["entry"] + s * 0.1 * pos["risk"], True
                if pos["be"]:
                    t = b["close"] - s * P["trail_atr"] * b["atr"]
                    if (t - pos["sl"]) * s > 0:
                        pos["sl"] = t
        if pos is None and pend:
            hit = (b["high"] >= pend["trig"]) if pend["dir"] > 0 else (b["low"] <= pend["trig"])
            if hit:
                pos = {**pend, "entry": pend["trig"], "be": False}
                state["last_dir"] = pend["dir"]
            pend = None
        if pos is None:
            pend = signal_at(d, i, state, tick)
    R = np.array(R)
    if not len(R):
        return {"trades": 0}
    gp, gl = R[R > 0].sum(), -R[R < 0].sum()
    return {"trades": int(len(R)), "win_rate": round(float((R > 0).mean()), 3),
            "profit_factor": round(float(gp / gl), 2) if gl else None, "expectancy_r": round(float(R.mean()), 3),
            "total_r": round(float(R.sum()), 2)}
