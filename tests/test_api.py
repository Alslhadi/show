import os
import time

os.environ["DASHBOARD_TOKEN"] = "t"
os.environ["WEBHOOK_SECRET"] = "s"

from fastapi.testclient import TestClient

from app.main import app, engine
from app.risk import risk_engine

H = {"X-Token": "t"}


def test_auth_required():
    with TestClient(app) as c:
        assert c.get("/api/snapshot").status_code == 401
        assert c.get("/api/snapshot", headers=H).status_code == 200


def test_snapshot_marks_simulation_and_demo():
    with TestClient(app) as c:
        s = c.get("/api/snapshot", headers=H).json()
        assert s["simulation"] is True and s["trading_mode"] == "DEMO"


def test_live_cannot_be_enabled_by_default():
    with TestClient(app) as c:
        r = c.post("/api/live/enable", headers=H, json={"confirm_phrase": "أوافق على التداول بأموال حقيقية"})
        assert r.status_code == 403


def test_webhook_secret_and_duplicates():
    with TestClient(app) as c:
        bad = c.post("/api/webhook/tradingview", json={"secret": "x", "symbol": "XAUUSD", "action": "BUY"})
        assert bad.status_code == 401
        body = {"secret": "s", "symbol": "XAUUSD", "action": "BUY", "id": "a1", "time": time.time()}
        assert c.post("/api/webhook/tradingview", json=body).json()["ok"]
        assert c.post("/api/webhook/tradingview", json=body).json().get("duplicate")
        old = {**body, "id": "a2", "time": time.time() - 999}
        assert c.post("/api/webhook/tradingview", json=old).status_code == 400


def test_kill_switch_blocks_execution():
    with TestClient(app) as c:
        c.post("/api/killswitch", headers=H, json={"on": True})
        ok, why = engine.trading_permitted()
        assert not ok and "KILL" in why
        c.post("/api/killswitch", headers=H, json={"on": False})
        assert not risk_engine.state.kill_switch
