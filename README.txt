ATLAS V — MANUAL INTEGRATION

1. Back up the current GitHub main.py.
2. Replace main.py with the supplied main.py in the repository root.
3. Replace or add strategy_analytics.py in the SAME repository root.
4. Commit both files together. Railway should redeploy from the linked branch.
5. Verify Railway logs show 'ATLAS V starting' and recent_reconciliation.
6. Within the next scheduled report interval (normally 1 hour), look for:
   ATLAS_STRATEGY_REPORT_1D
   ATLAS_STRATEGY_REPORT_7D
   ATLAS_STRATEGY_REPORT_30D
   ATLAS_STRATEGY_REPORT_90D
   The longer reports normally run every 6 hours, unless their state is not set.
7. Strategy output is ESTIMATED gross FIFO P/L, NOT official net returns.
   Historical coverage remains unverified; inspect data_quality.problematic
   and corporate_action_symbols before drawing conclusions.

No new environment variables, database migrations, or trading permissions.
No trading strategy or buyer/seller bot is changed.
The existing collector and existing ATLAS_REPORT_* remain in place.
A strategy-report failure is isolated from the collector cycle.
