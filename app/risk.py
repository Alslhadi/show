"""Risk engine: position sizing from real contract specs + hard limits + kill switch.

The risk engine has the final veto over every order, whatever its source
(AI, manual, TradingView). It never raises risk to recover losses.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from .config import settings


@dataclass
class SymbolSpec:
    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float  # account-currency value of one tick for 1.0 lot
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int = 0  # min stop distance in points
    trade_allowed: bool = True


@dataclass
class AccountState:
    balance: float
    equity: float
    margin_free: float
    currency: str = "USD"


@dataclass
class RiskState:
    kill_switch: bool = False
    kill_reason: str = ""
    day_start_equity: float | None = None
    week_start_equity: float | None = None
    peak_equity: float | None = None
    consecutive_losses: int = 0
    log: list = field(default_factory=list)


def round_volume(vol: float, spec: SymbolSpec) -> float:
    steps = math.floor(vol / spec.volume_step + 1e-9)
    v = steps * spec.volume_step
    v = min(v, spec.volume_max)
    return round(v, 8) if v >= spec.volume_min else 0.0


def position_size(equity: float, risk_pct: float, entry: float, sl: float, spec: SymbolSpec) -> dict:
    """Lots so that hitting SL loses risk_pct of equity (rounded DOWN to volume_step)."""
    risk_pct = min(risk_pct, settings.max_risk_per_trade_pct)
    dist = abs(entry - sl)
    if dist <= 0 or spec.tick_size <= 0 or spec.tick_value <= 0:
        return {"lots": 0.0, "reason": "مسافة وقف أو مواصفات غير صالحة"}
    loss_per_lot = dist / spec.tick_size * spec.tick_value
    risk_money = equity * risk_pct / 100
    raw = risk_money / loss_per_lot
    lots = round_volume(raw, spec)
    actual = lots * loss_per_lot
    reason = "" if lots > 0 else "الحجم المحسوب أقل من الحد الأدنى للوسيط — الصفقة مرفوضة بدل رفع المخاطرة"
    return {"lots": lots, "risk_money": round(actual, 2), "risk_pct_actual": round(actual / equity * 100, 3) if equity else 0,
            "loss_per_lot": round(loss_per_lot, 2), "raw_lots": raw, "reason": reason}


class RiskEngine:
    def __init__(self) -> None:
        self.state = RiskState()

    # --- kill switch -----------------------------------------------------
    def kill(self, reason: str) -> None:
        self.state.kill_switch = True
        self.state.kill_reason = reason
        self.state.log.append({"t": time.time(), "event": "KILL", "reason": reason})

    def release(self) -> None:
        self.state.kill_switch = False
        self.state.kill_reason = ""
        self.state.log.append({"t": time.time(), "event": "RELEASE"})

    # --- equity tracking ---------------------------------------------------
    def update_equity(self, acc: AccountState, new_day: bool = False, new_week: bool = False) -> None:
        s = self.state
        if s.day_start_equity is None or new_day:
            s.day_start_equity = acc.equity
        if s.week_start_equity is None or new_week:
            s.week_start_equity = acc.equity
        s.peak_equity = max(s.peak_equity or acc.equity, acc.equity)
        dd_day = (s.day_start_equity - acc.equity) / s.day_start_equity * 100
        dd_week = (s.week_start_equity - acc.equity) / s.week_start_equity * 100
        dd_peak = (s.peak_equity - acc.equity) / s.peak_equity * 100
        if dd_day >= settings.daily_max_loss_pct:
            self.kill(f"بلوغ حد الخسارة اليومي {settings.daily_max_loss_pct}%")
        elif dd_week >= settings.weekly_max_loss_pct:
            self.kill(f"بلوغ حد الخسارة الأسبوعي {settings.weekly_max_loss_pct}%")
        elif dd_peak >= settings.max_drawdown_pct:
            self.kill(f"بلوغ أقصى تراجع {settings.max_drawdown_pct}%")

    def record_result(self, profit: float) -> None:
        s = self.state
        s.consecutive_losses = s.consecutive_losses + 1 if profit < 0 else 0
        if s.consecutive_losses >= settings.max_consecutive_losses:
            self.kill(f"{s.consecutive_losses} خسائر متتالية")

    # --- pre-trade validation -------------------------------------------------
    def validate(self, *, symbol: str, direction: str, entry: float, sl: float, tp: float, lots: float,
                 spec: SymbolSpec, acc: AccountState, open_positions: list, spread: float,
                 margin_required: float | None = None) -> list[str]:
        errs: list[str] = []
        if self.state.kill_switch:
            errs.append(f"KILL SWITCH مفعّل: {self.state.kill_reason}")
        if not spec.trade_allowed:
            errs.append("التداول على الرمز غير مسموح من الوسيط")
        if lots <= 0:
            errs.append("حجم صفر")
        if lots < spec.volume_min or lots > spec.volume_max:
            errs.append("الحجم خارج حدود الوسيط")
        if abs(round(lots / spec.volume_step) * spec.volume_step - lots) > 1e-9:
            errs.append("الحجم لا يطابق خطوة الحجم")
        if direction == "BUY" and not (sl < entry < tp):
            errs.append("ترتيب SL/TP غير صحيح للشراء")
        if direction == "SELL" and not (tp < entry < sl):
            errs.append("ترتيب SL/TP غير صحيح للبيع")
        sl_dist = abs(entry - sl)
        if spec.stops_level and sl_dist < spec.stops_level * spec.point:
            errs.append("الوقف أقرب من الحد الأدنى للوسيط")
        if sl_dist and spread / sl_dist > settings.max_spread_to_sl_ratio:
            errs.append(f"السبريد {spread / sl_dist:.0%} من مسافة الوقف — مرتفع")
        if len(open_positions) >= settings.max_open_trades:
            errs.append("بلوغ الحد الأقصى للصفقات المفتوحة")
        if sum(1 for p in open_positions if p.get("symbol") == symbol) >= settings.max_trades_per_symbol:
            errs.append("بلوغ الحد الأقصى للصفقات على هذا الرمز")
        risk_money = sl_dist / spec.tick_size * spec.tick_value * lots
        if acc.equity and risk_money / acc.equity * 100 > settings.max_risk_per_trade_pct + 1e-9:
            errs.append("المخاطرة تتجاوز السقف المطلق للصفقة")
        if margin_required is not None and margin_required > acc.margin_free:
            errs.append("الهامش الحر غير كافٍ")
        return errs


risk_engine = RiskEngine()
