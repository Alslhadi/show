"""Tiny Postgres persistence (Neon). Falls back to in-memory when DATABASE_URL is not set (tests/local)."""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

_lock = threading.Lock()
_mem_kv: dict[str, str] = {}
_mem_signals: list[dict] = []

SIGNAL_COLS = ["id", "channel", "msg_id", "received_at", "text", "kind", "symbol", "side", "entry", "entry_lo",
               "entry_hi", "sl", "sl_source", "tp", "status", "outcome", "r_multiple", "closed_at", "executed",
               "ticket", "note"]


def _url() -> str | None:
    return os.getenv("DATABASE_URL")


def _conn():
    import psycopg
    return psycopg.connect(_url(), autocommit=True, connect_timeout=10)


def init() -> None:
    if not _url():
        return
    with _conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS kv (k text PRIMARY KEY, v text NOT NULL, updated_at timestamptz DEFAULT now())")
        c.execute("""CREATE TABLE IF NOT EXISTS tg_signals (id bigserial PRIMARY KEY, channel text NOT NULL,
            msg_id bigint NOT NULL, received_at timestamptz DEFAULT now(), text text, kind text, symbol text, side text,
            entry double precision, entry_lo double precision, entry_hi double precision, sl double precision,
            sl_source text, tp double precision, status text DEFAULT 'open', outcome text, r_multiple double precision,
            closed_at timestamptz, executed boolean DEFAULT false, ticket text, note text, UNIQUE(channel, msg_id))""")


def kv_get(k: str) -> str | None:
    if not _url():
        return _mem_kv.get(k)
    with _conn() as c:
        r = c.execute("SELECT v FROM kv WHERE k=%s", (k,)).fetchone()
        return r[0] if r else None


def kv_set(k: str, v: str) -> None:
    if not _url():
        _mem_kv[k] = v
        return
    with _conn() as c:
        c.execute("INSERT INTO kv(k,v) VALUES(%s,%s) ON CONFLICT(k) DO UPDATE SET v=EXCLUDED.v, updated_at=now()", (k, v))


def kv_del(k: str) -> None:
    if not _url():
        _mem_kv.pop(k, None)
        return
    with _conn() as c:
        c.execute("DELETE FROM kv WHERE k=%s", (k,))


def insert_signal(row: dict) -> dict | None:
    """Returns the stored row, or None if this (channel, msg_id) was already stored."""
    cols = [k for k in row if k in SIGNAL_COLS and k != "id"]
    if not _url():
        with _lock:
            if any(s["channel"] == row["channel"] and s["msg_id"] == row["msg_id"] for s in _mem_signals):
                return None
            r = {c: None for c in SIGNAL_COLS}
            r.update(row, id=len(_mem_signals) + 1, received_at=datetime.now(timezone.utc),
                     status=row.get("status", "open"), executed=row.get("executed", False))
            _mem_signals.append(r)
            return dict(r)
    with _conn() as c:
        q = (f"INSERT INTO tg_signals({','.join(cols)}) VALUES({','.join(['%s'] * len(cols))}) "
             f"ON CONFLICT(channel,msg_id) DO NOTHING RETURNING {','.join(SIGNAL_COLS)}")
        r = c.execute(q, [row[k] for k in cols]).fetchone()
        return dict(zip(SIGNAL_COLS, r)) if r else None


def update_signal(sid: int, **fields) -> None:
    if not fields:
        return
    if not _url():
        with _lock:
            for s in _mem_signals:
                if s["id"] == sid:
                    s.update(fields)
        return
    with _conn() as c:
        sets = ",".join(f"{k}=%s" for k in fields)
        c.execute(f"UPDATE tg_signals SET {sets} WHERE id=%s", [*fields.values(), sid])


def list_signals(status: tuple[str, ...] | None = None, channel: str | None = None, limit: int = 100) -> list[dict]:
    if not _url():
        rows = [dict(s) for s in _mem_signals if (not status or s["status"] in status)
                and (not channel or s["channel"] == channel)]
        return sorted(rows, key=lambda r: r["id"], reverse=True)[:limit]
    where, args = [], []
    if status:
        where.append("status = ANY(%s)")
        args.append(list(status))
    if channel:
        where.append("channel=%s")
        args.append(channel)
    q = f"SELECT {','.join(SIGNAL_COLS)} FROM tg_signals {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY id DESC LIMIT %s"
    with _conn() as c:
        return [dict(zip(SIGNAL_COLS, r)) for r in c.execute(q, [*args, limit]).fetchall()]


def channel_stats() -> dict[str, dict]:
    rows = list_signals(limit=5000)
    out: dict[str, dict] = {}
    for r in rows:
        if r["kind"] != "OPEN" or "|Q|" in (r.get("sl_source") or ""):
            continue  # count each analyst entry once (the runner leg carries the call to its final target)
        s = out.setdefault(r["channel"], {"signals": 0, "closed": 0, "wins": 0, "losses": 0, "be": 0, "sum_r": 0.0})
        s["signals"] += 1
        if r["status"] == "closed" and r["r_multiple"] is not None:
            s["closed"] += 1
            s["sum_r"] += r["r_multiple"]
            if r["r_multiple"] > 0.05:
                s["wins"] += 1
            elif r["r_multiple"] < -0.05:
                s["losses"] += 1
            else:
                s["be"] += 1
    for s in out.values():
        decided = s["wins"] + s["losses"]
        s["win_rate"] = round(s["wins"] / decided * 100, 1) if decided else None
        s["avg_r"] = round(s["sum_r"] / s["closed"], 2) if s["closed"] else None
    return out
