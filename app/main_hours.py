"""FX/gold trading week in UTC: Sunday 21:00 -> Friday 21:00 (UAE: Monday 01:00 -> Saturday 01:00)."""
from datetime import datetime, timezone


def fx_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    wd, h = now.weekday(), now.hour
    if wd == 5:
        return False
    if wd == 4 and h >= 21:
        return False
    if wd == 6 and h < 21:
        return False
    return True
