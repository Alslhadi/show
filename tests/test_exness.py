from app.brokers.exness_api import load_private_key, signed_headers

# Official test vector from exness-api.com Code Examples (RFC 8032 Test 1 key, synthetic).
RFC8032_HEX = ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
               "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")


def test_signing_matches_official_vector():
    k = load_private_key(RFC8032_HEX)
    h = signed_headers("exnsk-synthetic-example", k, "GET", "/v1/trading/access-point?account_id=123456",
                       ts_ms=1773656070123)
    assert h["EXN-SIGN"].startswith("l-H_Acia8YoTjKFMg-3fon3NOEX8TalNV")
    assert h["EXN-DATA"].startswith("eyJhcGlfa2V5IjoiZXhuc2stc3ludGhld")
    assert h["EXN-IDEMPOTENCY-KEY"] == "" and h["EXN-SIGN-VERSION"] == "1"


def test_rejects_mismatched_public_half():
    import pytest
    bad = RFC8032_HEX[:-2] + "00"
    with pytest.raises(ValueError):
        load_private_key(bad)


def test_ticks_subscription_payload():
    from app.brokers.exness_api import ticks_subscription
    assert ticks_subscription(["XAUUSD"], "ticks-xauusd-1") == {
        "id": "ticks-xauusd-1", "subscribe": {"event": "ticks", "instruments": ["XAUUSD"]}}


def test_error_hint():
    from app.brokers.exness_api import ExnessError
    assert "INVALID_VOLUME" in str(ExnessError(400, 5000, {}))
