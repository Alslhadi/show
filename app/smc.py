"""Smart-Money-Concepts detection with explicit, measurable rules.

Every pattern is a deterministic function of past candles only. A swing point is
"known" only N bars after it forms (confirm_idx), which prevents look-ahead bias
in backtests. Thresholds are expressed in ATR so they adapt to each symbol.

These are price patterns, not visibility into bank orders. Their usefulness must
be measured per symbol/timeframe in the research lab before being trusted.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import pandas as pd

from .indicators import atr as atr_fn


@dataclass
class Swing:
    idx: int
    confirm_idx: int
    kind: str  # "H" | "L"
    price: float
    label: str = ""  # HH / LH / HL / LL


@dataclass
class StructureEvent:
    idx: int
    kind: str  # BOS | CHOCH
    direction: str  # up | down
    level: float


@dataclass
class Zone:
    kind: str  # FVG | OB
    direction: str  # bull | bear
    top: float
    bottom: float
    idx: int
    mitigated: bool = False


def find_swings(df: pd.DataFrame, n: int = 3) -> list[Swing]:
    hi, lo = df["high"].values, df["low"].values
    swings: list[Swing] = []
    for i in range(n, len(df) - n):
        if hi[i] == max(hi[i - n : i + n + 1]) and hi[i] > max(hi[i - n : i]):
            swings.append(Swing(i, i + n, "H", float(hi[i])))
        if lo[i] == min(lo[i - n : i + n + 1]) and lo[i] < min(lo[i - n : i]):
            swings.append(Swing(i, i + n, "L", float(lo[i])))
    swings.sort(key=lambda s: (s.idx, s.kind))
    last = {"H": None, "L": None}
    for s in swings:
        prev = last[s.kind]
        if prev is not None:
            if s.kind == "H":
                s.label = "HH" if s.price > prev.price else "LH"
            else:
                s.label = "HL" if s.price > prev.price else "LL"
        last[s.kind] = s
    return swings


def market_structure(df: pd.DataFrame, swings: list[Swing], atr_s: pd.Series, k: float = 0.1):
    """Walk candles forward; BOS = close beyond last swing in trend direction,
    CHoCH = first close beyond the protective swing against the trend."""
    close = df["close"].values
    events: list[StructureEvent] = []
    trend = "range"
    last_h = last_l = None
    broken_h = broken_l = None
    pending = sorted(swings, key=lambda s: s.confirm_idx)
    p = 0
    for i in range(len(df)):
        while p < len(pending) and pending[p].confirm_idx <= i:
            s = pending[p]
            if s.kind == "H":
                last_h = s
            else:
                last_l = s
            p += 1
        buf = k * float(atr_s.iloc[i])
        if last_h is not None and broken_h is not last_h and close[i] > last_h.price + buf:
            kind = "BOS" if trend == "up" else "CHOCH"
            events.append(StructureEvent(i, kind, "up", last_h.price))
            trend, broken_h = "up", last_h
        if last_l is not None and broken_l is not last_l and close[i] < last_l.price - buf:
            kind = "BOS" if trend == "down" else "CHOCH"
            events.append(StructureEvent(i, kind, "down", last_l.price))
            trend, broken_l = "down", last_l
    return trend, events


def fair_value_gaps(df: pd.DataFrame, atr_s: pd.Series, min_atr: float = 0.3, lookback: int = 150):
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    zones: list[Zone] = []
    start = max(2, len(df) - lookback)
    for i in range(start, len(df)):
        a = float(atr_s.iloc[i])
        if l[i] > h[i - 2] and l[i] - h[i - 2] >= min_atr * a:
            zones.append(Zone("FVG", "bull", float(l[i]), float(h[i - 2]), i))
        if h[i] < l[i - 2] and l[i - 2] - h[i] >= min_atr * a:
            zones.append(Zone("FVG", "bear", float(l[i - 2]), float(h[i]), i))
    for z in zones:  # mitigated once price trades through the middle
        mid = (z.top + z.bottom) / 2
        after = slice(z.idx + 1, len(df))
        if z.direction == "bull" and (l[after] <= mid).any():
            z.mitigated = True
        if z.direction == "bear" and (h[after] >= mid).any():
            z.mitigated = True
    return zones


def order_blocks(df: pd.DataFrame, events: list[StructureEvent], atr_s: pd.Series, disp: float = 1.5):
    """OB = last opposite-colour candle before a displacement leg that produced a BOS/CHoCH."""
    o, c, h, l = (df[k].values for k in ("open", "close", "high", "low"))
    zones: list[Zone] = []
    for ev in events[-12:]:
        j = ev.idx
        lo = max(0, j - 10)
        body = abs(c[j] - o[j])
        leg = abs(c[j] - c[max(lo, j - 3)])
        if max(body, leg) < disp * float(atr_s.iloc[j]):
            continue
        for k in range(j - 1, lo - 1, -1):
            if ev.direction == "up" and c[k] < o[k]:
                zones.append(Zone("OB", "bull", float(h[k]), float(l[k]), k))
                break
            if ev.direction == "down" and c[k] > o[k]:
                zones.append(Zone("OB", "bear", float(h[k]), float(l[k]), k))
                break
    for z in zones:
        after = slice(z.idx + 4, len(df))
        if z.direction == "bull" and (l[after] < z.bottom).any():
            z.mitigated = True
        if z.direction == "bear" and (h[after] > z.top).any():
            z.mitigated = True
    return zones


def liquidity_pools(swings: list[Swing], last_atr: float, tol: float = 0.1, upto: int | None = None):
    """Equal highs / equal lows: 2+ confirmed swings within tol*ATR."""
    pools = []
    for kind in ("H", "L"):
        pts = [s for s in swings if s.kind == kind and (upto is None or s.confirm_idx <= upto)][-20:]
        used = set()
        for i, s in enumerate(pts):
            if i in used:
                continue
            group = [s] + [t for j, t in enumerate(pts[i + 1 :], i + 1) if abs(t.price - s.price) <= tol * last_atr]
            if len(group) >= 2:
                used.update(pts.index(g) for g in group)
                pools.append(
                    {"side": "buy" if kind == "H" else "sell",
                     "price": round(sum(g.price for g in group) / len(group), 5),
                     "touches": len(group)}
                )
    return pools


def recent_sweep(df: pd.DataFrame, swings: list[Swing], bars: int = 3):
    """Wick through a confirmed swing level then close back inside, within the last `bars` candles."""
    n = len(df)
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    for i in range(n - 1, max(n - 1 - bars, 0), -1):
        known = [s for s in swings if s.confirm_idx < i][-8:]
        for s in reversed(known):
            if s.kind == "L" and l[i] < s.price and c[i] > s.price:
                return {"direction": "bull", "level": s.price, "idx": i}
            if s.kind == "H" and h[i] > s.price and c[i] < s.price:
                return {"direction": "bear", "level": s.price, "idx": i}
    return None


def analyze(df: pd.DataFrame, swing_n: int = 3) -> dict:
    """Full SMC snapshot for one timeframe (uses only closed candles)."""
    a = atr_fn(df)
    swings = find_swings(df, swing_n)
    trend, events = market_structure(df, swings, a)
    fvg = [z for z in fair_value_gaps(df, a) if not z.mitigated][-5:]
    obs = [z for z in order_blocks(df, events, a) if not z.mitigated][-5:]
    last_atr = float(a.iloc[-1])
    price = float(df["close"].iloc[-1])
    known = [s for s in swings if s.confirm_idx < len(df)]
    hs = [s for s in known if s.kind == "H"][-1:] or None
    ls = [s for s in known if s.kind == "L"][-1:] or None
    zone = None
    if hs and ls and hs[0].price > ls[0].price:
        eq = (hs[0].price + ls[0].price) / 2
        zone = {"high": hs[0].price, "low": ls[0].price, "equilibrium": eq,
                "position": "premium" if price > eq else "discount"}
    return {
        "trend": trend,
        "price": price,
        "atr": last_atr,
        "last_event": asdict(events[-1]) if events else None,
        "events": [asdict(e) for e in events[-6:]],
        "swings": [asdict(s) for s in known[-8:]],
        "fvg": [asdict(z) for z in fvg],
        "order_blocks": [asdict(z) for z in obs],
        "liquidity": liquidity_pools(known, last_atr),
        "sweep": recent_sweep(df, known),
        "range": zone,
    }
