# ATLAS V Audit Intelligence v2 — review-ready upgrade

**Status: built and unit-tested locally, NOT committed or deployed.**

This upgrade is designed for the existing GitHub repository:
`JSPierceColorado/ATLAS-V-Trade-Intelligence-Audit-Logger-is-ready-for-deployment.`

The assistant's GitHub connection could read the source but returned HTTP 403 for branch creation. The bundle therefore contains a fail-closed upgrade script rather than modifying the live `main` branch. The script's source anchors were verified against GitHub `main` on November 6, 2026. **Do not apply straight to production.**

## New capabilities

| Capability | Implementation | Evidence limitations |
| --- | --- | --- |
| V3.1 attribution | `v3trail-` recognition in collector and historical strategy reports | Still depends on buyer order ID prefix; unknown IDs remain unattributed |
| Strategy version breakdown | Known version-prefix counts in execution report | Does not assign ATLAS I-IV research engine provenance |
| Account concentration and exposure | Single-name/top-five/gross exposure, short exposure and negative cash alerts | No sector data, VaR, beta or price forecast |
| External cash activity audit | CSD deposits, CSW withdrawals, dividends, fees and interest separated | Journals, security transfers and ambiguous activity types suppress cash-flow conclusions |
| Provisional cash-adjusted equity change | Endpoint-aligned equity change minus identified cash contributions | NOT official P/L, time-weighted return or IRR; requires full history, current snapshots and no ambiguous flows |
| Execution monitoring | Order statuses, median and p90 fill delay, unrecognized order IDs | No execution slippage without decision-time bid/ask |
| Reentry monitoring | Distinct buy orders after same-symbol sales, within 24 hours and 7 days | Not proof of violation or attribution of seller bot |
| Data-quality gating | Stale snapshot/reconciliation warnings, fill-quantity mismatch, errors, corporate-action warnings, query truncation | Healthy collection does not certify cost basis |
| Audit reports | Added `intelligence_v2` object to existing hourly and longer reports | Existing reports remain unchanged; optional extension errors do not block collection |

No new Alpaca endpoints, write methods, trading actions, database migrations, or secrets are added.

## Files

- `audit_intelligence.py` — new read-only analysis module, using parameterized SQL SELECT statements.
- `apply_upgrade.py` — safe, anchor-checked updater for `main.py`, `strategy_analytics.py`, and `Dockerfile`.
- `tests/` — pure unit and fake-DB integration tests.
- `README_UPGRADE.md` — this guide.

`apply_upgrade.py` refuses to modify files if its expected source anchors have drifted or if the destination module already contains different code. It displays a dry-run diff by default.

## Installation / deployment plan

1. On a development machine, clone the original repository and **create a feature branch**:
   ```bash
   git clone <EXACT_CLONE_URL_FROM_GITHUB> atlas-v-dev
   cd atlas-v-dev
   git checkout -b feature/atlas-v-audit-intelligence-v2
   ```
   Copy the exact URL from the repository's GitHub **Code → Clone** control. The repository name has a trailing period, so do not guess or shorten its URL.
2. Unzip this upgrade package into a separate local directory, not over the repository.
3. In the upgrade package, first check the dry-run diffs:
   ```bash
   python apply_upgrade.py --repo /path/to/ATLAS-V-repo
   ```
4. After reviewing the diff, apply it to the feature branch:
   ```bash
   python apply_upgrade.py --repo /path/to/ATLAS-V-repo --write
   ```
5. Run the module's tests from the upgrade directory:
   ```bash
   python -m unittest discover -s tests -v
   ```
6. Check Python syntax and diff on the feature branch:
   ```bash
   cd /path/to/ATLAS-V-repo
   python -m py_compile main.py strategy_analytics.py audit_intelligence.py
   git diff --check
   git diff
   ```
7. Review and create a pull request. Run the collector on a **staging database** (or read-only clone) before production; confirm report shape, database query performance and consistency with broker records. Do not connect the upgrade to a new live buyer deployment during validation.

## Safe operating principles

- The Alpaca `AlpacaReadOnly` client remains unchanged and uses GET only.
- The extension operates entirely on data already stored in PostgreSQL.
- Database writes are NOT introduced in this upgrade. The existing collector retains its normal writes to the audit database.
- The optional report extension fails independently of data collection. Any exception is recorded as an unavailable report, not a fabricated number.
- Time-weighted/money-weighted returns, strategy capital allocation, real order slippage, price-movement analysis, benchmark comparisons, sector exposures, and replay/shadow strategy outcomes **remain future work**. They require high-quality historical inputs, not merely formulas.
- The extension intentionally does not overwrite historical audit data, correct broker cost basis, or silently reinterpret transfers.

## Testing scope

Local Python unit tests exercise pure analytics, cash-flow fail-closed behavior, partial-fill reentry deduplication, risk alerts, snapshot-aligned cash adjustment, read-only SQL report integration with a fake database, and upgrade-script anchors. The anchor strings were also cross-checked against the current GitHub source. **The full updated application has not been integration-tested against the user's live Railway database or Alpaca account**, and no deployment has occurred.

## Rollback

If a reviewed feature branch is deployed and later causes report errors, revert the feature branch commit or restore `main.py`, `strategy_analytics.py`, and `Dockerfile` to their prior commit, remove `audit_intelligence.py`, and redeploy the last known working audit collector. No database migration is needed for this upgrade.
