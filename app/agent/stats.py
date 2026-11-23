"""Live/paper performance ledger for the 5-gate rubric (Gate 3 + Gate 5). Every bot/Telegram order
records its initial risk; on close we compute R = profit / initial risk. Persisted in the kv store."""
from __future__ import annotations

import json
import time

import numpy as np

from .. import storage


class Ledger:
    KEY = "ledger_v1"

    def __init__(self):
        self.open: dict[str, dict] = {}
        self.closed: list[dict] = []
        self._loaded = False

    def load(self):
        if self._loaded:
            return
        try:
            raw = storage.kv_get(self.KEY)
            if raw:
                j = json.loads(raw)
                self.open, self.closed = j.get("open", {}), j.get("closed", [])
        except Exception:
            pass
        self._loaded = True

    def save(self):
        try:
            storage.kv_set(self.KEY, json.dumps({"open": self.open, "closed": self.closed[-2000:]}))
        except Exception:
            pass

    def on_open(self, ticket, symbol, source, strategy, risk_money, meta_p=None):
        self.load()
        self.open[str(ticket)] = {"symbol": symbol, "source": source, "strategy": strategy,
                                  "risk": float(risk_money or 0), "p": meta_p, "t": time.time()}
        self.save()

    def on_close(self, ticket, profit: float):
        self.load()
        o = self.open.pop(str(ticket), None)
        if o is None:
            return
        o.update(profit=float(profit), r=(float(profit) / o["risk"]) if o["risk"] else None, closed=time.time())
        self.closed.append(o)
        self.save()

    def report(self, group: str | None = None) -> dict:
        self.load()
        rows = [c for c in self.closed if group is None or (c["source"].startswith("TG") == (group == "telegram"))]
        P = np.array([c["profit"] for c in rows], dtype=float)
        Rs = np.array([c["r"] for c in rows if c.get("r") is not None], dtype=float)
        if not len(P):
            return {"trades": 0, "gates": []}
        gp, gl = P[P > 0].sum(), -P[P < 0].sum()
        eq = np.cumsum(P)
        peak = np.maximum.accumulate(np.concatenate([[0], eq]))
        dd = float((peak - np.concatenate([[0], eq])).max())
        pf = round(float(gp / gl), 2) if gl else None
        exr = round(float(Rs.mean()), 3) if len(Rs) else None
        sharpe = round(float(Rs.mean() / Rs.std()), 3) if len(Rs) > 1 and Rs.std() else None
        out = {"trades": int(len(P)), "win_rate": round(float((P > 0).mean()), 3), "net": round(float(P.sum()), 2),
               "profit_factor": pf, "expectancy_r": exr, "max_dd_usd": round(dd, 2), "sharpe_trade": sharpe}
        out["gates"] = [
            {"gate": "≥ 100 صفقة مغلقة", "ok": len(P) >= 100, "value": int(len(P))},
            {"gate": "Profit Factor ≥ 1.40", "ok": bool(pf and pf >= 1.4), "value": pf},
            {"gate": "Expectancy > +0.15R", "ok": bool(exr is not None and exr > 0.15), "value": exr},
        ]
        return out


ledger = Ledger()
