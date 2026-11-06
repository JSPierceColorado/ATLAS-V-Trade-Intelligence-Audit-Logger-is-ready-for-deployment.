# ATLAS V — Psycopg LIKE wildcard hotfix (November 6, 2026)

## What was found
The service is collecting snapshots, orders, and activities, but the hourly,
strategy, and long-term report stages repeatedly fail with:
`psycopg.ProgrammingError: only '%s', '%b', '%t' are allowed as placeholders, got '%''`

The deployed Python source contains three SQL patterns `LIKE 'v3trail-%'` inside
parameterized Psycopg execute calls. In a parameterized query, a literal `%`
must be escaped as `%%`; hence `LIKE 'v3trail-%%'` in Python source becomes
`LIKE 'v3trail-%'` in PostgreSQL.

## Upload instructions
1. Make a backup / feature branch for the current `JSPierceColorado/ATLAS-V` repository.
2. Replace ONLY root `main.py` and `strategy_analytics.py` with these two files.
3. Leave `audit_intelligence.py`, `Dockerfile`, `requirements.txt`, secrets, and Postgres unchanged.
4. Let Railway build the new GitHub commit. Watch deployment logs for
   `ATLAS_REPORT_1D`, `ATLAS_STRATEGY_REPORT_...`, and `ATLAS_REPORT_7D` / `30D`.
5. Confirm error `only '%s', '%b', '%t' ... got '%''` no longer occurs.

## Safety and scope
- Both source files were based on GitHub-matching files (verified content-addressed SHA)
  and have EXACTLY three literal wildcard escapes changed in total.
- No changes to Alpaca requests, collector cadence, tables, database writes,
  account credentials, order execution, or risk rules.
- Local compile checks passed. This is not a live Railway integration test.
- Back up the current repo, verify the diff, and deploy via a reviewed commit.
