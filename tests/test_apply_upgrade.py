import unittest
from apply_upgrade import patch_main, patch_strategy, patch_docker


class PatchTests(unittest.TestCase):
    def test_rejects_unknown_code_instead_of_silent_edit(self):
        with self.assertRaises(RuntimeError):
            patch_main('def main():\n    pass\n')

    def test_known_anchors_patch_correctly(self):
        snippet = '''def identify_bot(value):
    if value.startswith("v3dip-"):
        return "V3-buyer"

count(*) FILTER (WHERE bot='unattributed')
SELECT coalesce(bot,'unattributed'),count(*),
    return {
        "period_days": days, "account_id": account_id, "generated_at_utc": iso(now_utc()),
        "collector_errors_in_period": error_count,
    }


def report(db):
    pass
'''
        patched = patch_main(snippet)
        self.assertIn('V3.1 Trailing Buyer', patched)
        self.assertIn('ATLAS_V_INTELLIGENCE_V2', patched)
        self.assertEqual(patch_main(patched), patched)

    def test_strategy_query(self):
        original = "                           coalesce(o.bot,'unattributed')\n                    FROM atlas_activities a LEFT JOIN atlas_orders o"
        new = patch_strategy(original)
        self.assertIn('v3trail-%', new)

    def test_docker(self):
        self.assertEqual(patch_docker('COPY main.py strategy_analytics.py ./'),
                         'COPY main.py strategy_analytics.py audit_intelligence.py ./')


if __name__ == '__main__':
    unittest.main()
