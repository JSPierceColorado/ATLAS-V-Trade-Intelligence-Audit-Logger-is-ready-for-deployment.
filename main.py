"""ATLAS V: independent, READ-ONLY Alpaca trading account audit collector.

Commands:
    python main.py                 # continuous service (Railway: NO cron schedule)
    python main.py once            # one collection cycle
    python main.py report --days 30

Requires a persistent PostgreSQL DATABASE_URL. No trading endpoints are called.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import signal
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg
import requests
from psycopg.types.json import Jsonb

from strategy_analytics import read_from_db as read_strategy_report

LOG = logging.getLogger("atlas-v")
UTC = timezone.utc
ZERO = Decimal("0")
STOP = False


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(value, UTC)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def number(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def cfg_int(name: str, default: int, low: int, high: int) -> int:
    val = int(os.getenv(name, str(default)))
    if not low <= val <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return val


def cfg_bool(name: str, default: bool) -> bool:
    val = os.getenv(name, str(default)).strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class Config:
    db_url: str
    api_key: str
    api_secret: str
    paper: bool
    expected_account_id: str
    interval: int
    overlap_days: int
    backfill_start: datetime
    backfill_days: int
    request_pause: float
    snapshots_every: int
    history_every: int
    report_every: int
    long_report_every: int
    old_order_refresh_limit: int

    @classmethod
    def load(cls, *, require_api: bool = True) -> "Config":
        db_url = os.getenv("DATABASE_URL", "").strip()
        if not db_url:
            raise ValueError("DATABASE_URL is required: add Railway PostgreSQL and link its DATABASE_URL")
        key = os.getenv("APCA_API_KEY_ID", os.getenv("ALPACA_API_KEY", "")).strip()
        secret = os.getenv("APCA_API_SECRET_KEY", os.getenv("ALPACA_SECRET_KEY", "")).strip()
        if require_api and not (key and secret):
            raise ValueError("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY (or existing ALPACA_API_KEY/ALPACA_SECRET_KEY)")
        start = parse_time(os.getenv("BACKFILL_START_DATE", "2020-01-01T00:00:00Z"))
        if start is None or start > now_utc():
            raise ValueError("BACKFILL_START_DATE must be an ISO date in the past")
        return cls(
            db_url=db_url,
            api_key=key,
            api_secret=secret,
            paper=cfg_bool("ALPACA_PAPER", True),
            expected_account_id=os.getenv("EXPECTED_ACCOUNT_ID", "").strip(),
            interval=cfg_int("POLL_SECONDS", 120, 30, 3600),
            overlap_days=cfg_int("RECONCILE_DAYS", 14, 1, 60),
            backfill_start=start,
            backfill_days=cfg_int("BACKFILL_DAYS_PER_CYCLE", 90, 1, 365),
            request_pause=float(os.getenv("REQUEST_PAUSE_SECONDS", "0.35")),
            snapshots_every=cfg_int("SNAPSHOT_SECONDS", 600, 60, 86400),
            history_every=cfg_int("PORTFOLIO_HISTORY_SECONDS", 86400, 3600, 604800),
            report_every=cfg_int("REPORT_SECONDS", 3600, 300, 86400),
            long_report_every=cfg_int("LONG_REPORT_SECONDS", 21600, 3600, 604800),
            old_order_refresh_limit=cfg_int("OLD_ORDER_REFRESH_LIMIT", 50, 1, 500),
        )


SCHEMA = """
CREATE TABLE IF NOT EXISTS atlas_state (
  account_id TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (account_id, name)
);
CREATE TABLE IF NOT EXISTS atlas_orders (
  account_id TEXT NOT NULL, order_id TEXT NOT NULL, client_order_id TEXT,
  symbol TEXT, side TEXT, status TEXT, submitted_at TIMESTAMPTZ,
  filled_at TIMESTAMPTZ, filled_qty NUMERIC, filled_avg_price NUMERIC,
  bot TEXT NOT NULL DEFAULT 'unattributed', raw JSONB NOT NULL,
  first_seen TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, order_id)
);
CREATE INDEX IF NOT EXISTS atlas_orders_time_idx ON atlas_orders(account_id, submitted_at);
CREATE INDEX IF NOT EXISTS atlas_orders_symbol_idx ON atlas_orders(account_id, symbol, submitted_at);
CREATE TABLE IF NOT EXISTS atlas_order_observations (
  account_id TEXT NOT NULL, order_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
  status TEXT, seen_at TIMESTAMPTZ NOT NULL DEFAULT now(), raw JSONB NOT NULL,
  PRIMARY KEY (account_id, order_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS atlas_activities (
  account_id TEXT NOT NULL, activity_id TEXT NOT NULL, activity_type TEXT,
  occurred_at TIMESTAMPTZ, symbol TEXT, order_id TEXT, side TEXT,
  qty NUMERIC, price NUMERIC, net_amount NUMERIC,
  raw JSONB NOT NULL, last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, activity_id)
);
CREATE INDEX IF NOT EXISTS atlas_activities_time_idx ON atlas_activities(account_id, occurred_at);
CREATE INDEX IF NOT EXISTS atlas_activities_order_idx ON atlas_activities(account_id, order_id);
CREATE TABLE IF NOT EXISTS atlas_accounts (
  account_id TEXT NOT NULL, captured_at TIMESTAMPTZ NOT NULL,
  equity NUMERIC, cash NUMERIC, buying_power NUMERIC, portfolio_value NUMERIC,
  long_market_value NUMERIC, short_market_value NUMERIC, maintenance_margin NUMERIC,
  raw JSONB NOT NULL, PRIMARY KEY (account_id, captured_at)
);
CREATE INDEX IF NOT EXISTS atlas_accounts_time_idx ON atlas_accounts(account_id, captured_at DESC);
CREATE TABLE IF NOT EXISTS atlas_positions (
  account_id TEXT NOT NULL, captured_at TIMESTAMPTZ NOT NULL, symbol TEXT NOT NULL,
  qty NUMERIC, market_value NUMERIC, cost_basis NUMERIC, unrealized_pl NUMERIC,
  unrealized_plpc NUMERIC, current_price NUMERIC, asset_class TEXT, raw JSONB NOT NULL,
  PRIMARY KEY (account_id, captured_at, symbol)
);
CREATE TABLE IF NOT EXISTS atlas_portfolio_history (
  account_id TEXT NOT NULL, bucket_time TIMESTAMPTZ NOT NULL, timeframe TEXT NOT NULL,
  equity NUMERIC, profit_loss NUMERIC, profit_loss_pct NUMERIC, base_value NUMERIC,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, bucket_time, timeframe)
);
CREATE TABLE IF NOT EXISTS atlas_errors (
  id BIGSERIAL PRIMARY KEY, occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  account_id TEXT, stage TEXT NOT NULL, error_type TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS atlas_errors_time_idx ON atlas_errors(occurred_at DESC);
"""


def connect(cfg: Config) -> psycopg.Connection:
    # Railway internal addresses may take several seconds to resolve after a reboot.
    return psycopg.connect(cfg.db_url, autocommit=True, connect_timeout=12, application_name="atlas_v_audit")


def init_db(db: psycopg.Connection) -> None:
    with db.transaction(), db.cursor() as cur:
        cur.execute(SCHEMA)


def get_state(db: psycopg.Connection, account_id: str, key: str) -> str | None:
    with db.cursor() as cur:
        cur.execute("SELECT value FROM atlas_state WHERE account_id=%s AND name=%s", (account_id, key))
        record = cur.fetchone()
        return record[0] if record else None


def put_state(db: psycopg.Connection, account_id: str, key: str, value: str) -> None:
    with db.cursor() as cur:
        cur.execute("""INSERT INTO atlas_state(account_id,name,value) VALUES(%s,%s,%s)
                     ON CONFLICT(account_id,name) DO UPDATE SET value=EXCLUDED.value,updated_at=now()""",
                    (account_id, key, value))


def log_error(db: psycopg.Connection, account_id: str | None, stage: str, error: Exception) -> None:
    detail = str(error)
    for env_key in ("APCA_API_SECRET_KEY", "ALPACA_SECRET_KEY", "DATABASE_URL"):
        secret = os.getenv(env_key, "")
        if secret:
            detail = detail.replace(secret, "[redacted]")
    # Do not store request headers, DB URLs, or full exception tracebacks in the DB.
    with db.cursor() as cur:
        cur.execute("INSERT INTO atlas_errors(account_id,stage,error_type,detail) VALUES(%s,%s,%s,%s)",
                    (account_id, stage, type(error).__name__, detail[:500]))


class AlpacaReadOnly:
    """GET requests only; never POST, PATCH, DELETE, or PUT."""
    def __init__(self, cfg: Config):
        self.base = "https://paper-api.alpaca.markets" if cfg.paper else "https://api.alpaca.markets"
        self.s = requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": cfg.api_key,
                               "APCA-API-SECRET-KEY": cfg.api_secret,
                               "Accept": "application/json", "User-Agent": "atlas-v-readonly/1.0"})
        self.pause = cfg.request_pause

    def get(self, path: str, params: dict | None = None) -> Any:
        if not path.startswith("/v2/"):
            raise ValueError("Only Alpaca /v2/ GET endpoints are permitted")
        # Retry only non-mutating GET requests, including 429 and transient outages.
        for attempt in range(6):
            try:
                r = self.s.get(self.base + path, params=params, timeout=(8, 35))
            except requests.RequestException:
                if attempt == 5:
                    raise
                time.sleep(min(30, 2 ** attempt + random.uniform(0, 1)))
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == 5:
                    r.raise_for_status()
                retry = r.headers.get("Retry-After", "")
                try:
                    delay = min(60.0, max(1.0, float(retry)))
                except ValueError:
                    delay = min(30.0, 2 ** attempt + random.uniform(0, 1))
                time.sleep(delay)
                continue
            r.raise_for_status()
            time.sleep(max(0, self.pause))
            return r.json()
        raise RuntimeError("GET retry limit exhausted")

    def account(self) -> dict:
        result = self.get("/v2/account")
        if not isinstance(result, dict) or not result.get("id"):
            raise RuntimeError("Alpaca returned an invalid account response")
        return result

    def order(self, order_id: str) -> dict:
        from urllib.parse import quote
        data = self.get("/v2/orders/" + quote(order_id, safe=""))
        if not isinstance(data, dict) or str(data.get("id")) != order_id:
            raise RuntimeError("Alpaca returned invalid individual order")
        return data

    def positions(self) -> list[dict]:
        result = self.get("/v2/positions")
        if not isinstance(result, list):
            raise RuntimeError("Alpaca returned an invalid positions response")
        return result

    def orders_in_window(self, start: datetime, end: datetime, depth: int = 0) -> list[dict]:
        """Order API max=500: bisect windows instead of silently dropping rows.

        `after` and `until` are exclusive; adjacent windows overlap by 1 second;
        all inserts are idempotent. A single-second bucket of >=500 is an error.
        """
        if end <= start:
            return []
        data = self.get("/v2/orders", {"status": "all", "after": iso(start - timedelta(seconds=1)),
                                       "until": iso(end), "limit": 500,
                                       "direction": "asc", "nested": "false"})
        if not isinstance(data, list):
            raise RuntimeError("Alpaca returned invalid orders list")
        if len(data) < 500:
            return data
        if depth >= 28 or end - start <= timedelta(seconds=2):
            raise RuntimeError("Order history returned 500+ orders in a very small window; refusing to truncate")
        middle = start + (end - start) / 2
        left = self.orders_in_window(start, middle, depth + 1)
        right = self.orders_in_window(middle, end, depth + 1)
        return left + right

    def activities_in_window(self, start: datetime, end: datetime) -> list[dict]:
        """Broker-provided activity-id pagination: all activity types incl. fees/transfers."""
        params = {"after": iso(start - timedelta(seconds=1)), "until": iso(end),
                  "direction": "asc", "page_size": 100}
        rows: list[dict] = []
        seen_tokens: set[str] = set()
        for _ in range(10000):
            data = self.get("/v2/account/activities", params)
            if not isinstance(data, list):
                raise RuntimeError("Alpaca returned invalid activities list")
            rows.extend(data)
            if len(data) < 100:
                return rows
            token = str(data[-1].get("id", ""))
            if not token or token in seen_tokens:
                raise RuntimeError("Activity pagination has stalled; no cursor advanced")
            seen_tokens.add(token)
            params["page_token"] = token
        raise RuntimeError("Activity pagination safety cap reached; try smaller backfill windows")

    def portfolio_daily(self, historical_start: datetime) -> dict:
        # A stable full-range baseline prevents overlapping 1-year windows from
        # repeatedly rewriting old P&L points against different base values.
        # Alpaca documents 1D history with explicit start/end since account creation.
        result = self.get("/v2/account/portfolio/history",
                          {"start": iso(historical_start), "end": iso(now_utc()), "timeframe": "1D"})
        if not isinstance(result, dict):
            raise RuntimeError("Invalid portfolio history response")
        return result


def identify_bot(client_id: Any) -> str:
    """Proof-based attribution only. Never infer sellers from threshold alone."""
    value = str(client_id or "").lower()
    if value.startswith("v3trail-"):
        return "V3.1 Trailing Buyer"
    if value.startswith("v3dip-"):
        return "V3-buyer"
    if value.startswith("ab-"):
        return "Alpaca-Buyer"
    if value.startswith("v2sp100-"):
        return "Buyer (V2 S&P100)"
    if value.startswith("atlas-"):
        return "ATLAS-tagged"
    return "unattributed"


def store_orders(db: psycopg.Connection, aid: str, orders: list[dict]) -> int:
    unique: dict[str, dict] = {str(o["id"]): o for o in orders if o.get("id")}
    with db.transaction(), db.cursor() as cur:
        for o in unique.values():
            raw = json.dumps(o, sort_keys=True, separators=(",", ":"))
            fingerprint = hashlib.sha256(raw.encode()).hexdigest()
            cur.execute("""INSERT INTO atlas_orders
                (account_id,order_id,client_order_id,symbol,side,status,submitted_at,filled_at,
                 filled_qty,filled_avg_price,bot,raw) VALUES
                (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(account_id,order_id) DO UPDATE SET
                client_order_id=EXCLUDED.client_order_id,symbol=EXCLUDED.symbol,
                side=EXCLUDED.side,status=EXCLUDED.status,submitted_at=EXCLUDED.submitted_at,
                filled_at=EXCLUDED.filled_at,filled_qty=EXCLUDED.filled_qty,
                filled_avg_price=EXCLUDED.filled_avg_price,bot=EXCLUDED.bot,
                raw=EXCLUDED.raw,last_seen=now()""",
                (aid, o["id"], o.get("client_order_id"), o.get("symbol"), o.get("side"),
                 o.get("status"), parse_time(o.get("submitted_at")), parse_time(o.get("filled_at")),
                 number(o.get("filled_qty")), number(o.get("filled_avg_price")),
                 identify_bot(o.get("client_order_id")), Jsonb(o)))
            cur.execute("""INSERT INTO atlas_order_observations
                         (account_id,order_id,fingerprint,status,raw) VALUES(%s,%s,%s,%s,%s)
                         ON CONFLICT DO NOTHING""",
                        (aid, o["id"], fingerprint, o.get("status"), Jsonb(o)))
    return len(unique)


def store_activities(db: psycopg.Connection, aid: str, activities: list[dict]) -> int:
    unique = {str(a["id"]): a for a in activities if a.get("id")}
    with db.transaction(), db.cursor() as cur:
        for a in unique.values():
            dt = parse_time(a.get("transaction_time") or a.get("created_at") or a.get("date"))
            cur.execute("""INSERT INTO atlas_activities
                (account_id,activity_id,activity_type,occurred_at,symbol,order_id,side,
                qty,price,net_amount,raw) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(account_id,activity_id) DO UPDATE SET
                activity_type=EXCLUDED.activity_type,occurred_at=EXCLUDED.occurred_at,
                symbol=EXCLUDED.symbol,order_id=EXCLUDED.order_id,
                side=EXCLUDED.side,qty=EXCLUDED.qty,price=EXCLUDED.price,
                net_amount=EXCLUDED.net_amount,raw=EXCLUDED.raw,last_seen=now()""",
                (aid, a["id"], a.get("activity_type"), dt, a.get("symbol"), a.get("order_id"),
                 a.get("side"), number(a.get("qty")), number(a.get("price")),
                 number(a.get("net_amount")), Jsonb(a)))
    return len(unique)


def store_snapshot(db: psycopg.Connection, aid: str, acct: dict, positions: list[dict],
                   captured: datetime) -> None:
    with db.transaction(), db.cursor() as cur:
        cur.execute("""INSERT INTO atlas_accounts(account_id,captured_at,equity,cash,buying_power,
                    portfolio_value,long_market_value,short_market_value,maintenance_margin,raw)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (aid, captured, number(acct.get("equity")), number(acct.get("cash")),
                     number(acct.get("buying_power")), number(acct.get("portfolio_value")),
                     number(acct.get("long_market_value")), number(acct.get("short_market_value")),
                     number(acct.get("maintenance_margin")), Jsonb(acct)))
        for p in positions:
            cur.execute("""INSERT INTO atlas_positions(account_id,captured_at,symbol,qty,
                        market_value,cost_basis,unrealized_pl,unrealized_plpc,current_price,
                        asset_class,raw) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (aid, captured, p["symbol"], number(p.get("qty")),
                         number(p.get("market_value")), number(p.get("cost_basis")),
                         number(p.get("unrealized_pl")), number(p.get("unrealized_plpc")),
                         number(p.get("current_price")), p.get("asset_class"), Jsonb(p)))


def store_history(db: psycopg.Connection, aid: str, h: dict) -> int:
    times = h.get("timestamp") or []
    equities = h.get("equity") or []
    pnl = h.get("profit_loss") or []
    pct = h.get("profit_loss_pct") or []
    count = 0
    with db.transaction(), db.cursor() as cur:
        for i, ts in enumerate(times):
            dt = parse_time(ts)
            if dt is None:
                continue
            cur.execute("""INSERT INTO atlas_portfolio_history
                    (account_id,bucket_time,timeframe,equity,profit_loss,profit_loss_pct,base_value)
                    VALUES(%s,%s,'1D',%s,%s,%s,%s)
                    ON CONFLICT(account_id,bucket_time,timeframe) DO UPDATE SET
                    equity=EXCLUDED.equity,profit_loss=EXCLUDED.profit_loss,
                    profit_loss_pct=EXCLUDED.profit_loss_pct,base_value=EXCLUDED.base_value,
                    updated_at=now()""",
                    (aid, dt, number(equities[i]) if i < len(equities) else None,
                     number(pnl[i]) if i < len(pnl) else None,
                     number(pct[i]) if i < len(pct) else None, number(h.get("base_value"))))
            count += 1
    return count


def record_stage(db: psycopg.Connection, aid: str, stage: str, fn):
    try:
        return fn()
    except Exception as exc:
        LOG.exception("Collection stage %s failed; retry at next interval", stage)
        try:
            log_error(db, aid, stage, exc)
        except Exception:
            LOG.exception("Could not persist collector error")
        return None


def collect_window(db: psycopg.Connection, api: AlpacaReadOnly, aid: str,
                   start: datetime, end: datetime, label: str) -> bool:
    """Do not advance historical cursor if either list fails."""
    def work() -> tuple[int, int]:
        orders = api.orders_in_window(start, end)
        activities = api.activities_in_window(start, end)
        # Persist separately after both requests complete, so retries are harmless.
        n1 = store_orders(db, aid, orders)
        n2 = store_activities(db, aid, activities)
        LOG.info("%s %s..%s orders=%d activities=%d", label, iso(start), iso(end), n1, n2)
        return n1, n2
    return record_stage(db, aid, label, work) is not None


def cycle(db: psycopg.Connection, api: AlpacaReadOnly, cfg: Config) -> str:
    started = now_utc()
    acct = api.account()
    aid = str(acct["id"])
    if cfg.expected_account_id and cfg.expected_account_id != aid:
        raise ValueError("Account ID does not match EXPECTED_ACCOUNT_ID; collection stopped")
    LOG.info("Collection cycle account=%s mode=%s", aid, "paper" if cfg.paper else "live")

    snapshot_last = parse_time(get_state(db, aid, "last_snapshot"))
    if snapshot_last is None or (started - snapshot_last).total_seconds() >= cfg.snapshots_every:
        def snapshot():
            pos = api.positions()
            store_snapshot(db, aid, acct, pos, started)
            put_state(db, aid, "last_snapshot", iso(started))
            LOG.info("Snapshot equity=%s cash=%s positions=%s", acct.get("equity"), acct.get("cash"), len(pos))
        record_stage(db, aid, "account_snapshot", snapshot)

    recent_start = started - timedelta(days=cfg.overlap_days)
    recent_ok = collect_window(db, api, aid, recent_start, started + timedelta(seconds=1), "recent_reconciliation")
    if recent_ok:
        put_state(db, aid, "last_reconciliation_success", iso(started))

    # An old GTC order can change state weeks after submission and won't appear
    # in our rolling submission-time window. Re-query older incomplete orders.
    def refresh_open():
        with db.cursor() as cur:
            cur.execute("""SELECT order_id FROM atlas_orders
                        WHERE account_id=%s AND submitted_at < %s
                        AND status NOT IN ('filled','canceled','expired','rejected','replaced','stopped')
                        ORDER BY last_seen ASC LIMIT %s""",
                        (aid, recent_start, cfg.old_order_refresh_limit))
            ids = [r[0] for r in cur.fetchall()]
        for oid in ids:
            store_orders(db, aid, [api.order(oid)])
        if ids:
            LOG.info("Refreshed %d older still-pending orders", len(ids))
    record_stage(db, aid, "old_open_orders", refresh_open)

    # Bounded historical backfill runs alongside ongoing recent reconciliation.
    # Once it has reached the rolling overlap window, it is COMPLETE permanently:
    # the overlap reconciler covers new time as the moving boundary advances.
    if get_state(db, aid, "backfill_complete") != "true":
        cursor = parse_time(get_state(db, aid, "backfill_cursor")) or cfg.backfill_start
        target = started - timedelta(days=cfg.overlap_days)
        end = min(target, cursor + timedelta(days=cfg.backfill_days))
        success = True
        if cursor < end:
            success = collect_window(db, api, aid, cursor, end, "historical_backfill")
            if success:
                put_state(db, aid, "backfill_cursor", iso(end))
                LOG.info("Backfill cursor advanced to %s", iso(end))
        if success and end >= target and recent_ok:
            put_state(db, aid, "backfill_complete", "true")
            LOG.info("Historical backfill complete; rolling reconciliation continues")

    history_last = parse_time(get_state(db, aid, "last_portfolio_history"))
    if history_last is None or (started - history_last).total_seconds() >= cfg.history_every:
        def history():
            count = store_history(db, aid, api.portfolio_daily(cfg.backfill_start))
            put_state(db, aid, "last_portfolio_history", iso(started))
            LOG.info("Broker daily portfolio history points=%d", count)
        record_stage(db, aid, "broker_portfolio_history", history)

    put_state(db, aid, "last_cycle", iso(started))
    report_last = parse_time(get_state(db, aid, "last_report"))
    if report_last is None or (started - report_last).total_seconds() >= cfg.report_every:
        def publish_report():
            summary = read_report(db, aid, 1)
            LOG.info("ATLAS_REPORT_1D %s", json.dumps(summary, default=str, separators=(",", ":")))
            put_state(db, aid, "last_report", iso(started))
        record_stage(db, aid, "hourly_analysis", publish_report)
    # Strategy analytics runs independently: a failure cannot stop collection.
    strategy_last = parse_time(get_state(db, aid, "last_strategy_report"))
    if strategy_last is None or (started - strategy_last).total_seconds() >= cfg.report_every:
        def publish_strategy_report():
            summary = read_strategy_report(db, aid, 1)
            LOG.info("ATLAS_STRATEGY_REPORT_1D %s", json.dumps(summary, default=str, separators=(",", ":")))
            put_state(db, aid, "last_strategy_report", iso(started))
        record_stage(db, aid, "strategy_analysis", publish_strategy_report)
    long_last = parse_time(get_state(db, aid, "last_long_report"))
    if long_last is None or (started - long_last).total_seconds() >= cfg.long_report_every:
        def publish_long_reports():
            for days in (7, 30):
                summary = read_report(db, aid, days)
                LOG.info("ATLAS_REPORT_%dD %s", days, json.dumps(summary, default=str, separators=(",", ":")))
            for days in (7, 30, 90):
                summary = read_strategy_report(db, aid, days)
                LOG.info("ATLAS_STRATEGY_REPORT_%dD %s", days, json.dumps(summary, default=str, separators=(",", ":")))
            put_state(db, aid, "last_long_report", iso(started))
        record_stage(db, aid, "long_term_analysis", publish_long_reports)
    return aid


def read_report(db: psycopg.Connection, account_id: str, days: int) -> dict:
    cutoff = now_utc() - timedelta(days=days)
    with db.cursor() as cur:
        cur.execute("""SELECT count(*),count(*) FILTER (WHERE status='filled'),
                    count(*) FILTER (WHERE status IN ('canceled','expired','rejected')),
                    count(*) FILTER (WHERE filled_qty>0 AND status<>'filled'),
                    count(*) FILTER (WHERE bot='unattributed' AND lower(coalesce(client_order_id,'')) NOT LIKE 'v3trail-%')
                    FROM atlas_orders WHERE account_id=%s AND submitted_at>=%s""", (account_id, cutoff))
        total, filled, failed, partial, unattributed = cur.fetchone()
        cur.execute("""SELECT CASE WHEN lower(coalesce(client_order_id,'')) LIKE 'v3trail-%' THEN 'V3.1 Trailing Buyer' ELSE coalesce(bot,'unattributed') END,count(*),
                    count(*) FILTER (WHERE side='buy'),count(*) FILTER (WHERE side='sell')
                    FROM atlas_orders WHERE account_id=%s AND submitted_at>=%s
                    GROUP BY 1 ORDER BY count(*) DESC""", (account_id, cutoff))
        bots = [{"bot": b, "orders": n, "buys": buy, "sells": sell}
                for b,n,buy,sell in cur.fetchall()]
        cur.execute("""SELECT coalesce(raw->>'group_role','unknown'),count(*)
                    FROM atlas_orders WHERE account_id=%s AND submitted_at>=%s
                    AND raw ? 'group_role' GROUP BY 1 ORDER BY count(*) DESC""", (account_id, cutoff))
        broker_order_roles = dict(cur.fetchall())
        cur.execute("""SELECT count(*) FROM atlas_orders o
                    LEFT JOIN (SELECT order_id,sum(qty) AS total_qty FROM atlas_activities
                               WHERE account_id=%s AND activity_type='FILL' AND order_id IS NOT NULL
                               GROUP BY order_id) a ON a.order_id=o.order_id
                    WHERE o.account_id=%s AND o.submitted_at>=%s
                    AND o.status='filled' AND o.filled_qty IS NOT NULL
                    AND abs(o.filled_qty-coalesce(a.total_qty,0))>0.000001""",
                    (account_id, account_id, cutoff))
        filled_orders_with_activity_qty_mismatch = cur.fetchone()[0]
        cur.execute("""SELECT activity_type,count(*),sum(net_amount),sum(abs(net_amount))
                    FROM atlas_activities WHERE account_id=%s AND occurred_at>=%s
                    AND activity_type <> 'FILL' GROUP BY activity_type ORDER BY activity_type""",
                    (account_id, cutoff))
        nontrade = [{"type": typ, "count": n,
                     "signed_net_amount_sum": str(net) if net is not None else None,
                     "sum_absolute_net_amount": str(amount) if amount is not None else None}
                    for typ,n,net,amount in cur.fetchall()]
        cur.execute("""SELECT a.occurred_at,a.activity_id,a.symbol,a.side,a.qty,a.price,
                    a.order_id,coalesce(o.bot,'unattributed')
                    FROM atlas_activities a LEFT JOIN atlas_orders o
                    ON a.account_id=o.account_id AND a.order_id=o.order_id
                    WHERE a.account_id=%s AND a.activity_type='FILL'
                    AND a.occurred_at IS NOT NULL
                    ORDER BY a.occurred_at,a.activity_id""", (account_id,))
        fills = cur.fetchall()
        cur.execute("""SELECT captured_at,equity,cash,buying_power,portfolio_value,
                    maintenance_margin,long_market_value,short_market_value
                    FROM atlas_accounts WHERE account_id=%s ORDER BY captured_at DESC LIMIT 1""", (account_id,))
        latest = cur.fetchone()
        cur.execute("""SELECT symbol,market_value,unrealized_pl,unrealized_plpc
                    FROM atlas_positions WHERE account_id=%s AND captured_at=
                    (SELECT max(captured_at) FROM atlas_accounts WHERE account_id=%s)
                    ORDER BY abs(market_value) DESC NULLS LAST LIMIT 10""", (account_id,account_id))
        positions = cur.fetchall()
        cur.execute("""SELECT name,value FROM atlas_state WHERE account_id=%s AND name IN
                    ('backfill_cursor','backfill_complete','last_cycle','last_snapshot','last_portfolio_history','last_reconciliation_success')""",
                    (account_id,))
        state = dict(cur.fetchall())
        cur.execute("""SELECT count(*) FROM atlas_errors WHERE account_id=%s AND occurred_at>=%s""",
                    (account_id, cutoff))
        error_count = cur.fetchone()[0]
        cur.execute("""SELECT symbol, count(*),
                    count(*) FILTER (WHERE side='buy'),count(*) FILTER (WHERE side='sell')
                    FROM atlas_orders WHERE account_id=%s AND submitted_at>=%s
                    AND symbol IS NOT NULL GROUP BY symbol ORDER BY count(*) DESC LIMIT 10""",
                    (account_id, cutoff))
        busiest_symbols = [{"symbol": s, "orders": n, "buys": b, "sells": x}
                           for s,n,b,x in cur.fetchall()]
        cur.execute("""SELECT captured_at,equity FROM atlas_accounts
                    WHERE account_id=%s AND captured_at >= %s ORDER BY captured_at ASC""",
                    (account_id, cutoff))
        equity_series = cur.fetchall()
        cur.execute("""SELECT bucket_time,equity,profit_loss,profit_loss_pct FROM atlas_portfolio_history
                    WHERE account_id=%s AND bucket_time >= %s ORDER BY bucket_time DESC LIMIT 3""",
                    (account_id, cutoff))
        broker_history = cur.fetchall()

    peak = None
    max_observed_dd = ZERO
    for _t, eq in equity_series:
        if eq is None or eq <= ZERO:
            continue
        if peak is None or eq > peak:
            peak = eq
        if peak and eq < peak:
            dd = (peak-eq)/peak
            max_observed_dd = max(max_observed_dd,dd)

    # FIFO matched fills are an ESTIMATE only: gross, before fees, corporate actions,
    # pre-backfill positions, changing asset symbols, wash sales and other adjustments.
    lots: dict[str, deque] = defaultdict(deque)
    last_sell: dict[str, tuple[datetime, Decimal | None]] = {}
    invalid_symbols: set[str] = set()
    cost_unknown = 0
    gross_estimated = ZERO
    matched_sales = 0
    suspicious_stops = 0
    rebuys = []
    count_buys = count_sells = 0
    traded_notional = ZERO
    hold_seconds: list[float] = []
    for at, act_id, sym, side, qty, price, oid, bot in fills:
        if not (sym and qty and price and at and qty > ZERO and price > ZERO):
            continue
        sym = sym.upper().replace("/", "")
        side = (side or "").lower()
        # If the instrument is short/complex, don't assume long-lot accounting applies.
        if side not in ("buy", "sell"):
            invalid_symbols.add(sym)
            continue
        if side == "buy":
            if at >= cutoff:
                count_buys += 1
                traded_notional += qty * price
                prev = last_sell.get(sym)
                if prev and at - prev[0] <= timedelta(days=7):
                    rebuys.append({"symbol": sym, "gap_hours": round((at-prev[0]).total_seconds()/3600, 2),
                                   "prior_sale_gross_return_pct": float(prev[1]*100) if prev[1] is not None else None,
                                   "buyer_bot": bot})
            lots[sym].append([qty, price, at])
            continue
        if at >= cutoff:
            count_sells += 1
            traded_notional += qty * price
        remaining = qty
        cost = ZERO
        matched = ZERO
        matched_holds: list[float] = []
        while remaining > ZERO and lots[sym]:
            old_qty, old_price, opened = lots[sym][0]
            used = min(remaining, old_qty)
            cost += used * old_price
            matched += used
            matched_holds.append((at - opened).total_seconds())
            remaining -= used
            if used == old_qty:
                lots[sym].popleft()
            else:
                lots[sym][0][0] -= used
        if remaining > ZERO:
            invalid_symbols.add(sym)
            if at >= cutoff:
                cost_unknown += 1
        # Don't claim full realized P&L on sells with unmatched quantity.
        complete = matched == qty and sym not in invalid_symbols
        sale_return = ((qty*price-cost)/cost) if complete and cost > ZERO else None
        last_sell[sym] = (at, sale_return)
        if complete and at >= cutoff:
            gain = qty*price - cost
            gross_estimated += gain
            matched_sales += 1
            hold_seconds.extend(matched_holds)
            if sale_return is not None and sale_return <= Decimal("-0.045"):
                suspicious_stops += 1

    # Some corporate actions can invalidate FIFO without a short sale. Report these rather
    # than falsely ascribing broker-exact P&L to unadjusted lots.
    with db.cursor() as cur:
        cur.execute("""SELECT DISTINCT upper(replace(symbol,'/','')) FROM atlas_activities
                    WHERE account_id=%s AND symbol IS NOT NULL
                    AND activity_type IN ('SPLIT','SPIN','MA','REORG','ACATS','JNLS','OPASN','OPCA','OPEXC','OPEXP')""",(account_id,))
        corp = {r[0] for r in cur.fetchall()}
    # Note: we cannot safely subtract the already-aggregated FIFO values by symbol.
    # If any symbol has complex corporate actions, suppress gross estimate entirely.
    gross_valid = len(corp) == 0 and len(invalid_symbols) == 0 and state.get("backfill_complete") == "true"
    loss_rebuys = [r for r in rebuys if r["prior_sale_gross_return_pct"] is not None
                    and r["prior_sale_gross_return_pct"] < 0]
    result = {
        "period_days": days, "account_id": account_id, "generated_at_utc": iso(now_utc()),
        "history": state,
        "orders": {"total": total, "filled": filled, "canceled_expired_rejected": failed,
                   "partially_filled_or_in_progress": partial, "unattributed": unattributed,
                   "by_bot": bots, "broker_tagged_order_roles": broker_order_roles},
        "data_quality": {"filled_orders_with_activity_qty_mismatch": filled_orders_with_activity_qty_mismatch,
                         "history_complete": state.get("backfill_complete") == "true",
                         "last_successful_recent_reconciliation": state.get("last_reconciliation_success")},
        "fills": {"buys": count_buys, "sells": count_sells,
                  "gross_traded_notional_usd": str(traded_notional),
                  "estimated_complete_fifo_sales": matched_sales, "incomplete_sell_count": cost_unknown,
                  "fifo_gross_realized_estimate_usd": str(gross_estimated) if gross_valid else None,
                  "fifo_estimate_available": gross_valid,
                  "fifo_caveat": "Long-only matched fills; excludes fees, taxes, wash sales, stock transfers and broker corporate actions. Not official realized P&L; suppressed if incomplete history or complex events.",
                  "possible_5pct_loss_exits_not_proven_stoploss": suspicious_stops,
                  "median_matched_lot_hold_hours": round(sorted(hold_seconds)[len(hold_seconds)//2]/3600,2) if hold_seconds else None},
        "seven_day_repurchases_after_sell": {"count": len(rebuys), "after_known_loss_count": len(loss_rebuys),
                                               "examples": rebuys[-20:]},
        "latest_account": ({"captured_at": iso(latest[0]), "equity": str(latest[1]),
                            "cash": str(latest[2]), "buying_power": str(latest[3]),
                            "portfolio_value": str(latest[4]),
                            "maintenance_margin": str(latest[5]),
                            "long_market_value": str(latest[6]),
                            "short_market_value": str(latest[7])} if latest else None),
        "top_positions": [{"symbol": s, "market_value": str(v), "unrealized_pl": str(p),
                            "unrealized_plpc": str(pct),
                            "weight_pct_of_equity": round(float(v/latest[1]*100), 2)
                            if latest and latest[1] and v is not None else None}
                           for s,v,p,pct in positions],
        "broker_portfolio_history_latest": [{"time": iso(t),"equity":str(e),"profit_loss":str(p),"profit_loss_pct":str(pct)}
                                            for t,e,p,pct in broker_history],
        "non_trade_cash_activities": nontrade,
        "most_traded_symbols_by_order_count": busiest_symbols,
        "observed_equity_peak_to_trough_decline_pct": round(float(max_observed_dd*100), 2),
        "observed_equity_decline_caveat": "This is an account-equity movement, NOT a time-weighted investment drawdown; deposits/withdrawals can distort it.",
        "complex_instrument_symbols": sorted(corp | invalid_symbols),
        "collector_errors_in_period": error_count,
    }
    # ATLAS_V_INTELLIGENCE_V2: isolated report extension; collector unaffected.
    try:
        from audit_intelligence import read_intelligence
        result["intelligence_v2"] = read_intelligence(db, account_id, days, now_utc())
    except Exception as exc:
        LOG.exception("Optional read-only intelligence report failed")
        result["intelligence_v2"] = {"status": "unavailable",
                                     "reason": type(exc).__name__,
                                     "note": "Collector continues; optional report failed."}
    return result


def report(db: psycopg.Connection, days: int, selected_account: str = "") -> None:
    with db.cursor() as cur:
        cur.execute("SELECT DISTINCT account_id FROM atlas_accounts ORDER BY account_id")
        accounts = [row[0] for row in cur.fetchall()]
    if selected_account:
        if selected_account not in accounts:
            raise ValueError("No account snapshots for ACCOUNT_ID requested")
        accounts = [selected_account]
    if not accounts:
        LOG.warning("No snapshots collected yet. Run 'once' or start the service.")
    for aid in accounts:
        print(json.dumps(read_report(db, aid, days), indent=2, default=str), flush=True)
        print(json.dumps({"strategy_report": read_strategy_report(db, aid, days)},
                         indent=2, default=str), flush=True)


def stop_handler(_signum, _frame) -> None:
    global STOP
    STOP = True


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)
    args = argparse.ArgumentParser(description="ATLAS V -- read-only Alpaca audit")
    args.add_argument("command", choices=("run", "once", "report"), nargs="?", default="run")
    args.add_argument("--days", type=int, default=30)
    args.add_argument("--account-id", default="")
    opts = args.parse_args()
    cfg = Config.load(require_api=opts.command != "report")
    if opts.command == "report" and not (1 <= opts.days <= 3650):
        raise ValueError("--days must be between 1 and 3650")
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    LOG.info("ATLAS V starting: command=%s mode=%s. GET requests only; database required.",
             opts.command, "PAPER" if cfg.paper else "LIVE")
    if opts.command == "report":
        with connect(cfg) as db:
            init_db(db)
            report(db, opts.days, opts.account_id)
        return 0
    api = AlpacaReadOnly(cfg)
    if opts.command == "once":
        with connect(cfg) as db:
            init_db(db)
            cycle(db, api, cfg)
        return 0

    while not STOP:
        start = time.monotonic()
        try:
            with connect(cfg) as db:
                init_db(db)
                cycle(db, api, cfg)
        except Exception:
            LOG.exception("Collection cycle failed; next cycle will retry")
        remaining = max(1, cfg.interval - (time.monotonic() - start))
        while remaining > 0 and not STOP:
            interval = min(remaining, 1)
            time.sleep(interval)
            remaining -= interval
    LOG.info("ATLAS V shutdown cleanly")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        logging.getLogger("atlas-v").exception("Fatal ATLAS V error: %s", type(error).__name__)
        raise SystemExit(1)