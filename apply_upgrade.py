#!/usr/bin/env python3
"""Apply ATLAS V intelligence upgrade to a checked-out repository safely.

Run from a copy/feature branch of the original repository, NEVER from live main.
Idempotent, anchor-checked and stages all edits before writing anything.
"""
from __future__ import annotations

import argparse
import difflib
from pathlib import Path


def require_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f'{label}: expected one matching source anchor, found {count}; no files changed.')
    return source.replace(old, new, 1)


def patch_main(content: str) -> str:
    if 'ATLAS_V_INTELLIGENCE_V2' in content:
        return content
    content = require_once(
        content,
        '    if value.startswith("v3dip-"):\n        return "V3-buyer"',
        '    if value.startswith("v3trail-"):\n        return "V3.1 Trailing Buyer"\n'
        '    if value.startswith("v3dip-"):\n        return "V3-buyer"',
        'main strategy attribution')
    # Old V3.1 orders are only reclassified by normal re-collection inside the
    # reconciliation window; this SELECT also corrects historic report grouping.
    content = require_once(
        content,
        "count(*) FILTER (WHERE bot='unattributed')",
        "count(*) FILTER (WHERE bot='unattributed' AND lower(coalesce(client_order_id,'')) NOT LIKE 'v3trail-%')",
        'main unattributed order count')
    content = require_once(
        content,
        "SELECT coalesce(bot,'unattributed'),count(*),",
        "SELECT CASE WHEN lower(coalesce(client_order_id,'')) LIKE 'v3trail-%' "
        "THEN 'V3.1 Trailing Buyer' ELSE coalesce(bot,'unattributed') END,count(*),",
        'main strategy grouping')
    content = require_once(
        content,
        '    return {\n        "period_days": days, "account_id": account_id, "generated_at_utc": iso(now_utc()),',
        '    result = {\n        "period_days": days, "account_id": account_id, "generated_at_utc": iso(now_utc()),',
        'main audit report result')
    content = require_once(
        content,
        '        "collector_errors_in_period": error_count,\n    }\n\n\ndef report(',
        '        "collector_errors_in_period": error_count,\n    }\n'
        '    # ATLAS_V_INTELLIGENCE_V2: isolated report extension; collector unaffected.\n'
        '    try:\n'
        '        from audit_intelligence import read_intelligence\n'
        '        result["intelligence_v2"] = read_intelligence(db, account_id, days, now_utc())\n'
        '    except Exception as exc:\n'
        '        LOG.exception("Optional read-only intelligence report failed")\n'
        '        result["intelligence_v2"] = {"status": "unavailable",\n'
        '                                     "reason": type(exc).__name__,\n'
        '                                     "note": "Collector continues; optional report failed."}\n'
        '    return result\n\n\ndef report(',
        'main intelligence integration')
    return content


def patch_strategy(content: str) -> str:
    if "THEN 'V3.1 Trailing Buyer'" in content:
        return content
    return require_once(
        content,
        "                           coalesce(o.bot,'unattributed')\n"
        "                    FROM atlas_activities a LEFT JOIN atlas_orders o",
        "                           CASE WHEN lower(coalesce(o.client_order_id,'')) LIKE 'v3trail-%'\n"
        "                                THEN 'V3.1 Trailing Buyer'\n"
        "                                ELSE coalesce(o.bot,'unattributed') END\n"
        "                    FROM atlas_activities a LEFT JOIN atlas_orders o",
        'historical strategy attribution')


def patch_docker(content: str) -> str:
    if 'audit_intelligence.py' in content:
        return content
    return require_once(content, 'COPY main.py strategy_analytics.py ./',
                        'COPY main.py strategy_analytics.py audit_intelligence.py ./', 'docker image')


def plan(root: Path, package: Path) -> dict[Path, str]:
    files = {
        root / 'main.py': patch_main,
        root / 'strategy_analytics.py': patch_strategy,
        root / 'Dockerfile': patch_docker,
    }
    planned = {}
    for path, transform in files.items():
        if not path.is_file():
            raise RuntimeError(f'Missing expected repo file: {path}')
        planned[path] = transform(path.read_text(encoding='utf-8'))
    src = package / 'audit_intelligence.py'
    if not src.is_file():
        raise RuntimeError('audit_intelligence.py not present alongside apply_upgrade.py')
    dest = root / 'audit_intelligence.py'
    if dest.exists() and dest.read_bytes() != src.read_bytes():
        raise RuntimeError('audit_intelligence.py already exists with different content: inspect manually')
    planned[dest] = src.read_text(encoding='utf-8')
    return planned


def main() -> int:
    parser = argparse.ArgumentParser(description='Apply ATLAS V read-only intelligence v2 update')
    parser.add_argument('--repo', type=Path, default=Path('.'), help='Existing local ATLAS V repo checkout')
    parser.add_argument('--write', action='store_true', help='Actually write the changes (default: dry run)')
    args = parser.parse_args()
    root = args.repo.resolve()
    package = Path(__file__).resolve().parent
    updates = plan(root, package)  # validate every anchor before writing any file
    for dest, new in updates.items():
        previous = dest.read_text(encoding='utf-8') if dest.exists() else ''
        diff = ''.join(difflib.unified_diff(previous.splitlines(True), new.splitlines(True),
                                            fromfile=str(dest), tofile=str(dest) + '.updated'))
        if diff:
            print(diff[:14000])
        else:
            print(f'Unchanged: {dest.name}')
    if not args.write:
        print('\nDRY RUN: no changes written. Rerun with --write on a feature branch.')
        return 0
    for dest, new in updates.items():
        if not dest.exists() or dest.read_text(encoding='utf-8') != new:
            dest.write_text(new, encoding='utf-8')
            print(f'Updated {dest}')
    print('Upgrade written to local checkout. Run unit tests and review git diff before commit/deploy.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
