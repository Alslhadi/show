"""FastAPI app: REST + WebSocket + TradingView webhook + dashboard."""
from __future__ import annotations

import asyncio
import hmac
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import settings
from .engine import MODES, Engine, audit
from .indicators import enrich
from .risk import position_size, risk_engine


def make_broker():
    if settings.data_source.upper() == "EXNESS":
        raise RuntimeError("ربط Exness للتنفيذ بانتظار الوثائق الرسمية — استخدم SIMULATION أو MT5 حالياً")
    if settings.data_source.upper() == "MT5":
        from .brokers.mt5_broker import MT5Broker
        return MT5Broker()
    from .brokers.paper import PaperBroker
    return PaperBroker(settings.symbol_list, balance=settings.paper_balance)


engine = Engine(make_broker())


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(engine.run(interval=2.0))
    engine.log("info", f"بدء النظام — المصدر: {engine.broker.name}، وضع التنفيذ: {engine.execution_mode}")
    yield
    task.cancel()


app = FastAPI(title="AI Trading Command Center", lifespan=lifespan)
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def auth(x_token: str | None = Header(default=None)) -> None:
    if not x_token or not hmac.compare_digest(x_token, settings.dashboard_token):
        raise HTTPException(401, "رمز الدخول غير صحيح")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {"ok": engine.status["loop_ok"], "connected": engine.broker.connected(), "last_loop": engine.status["last_loop"]}


@app.get("/api/snapshot", dependencies=[Depends(auth)])
def snapshot():
    return engine.snapshot()


@app.get("/api/candles/{symbol}/{tf}", dependencies=[Depends(auth)])
def candles(symbol: str, tf: str, n: int = 200):
    df = enrich(engine.broker.candles(symbol.upper(), tf.upper(), max(n, 220))).iloc[-n:]
    return {"symbol": symbol, "tf": tf, "simulation": engine.broker.is_simulation,
            "candles": df[["time", "open", "high", "low", "close", "ema20", "ema50"]].round(6).to_dict("records")}


@app.get("/api/analysis/{symbol}", dependencies=[Depends(auth)])
def analysis(symbol: str):
    return engine.analyze_symbol(symbol.upper())


class ModeIn(BaseModel):
    execution_mode: str


@app.post("/api/execution-mode", dependencies=[Depends(auth)])
def set_mode(m: ModeIn):
    if m.execution_mode not in MODES:
        raise HTTPException(400, "وضع غير معروف")
    engine.execution_mode = m.execution_mode
    engine.log("info", f"تغيير وضع التنفيذ إلى {m.execution_mode}")
    return {"ok": True}


class LiveIn(BaseModel):
    confirm_phrase: str


@app.post("/api/live/enable", dependencies=[Depends(auth)])
def enable_live(body: LiveIn):
    if not settings.live_trading_enabled:
        raise HTTPException(403, "التداول الحي معطّل من ملف الإعدادات (LIVE_TRADING_ENABLED=false)")
    if body.confirm_phrase.strip() != "أوافق على التداول بأموال حقيقية":
        raise HTTPException(400, "عبارة التأكيد غير مطابقة")
    engine.live_confirmed = True
    engine.log("warn", "تم تفعيل التداول الحي بتأكيد المستخدم")
    return {"ok": True}


@app.post("/api/live/disable", dependencies=[Depends(auth)])
def disable_live():
    engine.live_confirmed = False
    engine.log("info", "تم إيقاف التداول الحي")
    return {"ok": True}


class KillIn(BaseModel):
    on: bool
    close_all: bool = False
    reason: str = "يدوي من اللوحة"


@app.post("/api/killswitch", dependencies=[Depends(auth)])
def killswitch(k: KillIn):
    if k.on:
        risk_engine.kill(k.reason)
        closed = []
        if k.close_all:
            for p in engine.broker.positions():
                closed.append(engine.broker.close(p["ticket"], reason="kill switch"))
        engine.log("warn", "تفعيل KILL SWITCH", close_all=k.close_all, closed=len(closed))
        return {"ok": True, "closed": closed}
    risk_engine.release()
    engine.log("info", "إلغاء KILL SWITCH")
    return {"ok": True}


@app.post("/api/signals/{card_id}/approve", dependencies=[Depends(auth)])
def approve(card_id: str):
    card = engine.pending.pop(card_id, None)
    if not card:
        raise HTTPException(404, "الفرصة غير موجودة أو انتهت")
    if time.time() - card["created"] > 300:
        raise HTTPException(410, "انتهت صلاحية الفرصة (أكثر من 5 دقائق) — أعد التحليل")
    return engine.execute(card, "SEMI_AUTO approved")


@app.post("/api/signals/{card_id}/reject", dependencies=[Depends(auth)])
def reject(card_id: str):
    engine.pending.pop(card_id, None)
    return {"ok": True}


class CloseIn(BaseModel):
    lots: float | None = None


@app.post("/api/positions/{ticket}/close", dependencies=[Depends(auth)])
def close_pos(ticket: int, body: CloseIn):
    res = engine.broker.close(ticket, body.lots)
    audit("close", ticket=ticket, result=res)
    return res


class LotIn(BaseModel):
    symbol: str
    entry: float
    sl: float
    risk_pct: float | None = None


@app.post("/api/lot-size", dependencies=[Depends(auth)])
def lot_size(b: LotIn):
    acc = engine.broker.account()
    return position_size(acc.equity, b.risk_pct or settings.risk_per_trade_pct, b.entry, b.sl,
                         engine.broker.spec(b.symbol.upper()))


class NewsIn(BaseModel):
    name: str
    start: float
    end: float
    symbols: list[str] = []


@app.post("/api/news-window", dependencies=[Depends(auth)])
def add_news(n: NewsIn):
    engine.news_windows.append(n.model_dump())
    return {"ok": True, "count": len(engine.news_windows)}


# ---- TradingView webhook ------------------------------------------------------
_seen_alerts: dict[str, float] = {}


def _process_alert(payload: dict) -> None:
    sym, action = payload["symbol"].upper().replace(settings.symbol_suffix.upper(), ""), payload["action"].upper()
    card = engine.analyze_symbol(sym)
    card["source"] = "TradingView"
    if card["grade"] == "NO_TRADE" or card.get("direction") != action:
        engine.log("info", f"إشارة TradingView {action} {sym} لم تجتز التحقق", gates=card.get("gates_failed"))
        return
    engine.on_card(card)


@app.post("/api/webhook/tradingview")
async def tv_webhook(request: Request, bg: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "JSON غير صالح")
    if not hmac.compare_digest(str(payload.get("secret", "")), settings.webhook_secret):
        audit("webhook_rejected", reason="secret", ip=request.client.host if request.client else None)
        raise HTTPException(401, "unauthorized")
    alert_id = str(payload.get("id") or f"{payload.get('symbol')}-{payload.get('action')}-{payload.get('time')}")
    now = time.time()
    if alert_id in _seen_alerts:
        return {"ok": True, "duplicate": True}
    ts = payload.get("time")
    if ts and abs(now - float(ts)) > settings.webhook_max_age_s:
        raise HTTPException(400, "إشارة قديمة")
    if payload.get("action", "").upper() not in ("BUY", "SELL") or not payload.get("symbol"):
        raise HTTPException(400, "symbol/action مطلوبة")
    _seen_alerts[alert_id] = now
    bg.add_task(_process_alert, payload)  # respond fast: TradingView cancels after 3 s
    return {"ok": True}


@app.websocket("/ws")
async def ws(websocket: WebSocket, token: str = ""):
    if not hmac.compare_digest(token, settings.dashboard_token):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    q: asyncio.Queue = asyncio.Queue()
    engine.subscribers.add(q)
    try:
        import json
        await websocket.send_text(json.dumps(engine.snapshot(), ensure_ascii=False, default=str))
        while True:
            await websocket.send_text(await q.get())
    except WebSocketDisconnect:
        pass
    finally:
        engine.subscribers.discard(q)
