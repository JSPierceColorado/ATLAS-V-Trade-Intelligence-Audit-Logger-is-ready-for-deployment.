# ATLAS V — strategy analytics prototype (review-only)

This is an **unmerged, undeployed** proof-of-concept module for the existing
ATLAS V audit logger. It performs no broker calls and does not modify a database.

## Current capabilities

- Assign sale proceeds and matched FIFO cost back to the originating buyer bot,
  including when the seller's own order is unattributed.
- Handle partial sales and more than one bot buying the same symbol.
- Calculate **gross estimated** realized P/L, win/loss counts, profit factor,
  holding time, and matched-cost return by strategy.
- Allocate remaining open-position estimated P/L to buyer lots **only when**
  FIFO inventory quantity matches the broker position snapshot.
- Count purchases within 7 days of a sale and detect loss -> repurchase -> loss.
- Flag unmatched sales, unknown opening inventory, and corporate actions.

## Known limitations / prerequisites before production merge

1. **Not official broker realized P/L.** FIFO matching may not match tax lots,
   especially after transfers, corporate actions or historical missing fills.
2. The input order history must be complete from the start of positions;
   verify `backfill_complete` and account opening balances first.
3. Fee allocation, adjusted corporate-action cost basis, exact benchmark
   comparisons, return on strategy capital over time and ATLAS I-IV engine
   provenance **are not implemented yet**. Do not describe this prototype as
   a complete per-strategy performance dashboard.
4. Count of winning or losing lot portions is not necessarily the count of
   independent strategy decisions or trades.
5. Commission/fee handling and intraday strategy cash flows need separate
   reconciliation to avoid double counting.
6. The module currently returns unmatched or complex symbol warnings; until
   all relevant warnings are resolved, suppress headline total P/L claims.
7. Broker activity `FILL` quantity consistency should be validated against
   ATLAS V's existing `filled_orders_with_activity_qty_mismatch` check.

## Standalone tests

```bash
python -m unittest discover -s tests -v
```

## Integration candidate (after review)

Copy `strategy_analytics.py` next to `main.py`; for a **read-only preview** within
`report()` or an operator-only command, run:

```python
from strategy_analytics import read_from_db
report = read_from_db(db, aid, 30)
print(json.dumps(report, indent=2, default=str))
```

Do not yet hook this prototype into periodic live reporting: test against a
reconciled fixture containing historical corporate actions, partial fills,
pre-existing inventory, transfers, and shared-symbol multi-bot lots first.

## GitHub write restriction

The connected GitHub integration returned HTTP 403 when creating the requested
`feature/strategy-performance-attribution` branch. This package is therefore
provided for manual review and upload. No repo or Railway deployment was changed.
