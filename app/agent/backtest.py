"""Walk-forward evaluation (Gate 2/3 of the 5-gate rubric) on the broker's own historical candles.
Rolling IS:OOS = 3:1 over 5 windows; the meta-model is re-fit on each IS slice only, then scored on the
following OOS slice (with a purge gap of `horizon` bars). Costs: spread charged in R on every trade."""
from __future__ import annotations

import numpy as np

from ..config import settings
from . import features as F
from . import meta as M


def _metrics(R: list[float]) -> dict:
    R = np.array(R, dtype=float)
    if not len(R):
        return {"trades": 0}
    gp, gl = R[R > 0].sum(), -R[R < 0].sum()
    eq = np.cumsum(R)
    dd = float((np.maximum.accumulate(np.concatenate([[0], eq])) - np.concatenate([[0], eq])).max())
    return {"trades": int(len(R)), "win_rate": round(float((R > 0).mean()), 3),
            "profit_factor": round(float(gp / gl), 2) if gl else None, "expectancy_r": round(float(R.mean()), 3),
            "total_r": round(float(R.sum()), 2), "max_dd_r": round(dd, 2),
            "sharpe_trade": round(float(R.mean() / R.std()), 3) if R.std() else None}


def walk_forward(df, spread: float = 0.0, windows: int = 5) -> dict:
    d = F.build(df)
    H = settings.hybrid_horizon_bars
    all_ev = [i for i in F.events(d) if i < len(d) - H]
    if len(all_ev) < 60:
        return {"ok": False, "message": f"الأحداث قليلة ({len(all_ev)}) — يحتاج بيانات أطول"}
    X, y, R = M.dataset(d, all_ev)
    cost = np.array([spread / (settings.hybrid_sl_atr * float(d["atr"].iloc[i]) + spread) for i in all_ev])
    R = R - cost
    ev = np.array(all_ev)
    lo, hi = ev.min(), ev.max()
    step = (hi - lo) / (windows + 3)
    oos_R, is_R, rows = [], [], []
    for k in range(windows):
        is_a, is_b = lo + k * step, lo + (k + 3) * step
        oos_a, oos_b = is_b + H, is_b + step
        tr = (ev >= is_a) & (ev < is_b - H)
        te = (ev >= oos_a) & (ev < oos_b)
        if tr.sum() < 30 or te.sum() < 3:
            continue
        m = M.fit(X[tr], y[tr])
        p_tr = np.array([m.predict(x) for x in X[tr]])
        p_te = np.array([m.predict(x) for x in X[te]])
        take_tr = p_tr >= settings.meta_p_min
        take_te = p_te >= settings.meta_p_min
        r_is, r_oos = R[tr][take_tr], R[te][take_te]
        is_R += r_is.tolist()
        oos_R += r_oos.tolist()
        rows.append({"window": k + 1, "is_trades": int(take_tr.sum()), "oos_trades": int(take_te.sum()),
                     "is_exp_r": round(float(r_is.mean()), 3) if len(r_is) else None,
                     "oos_exp_r": round(float(r_oos.mean()), 3) if len(r_oos) else None})
    # per entry type, on the most recent 40% of events (out-of-sample-ish for the raw rules: no fitting involved)
    types = np.array([F.sig_type(d, i) for i in all_ev])
    recent = ev >= lo + 0.6 * (hi - lo)
    by_type = {t: _metrics(R[(types == t) & recent].tolist()) for t in ("tl", "sweep", "trend", "range")}
    is_m, oos_m = _metrics(is_R), _metrics(oos_R)
    raw = _metrics((R).tolist())
    wfe = None
    if is_m.get("trades") and oos_m.get("trades") and is_m["expectancy_r"] > 0:
        # per-bar return ratio (OOS slices are 1/3 the IS length)
        wfe = round((oos_m["total_r"] / 1) / (is_m["total_r"] / 3), 2)
    return {"ok": True, "bars": len(d), "events": len(all_ev), "unfiltered": raw, "in_sample": is_m,
            "out_of_sample": oos_m, "wfe": wfe, "windows": rows, "by_type": by_type,
            "verdict": verdict(oos_m, wfe)}


def verdict(m: dict, wfe) -> list[dict]:
    pf, ex, n, dd = m.get("profit_factor"), m.get("expectancy_r"), m.get("trades", 0), m.get("max_dd_r")
    return [
        {"gate": "WFE ≥ 0.50", "ok": wfe is not None and wfe >= 0.5, "value": wfe},
        {"gate": "Profit Factor ≥ 1.40", "ok": bool(pf and pf >= 1.4), "value": pf},
        {"gate": "Expectancy > +0.15R", "ok": bool(ex is not None and ex > 0.15), "value": ex},
        {"gate": "≥ 100 صفقة", "ok": n >= 100, "value": n},
        {"gate": "أقصى تراجع (بوحدات R)", "ok": dd is not None and dd < 10, "value": dd},
    ]
