"""FastAPI app: REST + WebSocket + TradingView webhook + dashboard."""
from __future__ import annotations

import asyncio
import hmac
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from .news import news
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
        from .brokers.exness_broker import ExnessBroker
        return ExnessBroker()
    if settings.data_source.upper() == "MT5":
        from .brokers.mt5_broker import MT5Broker
        return MT5Broker()
    from .brokers.paper import PaperBroker
    return PaperBroker(settings.symbol_list, balance=settings.paper_balance)


engine = Engine(make_broker())
from .telegram_bridge import TelegramBridge  # noqa: E402
from . import storage  # noqa: E402

tg = TelegramBridge(engine)


def market_open(now=None) -> bool:
    from .main_hours import fx_market_open
    return fx_market_open(now)


async def keep_awake() -> None:
    """Render free instances sleep after 15 min without inbound traffic.
    Ping our own public URL every 10 min while the market is open; let it sleep on
    weekends to save free hours. A GitHub Action wakes it on Sunday night."""
    import os
    import httpx
    url = os.getenv("RENDER_EXTERNAL_URL")  # set automatically by Render
    if not url:
        return
    async with httpx.AsyncClient(timeout=20) as c:
        while True:
            await asyncio.sleep(600)
            if not market_open() and not settings.keep_awake_weekends:
                engine.log("info", "السوق مغلق (ويكند) — السماح للخادم بالنوم")
                return
            try:
                await c.get(f"{url}/api/health")
            except Exception as e:
                engine.log("warn", f"keep-awake ping failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await asyncio.to_thread(storage.init)
    except Exception as e:
        print("STORAGE_INIT_FAILED", e, flush=True)

    async def tg_boot():
        try:
            await tg.start()
        except Exception as e:
            tg.status = f"خطأ: {e}"
        print("TELEGRAM", tg.status, flush=True)
    tg_task = asyncio.create_task(tg_boot())
    tracker = asyncio.create_task(tg.tracker_loop())
    task = asyncio.create_task(engine.run(interval=5.0 if settings.data_source.upper() == "EXNESS" else 2.0))
    pinger = asyncio.create_task(keep_awake())
    news_task = asyncio.create_task(news.loop(engine))

    async def model_loop():
        await asyncio.sleep(20)
        while True:
            if settings.strategy_mode.upper() == "HYBRID" and engine.broker.connected():
                try:
                    r = await asyncio.to_thread(engine.train_models)
                    print("MODELS", json.dumps(r, ensure_ascii=False, default=str), flush=True)
                    if not _BT:
                        from .agent.backtest import walk_forward
                        for sym in settings.symbol_list:
                            def run(sym=sym):
                                df = engine.history_candles(sym, settings.hybrid_tf, settings.meta_train_bars)
                                t = engine.broker.tick(sym)
                                return walk_forward(df, spread=t["ask"] - t["bid"])
                            bt = await asyncio.to_thread(run)
                            bt["t"] = time.time()
                            _BT[sym] = bt
                            print("WALKFORWARD", sym, json.dumps({k: bt.get(k) for k in ("out_of_sample", "in_sample", "wfe", "unfiltered")},
                                                                    ensure_ascii=False, default=str), flush=True)
                except Exception as e:
                    print("MODELS_ERROR", e, flush=True)
            await asyncio.sleep(1800)
    models_task = asyncio.create_task(model_loop())
    if settings.exn_api_key and settings.exn_private_key and settings.data_source.upper() != "EXNESS":
        async def startup_check():
            r = await asyncio.to_thread(exness_test)
            safe = {k: r.get(k) for k in ("ok", "message", "open_positions", "active_orders", "symbols")}
            if r.get("ok"):
                a = r.get("account") or {}
                safe.update(account_id=a.get("account_id"), account_name=a.get("account_name"),
                            currency=(a.get("settings") or {}).get("currency"),
                            trade_mode=(a.get("settings") or {}).get("trade_mode"),
                            account_state=r.get("account_state"))
            print("EXNESS_STARTUP_CHECK", json.dumps(safe, ensure_ascii=False), flush=True)
            engine.log("info" if r.get("ok") else "error", "فحص اتصال Exness: " + ("ناجح" if r.get("ok") else str(r.get("message"))))
        asyncio.create_task(startup_check())
    engine.log("info", f"بدء النظام — المصدر: {engine.broker.name}، وضع التنفيذ: {engine.execution_mode}")
    yield
    task.cancel()
    pinger.cancel()
    tracker.cancel()
    tg_task.cancel()


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


@app.get("/api/exness/test", dependencies=[Depends(auth)])
def exness_test():
    """Read-only connection check: discovery + account details + snapshot. Never trades."""
    if not all([settings.exn_api_key, settings.exn_private_key, settings.exn_account_id, settings.exn_discovery_base_url]):
        return {"ok": False, "message": "أضف EXN_API_KEY و EXN_PRIVATE_KEY و EXN_ACCOUNT_ID و EXN_DISCOVERY_BASE_URL في Render"}
    try:
        from .brokers.exness_api import ExnessClient
        c = ExnessClient(settings.exn_api_key, settings.exn_private_key, settings.exn_account_id,
                         settings.exn_discovery_base_url)
        ap = c.resolve_access_point()
        details = c.account_config()
        snap = c.snapshot()
        avail = c._request("GET", f"/v1/configuration/accounts/{c.account_id}/instruments").get("instruments", [])
        syms = {}
        for sym in settings.symbol_list:
            if sym in avail:
                cnd = c._request("GET", f"/v1/configuration/accounts/{c.account_id}/instruments/{sym}/conditions")
                syms[sym] = {k: cnd.get(k) for k in ("point_digits", "contract_size", "volume_min", "volume_step",
                                                     "exec_mode", "quote_currency")}
            else:
                syms[sym] = {"missing": True, "similar": [a for a in avail if a.upper().startswith(sym[:6])][:5]}
        return {"ok": True, "symbols": syms, "access_point": ap, "account": details, "account_state": snap.get("account_state"),
                "open_positions": len(snap.get("positions", [])), "active_orders": len(snap.get("orders", []))}
    except Exception as e:
        return {"ok": False, "message": str(e)}


# ---- Telegram ------------------------------------------------------------------
@app.get("/api/tg/status", dependencies=[Depends(auth)])
async def tg_status():
    return await asyncio.to_thread(tg.snapshot)


class PhoneIn(BaseModel):
    phone: str


@app.post("/api/tg/send-code", dependencies=[Depends(auth)])
async def tg_send_code(b: PhoneIn):
    try:
        return await tg.send_code(b.phone.strip())
    except Exception as e:
        return {"ok": False, "message": str(e)}


class CodeIn(BaseModel):
    code: str | None = None
    password: str | None = None


@app.post("/api/tg/sign-in", dependencies=[Depends(auth)])
async def tg_sign_in(b: CodeIn):
    try:
        return await tg.sign_in((b.code or "").strip(), b.password)
    except Exception as e:
        return {"ok": False, "message": str(e)}


@app.post("/api/tg/logout", dependencies=[Depends(auth)])
async def tg_logout():
    await tg.logout()
    return {"ok": True}


class TgModeIn(BaseModel):
    mode: str


@app.post("/api/tg/mode", dependencies=[Depends(auth)])
def tg_mode(b: TgModeIn):
    if b.mode not in ("track", "smart", "follow"):
        raise HTTPException(400, "وضع غير معروف")
    settings.tg_mode = b.mode
    storage.kv_set("tg_mode", b.mode)
    engine.log("info", f"وضع توصيات تلجرام: {b.mode}")
    return {"ok": True}


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


@app.get("/api/agent/stats", dependencies=[Depends(auth)])
def agent_stats():
    from .agent.stats import ledger
    models = {k: {"n": m.n, "base": round(m.base, 3), "auc": round(m.auc, 3) if m.auc else None,
                  "trained": m.trained, "ready": m.ready} for k, m in engine.models.items()}
    return {"strategy": settings.strategy_mode, "bot": ledger.report("bot"), "telegram": ledger.report("telegram"),
            "models": models, "backtests": _BT}


_BT: dict = {}


@app.post("/api/agent/backtest/{symbol}", dependencies=[Depends(auth)])
async def agent_backtest(symbol: str):
    from .agent.backtest import walk_forward
    symbol = symbol.upper()
    def run():
        df = engine.history_candles(symbol, settings.hybrid_tf, settings.meta_train_bars)
        t = engine.broker.tick(symbol)
        return walk_forward(df, spread=t["ask"] - t["bid"])
    r = await asyncio.to_thread(run)
    r["t"] = time.time()
    _BT[symbol] = r
    return r


@app.post("/api/agent/train", dependencies=[Depends(auth)])
async def agent_train():
    for m in engine.models.values():
        m.trained = 0
    return await asyncio.to_thread(engine.train_models)


@app.get("/api/news", dependencies=[Depends(auth)])
def get_news():
    v = news.view()
    for e in v["events"]:
        e["blocking"] = any(engine.news_block(sym) for sym in e["symbols"]) and e["impact"] == "High"
    return v


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
