# ATLAS V — Alpaca Trade Intelligence & Audit Logger

An independent, **GET-only** collector for Alpaca trading accounts. Designed as a continuously running Railway worker, not a scheduled cron job. It does not place, modify, or cancel orders. It never changes existing trading bots.

## Deploy on Railway

1. Create a new GitHub repository such as `atlas-v-trade-logger` with `main.py`, `requirements.txt`, and `Dockerfile` at the repository root. You may include this README.
2. In your existing Railway `Investing` project, add a **PostgreSQL** database. Turn on **backups** in its Railway settings. A database costs money and is required for durability; Railway service logs alone are not durable history.
3. Deploy a **new service with one replica** connected to the new GitHub repository. **Do not set a Railway cron schedule**, as this is a continuous worker. No public domain or HTTP health check is needed.
4. On the new ATLAS V service, add the following Railway Variables (reference your PostgreSQL service name as shown in Railway):

```dotenv
DATABASE_URL=${{Postgres.DATABASE_URL}}
APCA_API_KEY_ID=YOUR_OWN_ALPACA_KEY
APCA_API_SECRET_KEY=YOUR_OWN_ALPACA_SECRET
ALPACA_PAPER=true
```

`ALPACA_PAPER=true` (default) connects to the **paper** account. Set `ALPACA_PAPER=false` ONLY if the existing account to monitor is live, using the matching **live** API keys. The code still performs GET requests only. Credentials are secrets; configure them in Railway Variables, **never commit them to GitHub or paste them in chat**. Alpaca API keys may have trading privileges even though this code does not use them. Use least-privilege credentials where your account supports them.

The logger also recognizes the variable names `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` used by one of the existing bots. Set one complete pair.

5. Deploy and check the service **Deploy Logs** for `ATLAS V starting`, `Collection cycle`, `Snapshot`, and `recent_reconciliation`. You should see `ATLAS_REPORT_1D` JSON approximately hourly and `ATLAS_REPORT_7D`/`ATLAS_REPORT_30D` every six hours. The first backfill will progress in chunks, with `Backfill cursor advanced` log entries. Historical backfill can take multiple cycles.

### Optional environment variables

| Variable | Default | Purpose |
|---|---|---|
| `POLL_SECONDS` | `120` | Time between completed collection cycles; valid 30–3600 |
| `SNAPSHOT_SECONDS` | `600` | Capture account + all positions, every 10 minutes |
| `RECONCILE_DAYS` | `14` | Revisit the last 14 days of orders and activities each cycle |
| `BACKFILL_START_DATE` | `2020-01-01T00:00:00Z` | Start date for historical order and activity collection (set earlier if needed) |
| `BACKFILL_DAYS_PER_CYCLE` | `90` | Maximum historical calendar days collected per cycle |
| `OLD_ORDER_REFRESH_LIMIT` | `50` | Maximum older nonfinal orders refreshed per cycle |
| `REPORT_SECONDS` | `3600` | Emit structured rolling 24-hour report to Railway logs |
| `LONG_REPORT_SECONDS` | `21600` | Emit 7-day and 30-day analysis every six hours |
| `PORTFOLIO_HISTORY_SECONDS` | `86400` | Refresh Alpaca's daily portfolio history from `BACKFILL_START_DATE` with a consistent reporting baseline |
| `EXPECTED_ACCOUNT_ID` | empty | Optional fail-closed account identification guard |
| `REQUEST_PAUSE_SECONDS` | `0.35` | Pause between API calls to moderate rate usage |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

## What is recorded

All tables are created automatically, with the `atlas_` prefix:

- `atlas_orders`: latest Alpaca broker order state, IDs, symbol, side, status, fill quantities, attributed bot where confidently inferred, and raw JSON.
- `atlas_order_observations`: distinct observed order states over time (polling **does not** guarantee every momentary intermediate status is captured).
- `atlas_activities`: broker activities, including each fill/partial fill, dividends, deposits, withdrawals, interest, corporate actions, and fees when exposed by Alpaca.
- `atlas_accounts`: dated account equity, cash, buying power, portfolio value, margin figures, and raw account snapshots.
- `atlas_positions`: dated holdings, quantities, cost basis, exposure, unrealized P&L, and raw position snapshots.
- `atlas_portfolio_history`: broker-reported historical daily equity and P&L where available.
- `atlas_state`: backfill progress, last successful reconciliation, and health timestamps.
- `atlas_errors`: collection failures (secrets redacted from explicitly recognized environment variables).

Records are idempotently upserted and historical scanning is checkpointed. Polling re-reads the last 14 days to discover changed orders and delayed activities, and separately refreshes older orders previously observed as incomplete. All records are scoped by Alpaca account ID.

## Analysis

ATLAS V prints `ATLAS_REPORT_1D {json}` approximately every hour, and `ATLAS_REPORT_7D` / `ATLAS_REPORT_30D` every six hours, so Railway logs can be inspected using existing tools. It includes:

- Order totals, filled/canceled/rejected/partial statuses, bot-attributed order counts, and explicit stop-loss/take-profit order roles when broker-tagged.
- Buy/sell execution counts and total traded notional.
- Seven-day repurchases after a sale, including repurchases after *known losing* sales.
- Potential ~5% losing exits (**not proof** a stop-loss algorithm caused them).
- Approximate FIFO matched-fill realized **gross** P&L, only when sufficient history and no unhandled corporate/short activity is identified. **This is not official realized P&L**. It is deliberately null when records are incomplete or complexity is detected; tax-lot elections and fee effects may differ even when non-null.
- Holding duration, most-traded symbols, position concentration, latest account/position data, and observed peak-to-trough equity movement.
- Non-trade cash activities and broker-provided equity/P&L samples.
- Collector errors, discrepancies between filled quantities and execution activities, and backfill/last reconciliation status.

To print a JSON report for a different time window from a shell that has access to the Railway database:

```bash
python main.py report --days 7
python main.py report --days 30
python main.py once
```

No HTTP dashboard is exposed, by design. Direct database analysis is possible by connecting through an authorized database client. **Do not expose the PostgreSQL service publicly merely to query it.**

## Known limitations and important accuracy notes

- Paper and live accounts have separate keys/data. Orders submitted only in `DRY_RUN` don't exist in Alpaca and cannot be reconstructed from Alpaca alone.
- `V3-buyer` (`v3dip-`), `Buyer` (`v2sp100-`), and `Alpaca-Buyer` (`ab-`) use recognizable client order ID patterns. The existing seller bot uses `close_position` without a recognizable client order ID. Such orders are **unattributed** unless independently corroborated through Railway bot logs. No guesswork is used.
- Order status changes between polls can be missed. Completed order records and execution activity are reconciled separately.
- Alpaca activity availability and retention can limit historical backfill. Failed stages are logged and retried; no synthetic trades are created.
- The snapshot-equity peak-to-trough figure can include **deposits/withdrawals** and is not a time-weighted investment return. Broker historical profit/loss values are kept separate.
- FIFO lot-based realized estimates are not tax accounting. Corporate actions, shorts, preexisting positions, transfer history, fee treatment, crypto and symbol changes can make estimates unsuitable. Be guided by official broker statements for actual financial and tax reporting.
- PostgreSQL is not automatically backed up just because it is a separate service. Enable backups, monitor disk usage, and control who can access sensitive account records.
- This release was syntax-checked and its order/activity pagination logic was tested with simulated data, **but has not been integration-tested against your live Alpaca account or Railway database**. Validate its initial logs and stored data after deployment before relying on its analysis.

## Future upgrades

Once the logger is running we can instrument each trading bot with explicit, uniquely prefixed `client_order_id` tags and structured decision/event records, join these by Alpaca `order_id`, and add more precise stop-loss vs. take-profit attribution, execution timing, and strategy-level net economics.
