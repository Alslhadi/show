"""ExnessBroker against a mock server that follows trading.public.yaml shapes."""
import json
import time
from datetime import datetime, timezone

import httpx
import pytest

from app.config import settings

KEY = ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
       "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")


class FakeExness:
    def __init__(self):
        self.positions, self.orders, self.ops, self.calls = [], [], {}, []
        self.pid = 1000

    def iso(self, t):
        return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()

    def handler(self, req: httpx.Request) -> httpx.Response:
        for h in ("EXN-API-KEY", "EXN-TIMESTAMP", "EXN-SIGN-VERSION", "EXN-DATA", "EXN-SIGN", "EXN-IDEMPOTENCY-KEY"):
            assert h in req.headers, h
        p, m = req.url.path, req.method
        self.calls.append((m, str(req.url)))
        body = json.loads(req.content) if req.content else None
        if p == "/v1/trading/access-point":
            assert req.url.host == "gateway.test"
            return httpx.Response(200, json={"access_point": "https://ap.test", "account_id": "777"})
        assert req.url.host == "ap.test"
        if p.endswith("/limits"):
            return httpx.Response(200, json={"limits": {"rest": {"global_account_rate": {"limit": 600, "window_seconds": 60},
                                  "methods": [{"api_operation_id": "getCandleHistory", "http_method": "GET",
                                               "path": "/v1/market-data/accounts/{account_id}/candles",
                                               "rate_limit": {"limit": 100, "window_seconds": 60}}]}}})
        if p.endswith("/account"):
            return httpx.Response(200, json={"account_id": "777", "account_name": "Demo Standard", "create_date": "2026-01-01T00:00:00Z",
                                             "settings": {"currency": "USD", "leverage": "1:500", "trade_mode": "enabled",
                                                          "account_status": "active", "execution_mode": "market"}})
        if p.endswith("/conditions"):
            return httpx.Response(200, json={"instrument": "XAUUSD", "point_digits": 3, "contract_size": "100",
                                             "quote_currency": "USD", "volume_min": "0.01", "volume_max": "200", "volume_step": "0.01"})
        if p.endswith("/snapshot"):
            return httpx.Response(200, json={"account_state": {"balance": "100.00", "equity": "99.50", "used_margin": "4.00"},
                                             "positions": self.positions, "orders": self.orders})
        if p.endswith("/candles"):
            q = req.url.params
            assert q["from"] and int(q["count"]) < 0
            n = abs(int(q["count"]))
            now = time.time() // 60 * 60
            cs = [{"time": self.iso(now - 60 * (n - i)), "open": 2000, "high": 2001, "low": 1999, "close": 2000.5, "volume": 10}
                  for i in range(n)]
            if q.get("price_type") == "both":
                cs = [{"time": c["time"], "bid": {k: c[k] for k in ("open", "high", "low", "close")},
                       "ask": {k: c[k] + 0.2 for k in ("open", "high", "low", "close")}} for c in cs]
            return httpx.Response(200, json={"candles": cs})
        if p.endswith("/positions") and m == "POST":
            assert set(body) >= {"instrument", "side", "volume"} and body["side"] in ("buy", "sell")
            assert isinstance(body["volume"], str) and isinstance(body["stop_loss_price"], str)
            assert len(body["stop_loss_price"].split(".")[1]) <= 3  # point_digits respected
            self.pid += 1
            self.positions.append({"position_id": str(self.pid), "direction": body["side"], "instrument": body["instrument"],
                                   "state": "open", "volume": body["volume"], "open_price": "2000.200",
                                   "create_time": self.iso(time.time())})
            self.orders.append({"order_id": "9" + str(self.pid), "order_type": "sltp", "state": "placed",
                                "position_id": str(self.pid), "create_time": self.iso(time.time()),
                                "stop_loss_price": body["stop_loss_price"], "take_profit_price": body["take_profit_price"]})
            self.ops["1"] = "confirmed"
            return httpx.Response(202, json={"operation_id": "1", "client_request_id": req.headers["EXN-IDEMPOTENCY-KEY"],
                                             "status": "accepted"})
        if "/operations/" in p:
            op = p.rsplit("/", 1)[1]
            return httpx.Response(200, json={"operation_id": op, "status": self.ops.get(op, "pending"),
                                             "created_at": "x", "updated_at": "x"})
        if "/orders/" in p and m == "PATCH":
            for o in self.orders:
                if o["order_id"] == p.rsplit("/", 1)[1]:
                    o.update({k: v for k, v in body.items()})
            self.ops["2"] = "confirmed"
            return httpx.Response(202, json={"operation_id": "2", "status": "accepted"})
        if "/positions/" in p and m == "DELETE":
            pid = p.rsplit("/", 1)[1]
            self.positions = [x for x in self.positions if x["position_id"] != pid]
            self.ops["3"] = "confirmed"
            return httpx.Response(202, json={"operation_id": "3", "status": "accepted"})
        return httpx.Response(404, json={"code": "REQUEST_NOT_FOUND", "error_message": p})


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.setattr(settings, "exn_api_key", "exnsk-test")
    monkeypatch.setattr(settings, "exn_private_key", KEY)
    monkeypatch.setattr(settings, "exn_account_id", "777")
    monkeypatch.setattr(settings, "exn_discovery_base_url", "https://gateway.test")
    monkeypatch.setattr(settings, "symbols", "XAUUSD")
    from app.brokers.exness_broker import ExnessBroker
    monkeypatch.setattr(ExnessBroker, "_start_ticks", lambda self: None)
    fake = FakeExness()
    b = ExnessBroker()
    b.c.http = httpx.Client(transport=httpx.MockTransport(fake.handler), follow_redirects=False)
    assert b.connect(), b.last_error
    return b, fake


def test_connect_account_and_demo(broker):
    b, _ = broker
    assert b.is_demo()
    a = b.account()
    assert a.balance == 100.0 and a.equity == 99.5 and a.margin_free == 95.5


def test_spec_from_conditions(broker):
    b, _ = broker
    s = b.spec("XAUUSD")
    assert s.digits == 3 and s.tick_size == 0.001 and abs(s.tick_value - 0.1) < 1e-12 and s.volume_step == 0.01


def test_candles_closed_only_and_tick_fallback(broker):
    b, _ = broker
    df = b.candles("XAUUSD", "M1", 50)
    assert len(df) == 50 and (df["time"] + 60 <= time.time()).all()
    t = b.tick("XAUUSD")
    assert t["ask"] > t["bid"]


def test_open_confirmed_with_sl_tp_then_modify_and_close(broker):
    b, fake = broker
    r = b.market_order("XAUUSD", "BUY", 0.01, 1995.12345, 2010.6789, "AUTO", "abc123")
    assert r["ok"] and r["ticket"] == "1001"
    pos = b.positions()[0]
    assert pos["sl"] == 1995.123 and pos["volume"] == 0.01
    assert b.market_order("XAUUSD", "BUY", 0.01, 1995, 2010, "AUTO", "abc123").get("duplicate")
    assert b.modify("1001", 2000.2, None)["ok"]
    assert b.positions()[0]["sl"] == 2000.2
    assert b.close("1001")["ok"] and not b.positions()


def test_rejected_operation_is_not_success(broker):
    b, fake = broker
    orig = fake.handler

    def rejecting(req):
        if "/operations/" in req.url.path:
            return httpx.Response(200, json={"operation_id": "1", "status": "rejected", "created_at": "x", "updated_at": "x",
                                             "error_message": "TRADING_RULE_INSUFFICIENT_MARGIN"})
        return orig(req)
    b.c.http = httpx.Client(transport=httpx.MockTransport(rejecting))
    r = b.market_order("XAUUSD", "SELL", 0.01, 2010, 1990, "AUTO", "x1")
    assert not r["ok"] and "INSUFFICIENT_MARGIN" in r["message"]
