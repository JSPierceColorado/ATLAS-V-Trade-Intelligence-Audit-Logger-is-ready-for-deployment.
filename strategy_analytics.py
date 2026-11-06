"""ATLAS V strategy analytics (read-only; no broker write operations).

Fill-level FIFO attribution to the ORIGINAL BUYER; seller order identity does not
change ownership. Conservatively reports incomplete lots and unallocated shares.
All dollar figures are gross estimates before fees/taxes unless known elsewhere.
"""
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import median

D = lambda x: Decimal(str(x))


def calculate(fills, positions, cutoff, now=None):
    """Compute realized matched-lot metrics by strategy.

    fills: iterable dicts with symbol, side, qty, price, at, bot, order_id.
    positions: iterable dicts with symbol, qty, current_price. Mark-to-market
      is allocated only where the observed open lots reconcile to the position.
    cutoff: reporting window start, timezone-aware datetime.
    Note: all fills since account inception must be supplied for valid cost basis.
    """
    now = now or datetime.now(timezone.utc)
    inventory = defaultdict(deque)
    stats = defaultdict(lambda: {
        'buys': 0, 'sold_lots': 0, 'matched_sell_qty': D(0),
        'matched_sale_cost': D(0), 'matched_sale_proceeds': D(0),
        'gross_realized_pl': D(0), 'wins': 0, 'losses': 0,
        'gross_winning_pl': D(0), 'gross_losing_pl': D(0),
        'holding_hours': [], 'unrealized_pl_estimate': D(0),
        'marked_market_value': D(0), 'unknown_ownership_qty': D(0),
        'repurchases_within_7d': 0, 'repurchases_after_loss': 0,
        'repeated_loss_cycles': 0,
    })
    last_exit = {}  # symbol -> (timestamp, return_pct, prior buyer)
    problematic = []
    trade_cycles = []
    for f in sorted(fills, key=lambda r: (r['at'], str(r.get('order_id', '')))):
        sym = str(f['symbol']).upper().replace('/', '')
        qty, price = D(f['qty']), D(f['price'])
        at = f['at']
        side = f['side'].lower()
        if qty <= 0 or price <= 0 or at > now or side not in ('buy', 'sell'):
            problematic.append({'symbol': sym, 'reason': 'invalid_fill'})
            continue
        if side == 'buy':
            owner = f.get('bot') or 'unattributed'
            if at >= cutoff:
                stats[owner]['buys'] += 1
                prior = last_exit.get(sym)
                if prior and timedelta(0) <= at-prior[0] <= timedelta(days=7):
                    stats[owner]['repurchases_within_7d'] += 1
                    if prior[1] is not None and prior[1] < 0:
                        stats[owner]['repurchases_after_loss'] += 1
            inventory[sym].append({'qty': qty, 'price': price, 'at': at, 'bot': owner,
                                   'reentered_after_loss': bool(last_exit.get(sym) and
                                       timedelta(0) <= at-last_exit[sym][0] <= timedelta(days=7) and
                                       last_exit[sym][1] is not None and last_exit[sym][1] < 0)})
        else:
            left = qty
            matched_cost = D(0)
            matched_proceeds = D(0)
            by_owner = defaultdict(lambda: {'qty': D(0), 'cost': D(0), 'proceeds': D(0), 'repeated_loss': False})
            while left > 0 and inventory[sym]:
                lot = inventory[sym][0]
                used = min(left, lot['qty'])
                cost = used*lot['price']
                proceeds = used*price
                entry = by_owner[lot['bot']]
                entry['qty'] += used
                entry['cost'] += cost
                entry['proceeds'] += proceeds
                entry['repeated_loss'] = entry['repeated_loss'] or lot['reentered_after_loss']
                if at >= cutoff:
                    stats[lot['bot']]['holding_hours'].append(float((at-lot['at']).total_seconds()/3600))
                matched_cost += cost
                matched_proceeds += proceeds
                lot['qty'] -= used
                left -= used
                if lot['qty'] == 0:
                    inventory[sym].popleft()
            if left:
                problematic.append({'symbol': sym, 'reason': 'unmatched_sell', 'qty': str(left)})
            full_sale_return = (matched_proceeds-matched_cost)/matched_cost if not left and matched_cost else None
            last_exit[sym] = (at, full_sale_return, next(iter(by_owner)) if len(by_owner)==1 else None)
            if at >= cutoff:
                for owner, entry in by_owner.items():
                    s = stats[owner]
                    gain = entry['proceeds']-entry['cost']
                    s['sold_lots'] += 1
                    s['matched_sell_qty'] += entry['qty']
                    s['matched_sale_cost'] += entry['cost']
                    s['matched_sale_proceeds'] += entry['proceeds']
                    s['gross_realized_pl'] += gain
                    if gain > 0:
                        s['wins'] += 1
                        s['gross_winning_pl'] += gain
                    elif gain < 0:
                        s['losses'] += 1
                        s['gross_losing_pl'] += gain
                        if entry['repeated_loss']:
                            s['repeated_loss_cycles'] += 1
                    trade_cycles.append({'symbol': sym, 'buyer': owner, 'exit_at': at.isoformat(),
                                         'qty': str(entry['qty']), 'gross_pl': str(gain),
                                         'reentry_after_loss': entry['repeated_loss']})
    snapshot = {str(p['symbol']).upper().replace('/', ''): p for p in positions}
    for sym, lots in inventory.items():
        if not lots:
            continue
        if sym not in snapshot:
            problematic.append({'symbol': sym, 'reason': 'missing_snapshot'})
            continue
        observed_qty = D(snapshot[sym]['qty'])
        recorded_qty = sum((lot['qty'] for lot in lots), D(0))
        if abs(observed_qty-recorded_qty) > D('0.000001'):
            problematic.append({'symbol': sym, 'reason': 'position_qty_mismatch',
                                'broker_qty': str(observed_qty), 'fifo_qty': str(recorded_qty)})
            continue
        mark = D(snapshot[sym]['current_price'])
        for lot in lots:
            s = stats[lot['bot']]
            s['unrealized_pl_estimate'] += lot['qty']*(mark-lot['price'])
            s['marked_market_value'] += lot['qty']*mark
    for sym in set(snapshot)-{s for s, lots in inventory.items() if lots}:
        if D(snapshot[sym]['qty']) != 0:
            problematic.append({'symbol': sym, 'reason': 'untracked_open_position'})
    out = {}
    for owner, s in sorted(stats.items()):
        result = {k: (str(v) if isinstance(v, Decimal) else v) for k,v in s.items() if k != 'holding_hours'}
        result['win_rate_pct'] = round(100*s['wins']/(s['wins']+s['losses']), 2) if s['wins']+s['losses'] else None
        result['profit_factor'] = round(float(s['gross_winning_pl'] / -s['gross_losing_pl']), 4) if s['gross_losing_pl'] < 0 else None
        result['gross_realized_return_on_sold_cost_pct'] = round(float(100*s['gross_realized_pl']/s['matched_sale_cost']),4) if s['matched_sale_cost'] else None
        result['median_matched_hold_hours'] = round(median(s['holding_hours']),2) if s['holding_hours'] else None
        out[owner] = result
    return {'period_start': cutoff.isoformat(), 'strategies': out, 'trade_cycles': trade_cycles,
            'data_quality': {'problematic': problematic, 'problem_count': len(problematic),
                             'realized_pl_is_broker_official': False,
                             'net_of_fees': False,
                             'note': 'FIFO gross estimated matching; corporate actions, prehistory and fees may invalidate lot costs. Suppress headline totals until reconciled.'}}


def read_from_db(db, account_id, days):
    """Read existing ATLAS V SQL tables, without changing schema or broker activity."""
    now = datetime.now(timezone.utc)
    cutoff = now-timedelta(days=days)
    with db.cursor() as cur:
        cur.execute("""SELECT a.symbol,a.side,a.qty,a.price,a.occurred_at,a.order_id,
                           CASE WHEN lower(coalesce(o.client_order_id,'')) LIKE 'v3trail-%'
                                THEN 'V3.1 Trailing Buyer'
                                ELSE coalesce(o.bot,'unattributed') END
                    FROM atlas_activities a LEFT JOIN atlas_orders o
                    ON a.account_id=o.account_id AND a.order_id=o.order_id
                    WHERE a.account_id=%s AND a.activity_type='FILL'
                    AND a.occurred_at IS NOT NULL ORDER BY a.occurred_at,a.activity_id""", (account_id,))
        fills = [dict(zip(('symbol','side','qty','price','at','order_id','bot'),row)) for row in cur.fetchall()]
        cur.execute("""SELECT symbol,qty,current_price FROM atlas_positions WHERE account_id=%s
                    AND captured_at=(SELECT max(captured_at) FROM atlas_positions WHERE account_id=%s)""",
                    (account_id, account_id))
        positions = [dict(zip(('symbol','qty','current_price'),row)) for row in cur.fetchall()]
        cur.execute("""SELECT DISTINCT upper(replace(symbol,'/','')) FROM atlas_activities
                    WHERE account_id=%s AND symbol IS NOT NULL
                    AND activity_type IN ('SPLIT','SPIN','MA','REORG','ACATS','JNLS','OPASN','OPCA','OPEXC','OPEXP')""", (account_id,))
        complex_symbols = [x[0] for x in cur.fetchall()]
    report = calculate(fills, positions, cutoff, now)
    report['data_quality']['corporate_action_symbols'] = complex_symbols
    report['data_quality']['headline_pl_safe'] = False  # historical fill coverage is not independently verified
    report['data_quality']['historical_fill_coverage_verified'] = False
    return report
