import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from audit_intelligence import (
    strategy_from_client_id, account_risk, summarize_cash_activities,
    execution_quality, reentry_surveillance, period_cash_adjusted_equity_change,
    data_health, read_intelligence,
)

UTC = timezone.utc
T = datetime(2026, 11, 6, 16, 0, tzinfo=UTC)


class IntelligenceTests(unittest.TestCase):
    def test_strategy_attribution_versions(self):
        self.assertEqual(strategy_from_client_id('v3trail-abcd'), 'V3.1 Trailing Buyer')
        self.assertEqual(strategy_from_client_id('v3dip-123'), 'V3-buyer')
        self.assertEqual(strategy_from_client_id('V2SP100-ABC'), 'Buyer (V2 S&P100)')
        self.assertEqual(strategy_from_client_id('ab-100'), 'Alpaca-Buyer')
        self.assertEqual(strategy_from_client_id('manual'), 'unattributed')

    def test_risk_concentration_alerts_and_no_margin_inference(self):
        risks = account_risk({'equity': '1000', 'cash': '700', 'buying_power': '2000'},
                             [{'symbol': 'ABC', 'market_value': '300'},
                              {'symbol': 'DEF', 'market_value': '200'}])
        self.assertEqual(risks['largest_position_pct_of_equity'], 30)
        self.assertEqual(risks['gross_exposure_pct_of_equity'], 50)
        self.assertIn('single_name_concentration', [a['code'] for a in risks['alerts']])
        self.assertNotIn('negative_cash', [a['code'] for a in risks['alerts']])

    def test_negative_cash_short_exposure(self):
        risks = account_risk({'equity': '200', 'cash': '-20'},
                             [{'symbol': 'A', 'market_value': '-15'}])
        self.assertEqual(risks['short_market_value_abs_usd'], '15')
        self.assertIn('short_exposure', [a['code'] for a in risks['alerts']])
        self.assertIn('negative_cash', [a['code'] for a in risks['alerts']])

    def test_no_equity_suppresses_weights(self):
        r = account_risk({'equity': '0'}, [{'symbol': 'A', 'market_value': '1'}])
        self.assertIsNone(r['largest_position_pct_of_equity'])

    def test_cash_classification_not_confusing_dividend_with_deposit(self):
        c = summarize_cash_activities([
            {'activity_type': 'CSD', 'net_amount': '500'},
            {'activity_type': 'CSW', 'net_amount': '-70'},
            {'activity_type': 'DIV', 'net_amount': '2.50'},
            {'activity_type': 'FEE', 'net_amount': '-1.20'},
        ])
        self.assertEqual(c['identified_net_external_cash_flow_usd'], '430')
        self.assertEqual(c['observed_dividends_usd'], '2.50')
        self.assertEqual(c['observed_fees_usd'], '-1.20')

    def test_ambiguous_transfer_suppresses_flow_and_profit(self):
        c = summarize_cash_activities([{'activity_type': 'TRANS', 'net_amount': '100'}])
        self.assertFalse(c['external_cash_flows_classifiable'])
        self.assertIsNone(c['identified_net_external_cash_flow_usd'])
        p = period_cash_adjusted_equity_change(
            beginning={'captured_at': T-timedelta(days=1), 'equity': '100'},
            ending={'captured_at': T, 'equity': '200'}, cash=c,
            history_complete=True, cutoff=T-timedelta(days=1), now=T)
        self.assertEqual(p['status'], 'unavailable')

    def test_missing_cash_values_and_wrong_sign_fail_closed(self):
        c = summarize_cash_activities([{'activity_type': 'CSD', 'net_amount': '-100'}])
        self.assertFalse(c['external_cash_flows_classifiable'])
        self.assertIsNone(c['identified_net_external_cash_flow_usd'])

    def test_cash_adjusted_change_provisional_only(self):
        c = summarize_cash_activities([{'activity_type': 'CSD', 'net_amount': '500'}])
        p = period_cash_adjusted_equity_change(
            beginning={'captured_at': T-timedelta(days=1), 'equity': '1000'},
            ending={'captured_at': T, 'equity': '1525'}, cash=c,
            history_complete=True, cutoff=T-timedelta(days=1), now=T)
        self.assertEqual(p['status'], 'provisional')
        self.assertEqual(p['estimate_usd'], '25')
        self.assertIn('NOT broker-certified', p['caveat'])

    def test_stale_snapshot_suppresses_profit(self):
        c = summarize_cash_activities([])
        p = period_cash_adjusted_equity_change(
            beginning={'captured_at': T-timedelta(days=2), 'equity': '100'},
            ending={'captured_at': T, 'equity': '110'}, cash=c,
            history_complete=True, cutoff=T-timedelta(days=1), now=T)
        self.assertEqual(p['status'], 'unavailable')

    def test_execution_delay_and_order_id(self):
        x = execution_quality([
            {'status': 'filled', 'client_order_id': 'v3trail-1', 'submitted_at': T, 'filled_at': T+timedelta(seconds=5)},
            {'status': 'canceled', 'client_order_id': 'manual', 'submitted_at': T},
        ])
        self.assertEqual(x['observed_fill_delay_median_seconds'], 5)
        self.assertEqual(x['recognized_strategy_versions']['V3.1 Trailing Buyer'], 1)
        self.assertEqual(x['unattributed_order_count'], 1)

    def test_reentry_deduplicates_partial_fills_and_excludes_prior_window(self):
        f = [
            {'symbol': 'A', 'side': 'sell', 'at': T-timedelta(days=3), 'order_id': 's1'},
            {'symbol': 'A', 'side': 'buy', 'at': T-timedelta(days=3, hours=-1), 'order_id': 'old'},
            {'symbol': 'B', 'side': 'sell', 'at': T-timedelta(hours=2), 'order_id': 's2'},
            {'symbol': 'B', 'side': 'buy', 'at': T-timedelta(hours=1), 'order_id': 'b1'},
            {'symbol': 'B', 'side': 'buy', 'at': T-timedelta(minutes=50), 'order_id': 'b1'},
        ]
        r = reentry_surveillance(f, start_date=T-timedelta(days=1))
        self.assertEqual(r['repurchases_within_24h'], 1)
        self.assertEqual(r['repurchases_within_7d'], 1)

    def test_health_independently_does_not_certify_profit(self):
        x = data_health(history_complete=True, snapshot_at=T, reconciliation_at=T,
                        now=T, execution={'unattributed_order_count': 0},
                        activities={'external_cash_flows_classifiable': True}, truncated=False)
        self.assertEqual(x['status'], 'collection_healthy')
        self.assertEqual(x['profit_verification'], 'not_broker_certified')


class FakeCursor:
    def __init__(self, responses):
        self.responses = iter(responses)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def execute(self, query, params):
        self.data = next(self.responses)
        self.query = query
        self.params = params
        if 'FROM atlas_' in query:
            assert 'account_id=%s' in query, query
    def fetchone(self):
        return self.data
    def fetchall(self):
        return self.data


class FakeDB:
    def __init__(self, responses):
        self.cursor_obj = FakeCursor(responses)
    def cursor(self):
        return self.cursor_obj


class DBIntegrationTests(unittest.TestCase):
    def test_read_only_queries_and_report(self):
        latest = (T, Decimal('1050'), Decimal('550'), Decimal('550'), Decimal('1050'),
                  Decimal('500'), Decimal('0'), Decimal('120'))
        prior = (T-timedelta(days=7), Decimal('1000'), Decimal('500'), Decimal('500'), Decimal('1000'),
                 Decimal('500'), Decimal('0'), Decimal('120'))
        db = FakeDB([
            latest, prior,
            [('ABC', Decimal('500'))],
            [('CSD', Decimal('40'), T-timedelta(days=2))],
            [('filled', 'v3trail-1', T-timedelta(days=1), T-timedelta(days=1)+timedelta(seconds=3))],
            [('ABC', 'buy', T-timedelta(days=1), 'oid1', 'v3trail-1', 'unattributed')],
            [('backfill_complete', 'true'), ('last_reconciliation_success', T.isoformat())],
            (0,), [], (0,),
            [('CSD', Decimal('40'))],
        ])
        report = read_intelligence(db, 'accountABC', 7, T)
        self.assertEqual(report['mode'], 'read_only')
        self.assertEqual(report['account_risk']['gross_exposure_pct_of_equity'], 47.62)
        self.assertEqual(report['cash_adjusted_equity_change']['estimate_usd'], '10')
        self.assertEqual(report['data_health']['profit_verification'], 'not_broker_certified')


if __name__ == '__main__':
    unittest.main()
