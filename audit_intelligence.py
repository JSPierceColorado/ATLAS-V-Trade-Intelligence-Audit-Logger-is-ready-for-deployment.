"""ATLAS V audit intelligence: independently testable, READ-ONLY analytics.

Uses only the collector's existing PostgreSQL tables and SELECT statements.
No broker credentials, order endpoints, automatic execution, or schema changes.
Estimates are explicitly separated from broker-confirmed measurements.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

UTC = timezone.utc
ZERO = Decimal('0')
FLOW_TYPES = frozenset(('CSD', 'CSW'))
AMBIGUOUS_FLOW_TYPES = frozenset(('TRANS', 'ACATC', 'ACATS', 'JNLC', 'JNL', 'OCT', 'FOPT'))
FEE_TYPES = frozenset(('FEE', 'CFEE', 'DIVFEE', 'PTC'))
DIVIDEND_TYPES = frozenset(('DIV', 'DIVCGL', 'DIVCGS', 'DIVROC', 'DIVTXEX', 'CGD'))
INTEREST_TYPES = frozenset(('INT', 'INTNRA', 'INTTW'))
# Only an allowlist of *known versioned* client-order prefixes is used.
STRATEGY_PREFIXES = (
    ('v3trail-', 'V3.1 Trailing Buyer'),
    ('v3dip-', 'V3-buyer'),
    ('v2sp100-', 'Buyer (V2 S&P100)'),
    ('ab-', 'Alpaca-Buyer'),
    ('atlas-', 'ATLAS-tagged'),
)


def decimal(value: Any) -> Decimal | None:
    if value is None or value == '':
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def strategy_from_client_id(client_order_id: Any) -> str:
    normalized = str(client_order_id or '').strip().lower()
    for prefix, strategy in STRATEGY_PREFIXES:
        if normalized.startswith(prefix):
            return strategy
    return 'unattributed'


def account_risk(account: dict, positions: list[dict]) -> dict:
    """Position-level account exposure (NOT a forecast of potential loss).

    A positive buying power does not establish a cash balance or margin debt.
    All concentrations are percentages of account equity, when positive.
    """
    equity, cash = decimal(account.get('equity')), decimal(account.get('cash'))
    buying_power = decimal(account.get('buying_power'))
    entries = []
    invalid_count = 0
    for p in positions:
        value = decimal(p.get('market_value'))
        if value is None or not p.get('symbol'):
            invalid_count += 1
            continue
        entries.append((str(p['symbol']).upper(), value))
    long_value = sum((v for _, v in entries if v > ZERO), ZERO)
    short_value = sum((-v for _, v in entries if v < ZERO), ZERO)
    gross_value = long_value + short_value
    ranked = sorted(entries, key=lambda kv: abs(kv[1]), reverse=True)
    valid = equity is not None and equity > ZERO
    def pct(amount: Decimal | None) -> float | None:
        return round(float(100 * amount / equity), 2) if valid and amount is not None else None
    single = pct(abs(ranked[0][1])) if ranked else 0.0 if valid else None
    top5 = pct(sum((abs(v) for _, v in ranked[:5]), ZERO))
    gross = pct(gross_value)
    alerts = []
    if valid and single is not None and single >= 25:
        alerts.append({'code': 'single_name_concentration', 'severity': 'warning', 'detail': 'Largest position is at least 25% of equity.'})
    if valid and top5 is not None and top5 >= 75:
        alerts.append({'code': 'top_five_concentration', 'severity': 'warning', 'detail': 'Five largest positions exceed 75% of equity.'})
    if valid and gross is not None and gross > 100:
        alerts.append({'code': 'gross_exposure_above_equity', 'severity': 'warning', 'detail': 'Gross marked exposure exceeds reported equity; review financing and short exposure.'})
    if cash is not None and cash < ZERO:
        alerts.append({'code': 'negative_cash', 'severity': 'warning', 'detail': 'Reported cash balance is negative; verify whether borrowing is in use.'})
    if short_value > ZERO:
        alerts.append({'code': 'short_exposure', 'severity': 'warning', 'detail': 'Negative-valued positions were observed.'})
    if not valid:
        alerts.append({'code': 'missing_positive_equity', 'severity': 'data_quality', 'detail': 'Percentage exposures cannot be computed.'})
    if invalid_count:
        alerts.append({'code': 'invalid_position_values', 'severity': 'data_quality', 'detail': f'{invalid_count} positions omitted for missing or invalid value.'})
    return {
        'equity_usd': str(equity) if equity is not None else None,
        'cash_usd': str(cash) if cash is not None else None,
        'buying_power_usd': str(buying_power) if buying_power is not None else None,
        'long_market_value_usd': str(long_value),
        'short_market_value_abs_usd': str(short_value),
        'gross_marked_exposure_usd': str(gross_value),
        'gross_exposure_pct_of_equity': gross,
        'cash_pct_of_equity': pct(cash),
        'largest_position_pct_of_equity': single,
        'top_five_positions_pct_of_equity': top5,
        'position_count': len(entries),
        'largest_positions': [{'symbol': s, 'market_value_usd': str(v),
                               'abs_weight_pct_of_equity': pct(abs(v))}
                              for s, v in ranked[:5]],
        'alerts': alerts,
        'caveat': 'Informational marked exposures, not investment returns. Buying power may include financing capacity; it is not proof of a margin loan.',
    }


def summarize_cash_activities(activities: list[dict], *, truncated: bool = False) -> dict:
    """Recognize only explicit CSD and CSW cash flows; fail closed on ambiguity.

    Never treat dividends, fees, or trading proceeds as external contributions.
    A classification is not a broker-certified complete cash flow ledger.
    """
    deposits = withdrawals = fees = dividends = interest = ZERO
    counts = Counter()
    ambiguities = []
    for a in activities:
        typ = str(a.get('activity_type') or '').upper()
        counts[typ] += 1
        amt = decimal(a.get('net_amount'))
        if typ in AMBIGUOUS_FLOW_TYPES:
            ambiguities.append({'type': typ, 'reason': 'Transfers or journals may represent external flows; requires review.'})
        elif typ in FLOW_TYPES:
            if amt is None or (typ == 'CSD' and amt < 0) or (typ == 'CSW' and amt > 0):
                ambiguities.append({'type': typ, 'reason': 'Missing or unexpected net_amount sign.'})
            elif typ == 'CSD':
                deposits += amt
            else:
                withdrawals += amt  # negative net_amount
        elif typ in FEE_TYPES and amt is not None:
            fees += amt
        elif typ in DIVIDEND_TYPES and amt is not None:
            dividends += amt
        elif typ in INTEREST_TYPES and amt is not None:
            interest += amt
    if truncated:
        ambiguities.append({'type': 'TRUNCATED', 'reason': 'Not all activities were retrieved.'})
    flow_complete = not ambiguities
    return {
        'activity_counts': dict(sorted(counts.items())),
        'identified_deposits_usd': str(deposits),
        'identified_withdrawals_usd': str(withdrawals),
        'identified_net_external_cash_flow_usd': str(deposits + withdrawals) if flow_complete else None,
        'observed_fees_usd': str(fees),
        'observed_dividends_usd': str(dividends),
        'observed_interest_usd': str(interest),
        'external_cash_flows_classifiable': flow_complete,
        'ambiguous_activities': ambiguities[:25],
        'caveat': 'Only explicit CSD/CSW activity types are classified as external cash flows. Transfers, journals, securities transfers and missing history require reconciliation. Fees/dividends/interest are not deposits.',
    }


def execution_quality(orders: list[dict], *, truncated: bool = False) -> dict:
    """Order execution timing and status; *not* bid/ask slippage."""
    states = Counter(str(o.get('status') or 'unknown').lower() for o in orders)
    fill_delays = []
    unattributed = 0
    versions = Counter()
    for order in orders:
        version = strategy_from_client_id(order.get('client_order_id'))
        versions[version] += 1
        unattributed += int(version == 'unattributed')
        submitted, filled = order.get('submitted_at'), order.get('filled_at')
        if isinstance(submitted, datetime) and isinstance(filled, datetime):
            seconds = (filled - submitted).total_seconds()
            if seconds >= 0:
                fill_delays.append(seconds)
    fill_delays.sort()
    percentile_90 = fill_delays[max(0, (9 * len(fill_delays) + 9)//10 - 1)] if fill_delays else None
    return {
        'order_count': len(orders),
        'status_counts': dict(sorted(states.items())),
        'recognized_strategy_versions': dict(sorted(versions.items())),
        'unattributed_order_count': unattributed,
        'observed_fill_delay_median_seconds': round(median(fill_delays), 2) if fill_delays else None,
        'observed_fill_delay_p90_seconds': round(percentile_90, 2) if percentile_90 is not None else None,
        'complete': not truncated,
        'caveat': 'Fill delay from submission to complete fill is not price slippage; no bid/ask or decision-time quote data is collected.',
    }


def reentry_surveillance(fills: list[dict], *, truncated: bool = False, start_date: datetime | None = None) -> dict:
    """Reentry events after *any* sell; no inference of original seller strategy.

    De-duplicate multiple partial fills of the same buy order to avoid overstating
    reentry counts, while looking for last sale across all bots by symbol.
    """
    last_sale = {}
    seen_buy_orders = set()
    within_day = []
    within_week = 0
    for f in sorted(fills, key=lambda r: (r.get('at') or datetime.min.replace(tzinfo=UTC), str(r.get('order_id') or ''))):
        symbol = str(f.get('symbol') or '').upper().replace('/', '')
        when = f.get('at')
        side = str(f.get('side') or '').lower()
        if not symbol or not isinstance(when, datetime) or side not in ('buy', 'sell'):
            continue
        if side == 'sell':
            last_sale[symbol] = when
            continue
        oid = f.get('order_id') or f"{symbol}:{when.isoformat()}"
        if oid in seen_buy_orders:
            continue
        seen_buy_orders.add(oid)
        sold_at = last_sale.get(symbol)
        if sold_at is None:
            continue
        elapsed = when - sold_at
        if timedelta(0) <= elapsed <= timedelta(days=7) and (start_date is None or when >= start_date):
            within_week += 1
            if elapsed <= timedelta(hours=24):
                within_day.append({'symbol': symbol, 'hours_after_sale': round(elapsed.total_seconds()/3600, 2),
                                   'buyer': str(f.get('bot') or 'unattributed'), 'buy_order_id': str(oid)})
    return {
        'repurchases_within_24h': len(within_day) if not truncated else None,
        'repurchases_within_7d': within_week if not truncated else None,
        'same_day_examples': within_day[-20:],
        'complete': not truncated,
        'caveat': 'Counts distinct buy orders after the most recent sale of the same symbol. Does not establish whether the prior sale was profitable or whether a cooldown was violated.',
    }


def period_cash_adjusted_equity_change(*, beginning: dict | None, ending: dict | None,
                                       cash: dict, history_complete: bool,
                                       cutoff: datetime, now: datetime) -> dict:
    """Conservatively calculate equity delta minus *identified* flows.

    NOT a time-weighted return; suppress whenever either endpoint is stale,
    flows are ambiguous, or historical backfill is incomplete.
    """
    reasons = []
    if not history_complete:
        reasons.append('historical_backfill_unverified')
    if not cash.get('external_cash_flows_classifiable'):
        reasons.append('ambiguous_cash_flows')
    if beginning is None or ending is None:
        reasons.append('missing_boundary_snapshot')
    if beginning is not None:
        t = beginning.get('captured_at')
        if not isinstance(t, datetime) or t > cutoff or (cutoff - t) > timedelta(hours=1):
            reasons.append('beginning_snapshot_not_near_cutoff')
    if ending is not None:
        t = ending.get('captured_at')
        if not isinstance(t, datetime) or t > now or (now - t) > timedelta(hours=1):
            reasons.append('ending_snapshot_stale')
    begin_eq = decimal(beginning.get('equity')) if beginning else None
    end_eq = decimal(ending.get('equity')) if ending else None
    netflow = decimal(cash.get('identified_net_external_cash_flow_usd'))
    if begin_eq is None or end_eq is None or netflow is None:
        reasons.append('missing_numeric_values')
    if reasons:
        return {'status': 'unavailable', 'reasons': sorted(set(reasons)), 'estimate_usd': None,
                'caveat': 'Not enough verified evidence to calculate cash-flow-adjusted equity change.'}
    value = end_eq - begin_eq - netflow
    return {'status': 'provisional', 'estimate_usd': str(value),
            'beginning_snapshot_at': iso(beginning['captured_at']),
            'ending_snapshot_at': iso(ending['captured_at']),
            'caveat': 'Equity change minus identified external cash flows. NOT broker-certified P/L, TWR, IRR, or a performance percentage; transfers of securities and timing mismatches may distort it.'}


def data_health(*, history_complete: bool, snapshot_at: datetime | None,
                reconciliation_at: datetime | None, now: datetime,
                execution: dict, activities: dict, truncated: bool,
                fill_quantity_mismatches: int = 0, collector_errors: int = 0,
                corporate_action_symbols: list[str] | None = None) -> dict:
    problems = []
    if not history_complete:
        problems.append('historical backfill not independently confirmed')
    if snapshot_at is None or now - snapshot_at > timedelta(minutes=30):
        problems.append('account snapshot older than 30 minutes or unavailable')
    if reconciliation_at is None or now - reconciliation_at > timedelta(minutes=30):
        problems.append('recent reconciliation older than 30 minutes or unavailable')
    if not activities['external_cash_flows_classifiable']:
        problems.append('ambiguous external cash flows')
    if truncated:
        problems.append('one or more query limits reached')
    if fill_quantity_mismatches:
        problems.append('filled order quantities do not reconcile with activity fills')
    if collector_errors:
        problems.append('collector errors observed in reporting period')
    if corporate_action_symbols:
        problems.append('corporate actions require cost-basis review')
    return {'status': 'needs_review' if problems else 'collection_healthy',
            'issues': problems, 'unattributed_orders': execution['unattributed_order_count'],
            'fill_quantity_mismatches': fill_quantity_mismatches,
            'collector_errors_in_period': collector_errors,
            'corporate_action_symbols': sorted(corporate_action_symbols or []),
            'profit_verification': 'not_broker_certified',
            'caveat': 'Healthy collection does NOT establish broker-certified cost basis or profit/loss.'}


def _latest_snapshot(row):
    if row is None:
        return None
    return dict(zip(('captured_at', 'equity', 'cash', 'buying_power', 'portfolio_value',
                     'long_market_value', 'short_market_value', 'maintenance_margin'), row))


def read_intelligence(db, account_id: str, days: int, now: datetime | None = None) -> dict:
    """Read ATLAS V tables without changing data; attach to existing reports.

    Uses bounded queries and fails closed rather than silently summarizing
    truncated datasets. SQL is always account-scoped and parameterized.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    LIMIT = 10000
    with db.cursor() as cur:
        cur.execute('''SELECT captured_at,equity,cash,buying_power,portfolio_value,
                       long_market_value,short_market_value,maintenance_margin
                       FROM atlas_accounts WHERE account_id=%s
                       ORDER BY captured_at DESC LIMIT 1''', (account_id,))
        ending = _latest_snapshot(cur.fetchone())
        cur.execute('''SELECT captured_at,equity,cash,buying_power,portfolio_value,
                       long_market_value,short_market_value,maintenance_margin
                       FROM atlas_accounts WHERE account_id=%s AND captured_at<=%s
                       ORDER BY captured_at DESC LIMIT 1''', (account_id, cutoff))
        beginning = _latest_snapshot(cur.fetchone())
        cur.execute('''SELECT symbol,market_value FROM atlas_positions
                       WHERE account_id=%s AND captured_at=(SELECT max(captured_at) FROM atlas_accounts
                       WHERE account_id=%s)''', (account_id, account_id))
        positions = [{'symbol': symbol, 'market_value': value} for symbol, value in cur.fetchall()]
        cur.execute('''SELECT activity_type,net_amount,occurred_at FROM atlas_activities
                       WHERE account_id=%s AND occurred_at>%s AND occurred_at<=%s
                       AND activity_type<>'FILL' ORDER BY occurred_at LIMIT %s''',
                    (account_id, cutoff, now, LIMIT + 1))
        activity_rows = cur.fetchall()
        cur.execute('''SELECT status,client_order_id,submitted_at,filled_at FROM atlas_orders
                       WHERE account_id=%s AND submitted_at>=%s AND submitted_at<=%s
                       ORDER BY submitted_at LIMIT %s''', (account_id, cutoff, now, LIMIT + 1))
        order_rows = cur.fetchall()
        cur.execute('''SELECT a.symbol,a.side,a.occurred_at,a.order_id,o.client_order_id,
                       coalesce(o.bot,'unattributed')
                       FROM atlas_activities a LEFT JOIN atlas_orders o
                       ON a.account_id=o.account_id AND a.order_id=o.order_id
                       WHERE a.account_id=%s AND a.activity_type='FILL'
                       AND a.occurred_at>=%s AND a.occurred_at<=%s
                       ORDER BY a.occurred_at,a.activity_id LIMIT %s''',
                    (account_id, cutoff-timedelta(days=7), now, LIMIT + 1))
        fill_rows = cur.fetchall()
        cur.execute('''SELECT name,value FROM atlas_state WHERE account_id=%s AND name IN
                       ('backfill_complete','last_reconciliation_success')''', (account_id,))
        state = dict(cur.fetchall())
        cur.execute('''SELECT count(*) FROM atlas_orders o
                       LEFT JOIN (SELECT order_id,sum(qty) total_qty FROM atlas_activities
                                  WHERE account_id=%s AND activity_type='FILL'
                                  AND order_id IS NOT NULL GROUP BY order_id) a
                       ON a.order_id=o.order_id WHERE o.account_id=%s
                       AND o.submitted_at>=%s AND o.status='filled'
                       AND o.filled_qty IS NOT NULL
                       AND abs(o.filled_qty-coalesce(a.total_qty,0))>0.000001''',
                    (account_id, account_id, cutoff))
        fill_mismatches = cur.fetchone()[0]
        cur.execute('''SELECT DISTINCT upper(replace(symbol,'/','')) FROM atlas_activities
                       WHERE account_id=%s AND symbol IS NOT NULL AND activity_type IN
                       ('SPLIT','SSP','SPIN','SSO','MA','REORG','ACATS','JNLS',
                        'OPASN','OPCA','OPEXC','OPEXP','SC','NC')''', (account_id,))
        corp_symbols = [row[0] for row in cur.fetchall()]
        cur.execute('''SELECT count(*) FROM atlas_errors
                       WHERE account_id=%s AND occurred_at>=%s''', (account_id, cutoff))
        collector_errors = cur.fetchone()[0]
        # Profit-estimate cash flows must cover the exact snapshot-to-snapshot
        # interval, not the requested calendar cutoff to wall-clock now.
        if beginning and ending and beginning['captured_at'] < ending['captured_at']:
            cur.execute('''SELECT activity_type,net_amount FROM atlas_activities
                           WHERE account_id=%s AND occurred_at>%s AND occurred_at<=%s
                           AND activity_type<>'FILL' ORDER BY occurred_at LIMIT %s''',
                        (account_id, beginning['captured_at'], ending['captured_at'], LIMIT + 1))
            endpoint_flow_rows = cur.fetchall()
        else:
            endpoint_flow_rows = []
    truncated = any(len(rows) > LIMIT for rows in (activity_rows, order_rows, fill_rows, endpoint_flow_rows))
    activity_rows = activity_rows[:LIMIT]
    order_rows = order_rows[:LIMIT]
    fill_rows = fill_rows[:LIMIT]
    cash = summarize_cash_activities([{'activity_type': typ, 'net_amount': amt}
                                      for typ, amt, _ in activity_rows], truncated=truncated)
    endpoint_cash = summarize_cash_activities(
        [{'activity_type': typ, 'net_amount': amt} for typ, amt in endpoint_flow_rows[:LIMIT]],
        truncated=truncated)
    orders = execution_quality([{'status': status, 'client_order_id': cid,
                                 'submitted_at': submit, 'filled_at': filled}
                                for status, cid, submit, filled in order_rows], truncated=truncated)
    fills = [{'symbol': sym, 'side': side, 'at': at, 'order_id': oid,
              'bot': strategy_from_client_id(cid) if cid else bot}
             for sym, side, at, oid, cid, bot in fill_rows]
    reentry = reentry_surveillance(fills, truncated=truncated, start_date=cutoff)
    if ending:
        risks = account_risk(ending, positions)
    else:
        risks = {'status': 'unavailable', 'reason': 'No account snapshot'}
    reconciled = state.get('last_reconciliation_success')
    try:
        reconcile_at = datetime.fromisoformat(str(reconciled).replace('Z', '+00:00')) if reconciled else None
    except ValueError:
        reconcile_at = None
    verified = state.get('backfill_complete') == 'true'
    health = data_health(history_complete=verified,
                         snapshot_at=ending.get('captured_at') if ending else None,
                         reconciliation_at=reconcile_at, now=now,
                         execution=orders, activities=cash, truncated=truncated,
                         fill_quantity_mismatches=fill_mismatches,
                         collector_errors=collector_errors,
                         corporate_action_symbols=corp_symbols)
    delta = period_cash_adjusted_equity_change(beginning=beginning, ending=ending, cash=endpoint_cash,
                                               history_complete=verified, cutoff=cutoff, now=now)
    return {
        'schema': 'atlas_v_intelligence_2', 'generated_at_utc': iso(now),
        'period_days': days, 'mode': 'read_only',
        'account_risk': risks, 'cash_activity_audit': cash,
        'order_execution': orders, 'reentry_surveillance': reentry,
        'cash_adjusted_equity_change': delta, 'data_health': health,
        'limitations': [
            'No broker-certified realized profit/loss or tax-lot accounting.',
            'No bid/ask snapshots; price slippage cannot be measured.',
            'No historical sector or benchmark data; these metrics are not manufactured.',
            'The extension cannot validate unrecorded deposits or transferred securities.',
            'Risk alerts are informational and cannot submit or change orders.',
        ],
    }
