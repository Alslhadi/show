"""20-indicator confluence vote. Each indicator votes +1 (buy), -1 (sell) or 0 (neutral).
Many indicators are correlated, so this is a filter that confirms direction, not 20 independent proofs."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .indicators import adx, atr, ema, macd, rsi


def _last(s):
    return float(s.iloc[-1])


def vote(df: pd.DataFrame) -> dict:
    c, h, l = df["close"], df["high"], df["low"]
    v = df["volume"] if "volume" in df else pd.Series(1.0, index=df.index)
    px = _last(c)
    sgn = lambda x: 1 if x > 0 else -1 if x < 0 else 0
    out = {}
    out["EMA 9/20"] = sgn(_last(ema(c, 9)) - _last(ema(c, 20)))
    out["EMA 20/50"] = sgn(_last(ema(c, 20)) - _last(ema(c, 50)))
    out["EMA 50/200"] = sgn(_last(ema(c, 50)) - _last(ema(c, 200)))
    out["السعر فوق/تحت EMA50"] = sgn(px - _last(ema(c, 50)))
    m, ms, mh = macd(c)
    out["MACD"] = sgn(_last(m) - _last(ms))
    out["MACD Histogram"] = sgn(_last(mh) - float(mh.iloc[-2]))
    r = _last(rsi(c))
    out["RSI"] = 1 if 50 < r < 75 else -1 if 25 < r < 50 else 0
    lo14, hi14 = l.rolling(14).min(), h.rolling(14).max()
    k = ((c - lo14) / (hi14 - lo14).replace(0, np.nan) * 100).rolling(3).mean()
    out["Stochastic"] = sgn(_last(k) - _last(k.rolling(3).mean()))
    tp = (h + l + c) / 3
    cci = (tp - tp.rolling(20).mean()) / (0.015 * tp.rolling(20).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True))
    out["CCI"] = 1 if _last(cci) > 0 else -1
    up, dn = h.diff(), -l.diff()
    a14 = atr(df, 14)
    pdi = 100 * pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index).ewm(alpha=1 / 14, adjust=False).mean() / a14
    mdi = 100 * pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index).ewm(alpha=1 / 14, adjust=False).mean() / a14
    out["ADX +DI/-DI"] = sgn(_last(pdi) - _last(mdi)) if _last(adx(df)) >= 18 else 0
    wr = (hi14 - c) / (hi14 - lo14).replace(0, np.nan) * -100
    out["Williams %R"] = 1 if _last(wr) > -50 else -1
    out["ROC"] = sgn(px - float(c.iloc[-11]))
    out["Momentum"] = sgn(px - float(c.iloc[-6]))
    mid = c.rolling(20).mean()
    out["Bollinger"] = sgn(px - _last(mid))
    # Supertrend (10, 3)
    a10 = atr(df, 10)
    hl2 = (h + l) / 2
    ub, lb = hl2 + 3 * a10, hl2 - 3 * a10
    trend = 1
    fu, fl = ub.iloc[0], lb.iloc[0]
    for i in range(1, len(df)):
        fu = ub.iloc[i] if ub.iloc[i] < fu or c.iloc[i - 1] > fu else fu
        fl = lb.iloc[i] if lb.iloc[i] > fl or c.iloc[i - 1] < fl else fl
        if c.iloc[i] > fu:
            trend = 1
        elif c.iloc[i] < fl:
            trend = -1
    out["Supertrend"] = trend
    tenkan = (h.rolling(9).max() + l.rolling(9).min()) / 2
    kijun = (h.rolling(26).max() + l.rolling(26).min()) / 2
    out["Ichimoku"] = sgn(_last(tenkan) - _last(kijun))
    don_hi, don_lo = h.rolling(20).max().iloc[-2], l.rolling(20).min().iloc[-2]
    out["Donchian"] = sgn(px - (don_hi + don_lo) / 2)
    obv = (np.sign(c.diff()).fillna(0) * v).cumsum()
    out["OBV"] = sgn(_last(obv) - _last(obv.rolling(20).mean()))
    ha_c = (df["open"] + h + l + c) / 4
    ha_o = ((df["open"] + c) / 2).shift(1)
    out["Heikin-Ashi"] = sgn(_last(ha_c) - float(ha_o.iloc[-1]))
    kc_mid = ema(c, 20)
    out["Keltner"] = sgn(px - _last(kc_mid))
    buy = sum(1 for x in out.values() if x > 0)
    sell = sum(1 for x in out.values() if x < 0)
    return {"votes": out, "buy": buy, "sell": sell, "total": len(out)}
