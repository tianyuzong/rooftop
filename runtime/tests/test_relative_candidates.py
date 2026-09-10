import unittest
from app.quant_portfolio import _with_relative_candidates


class RelativeCandidateTests(unittest.TestCase):
    def decision(self):
        return {'status': 'AVAILABLE', 'result': {
            'request': {'capital': 10000, 'max_positions': 2},
            'recommendation': {'positions': [], 'publish_gate_passed': False,
                'candidate_ranking': [
                    {'symbol': '600001', 'name': '便宜候选', 'composite_score': -3,
                     'reference_price': 30, 'probability_up': .3, 'rejection_reasons': ['趋势未通过']},
                    {'symbol': '600002', 'name': '排名第一', 'composite_score': -1,
                     'reference_price': 200, 'probability_up': .4, 'rejection_reasons': ['模型未通过']},
                    {'symbol': '600003', 'name': '排名第二', 'composite_score': -2,
                     'reference_price': 80, 'probability_up': .35, 'rejection_reasons': ['模型未通过']},
                ]}}}

    def test_all_negative_failed_candidates_still_ranked(self):
        d = _with_relative_candidates(self.decision())
        r = d['result']['recommendation']
        self.assertEqual([x['symbol'] for x in r['relative_recommendations']], ['600002', '600003'])
        self.assertIn('排名第二', d['summary'])
        self.assertNotIn('排名第一', d['summary'])
        self.assertFalse(r['publish_gate_passed'])
        self.assertEqual(r['positions'], [])
        self.assertEqual(r['relative_recommendations'][0]['blockers'], ['模型未通过'])

    def test_affordable_candidates_ignore_trial_cap_and_keep_full_pool(self):
        r = _with_relative_candidates(self.decision())['result']['recommendation']
        self.assertEqual([x['symbol'] for x in r['affordable_relative_recommendations']], ['600003', '600001'])
        self.assertGreater(r['affordable_relative_recommendations'][0]['minimum_lot_cost'], 8000)
        self.assertFalse(r['relative_recommendations'][0]['capital_affordable'])

    def test_affordable_passed_candidates_precede_observation_candidates(self):
        d = self.decision()
        d['result']['recommendation']['candidate_ranking'][0]['eligible'] = True
        r = _with_relative_candidates(d)['result']['recommendation']
        self.assertEqual(r['affordable_relative_recommendations'][0]['symbol'], '600001')
        self.assertEqual(r['screening_passed_count'], 1)

    def test_funded_result_and_missing_evidence_not_overridden(self):
        d = self.decision()
        d['result']['recommendation']['positions'] = [{'shares': 100}]
        self.assertIn('relative_recommendations', _with_relative_candidates(d)['result']['recommendation'])
        self.assertEqual(d['result']['recommendation']['positions'], [{'shares': 100}])
        d = self.decision()
        d['result']['recommendation']['candidate_ranking'] = []
        self.assertNotIn('relative_recommendations', _with_relative_candidates(d)['result']['recommendation'])


class CachedResearchPoolTests(unittest.TestCase):
    def test_liquidity_cap_and_max_holdings_do_not_remove_research_candidates(self):
        from unittest.mock import MagicMock, patch
        from app import quant_portfolio as qp
        for count in (1, 35):
            candidates = [{"symbol": f"{i:06d}", "name": f"company{i}"} for i in range(count)]
            conn = MagicMock()
            conn.execute.side_effect = [
                MagicMock(fetchall=lambda: [{"asset_symbol": x["symbol"], "rows": 500,
                                            "data_start": "2024-01-01", "data_end": "2026-09-09"}
                                           for x in candidates]),
                MagicMock(fetchall=lambda: [{"asset_symbol": x["symbol"], "liquidity_amount": i}
                                           for i, x in enumerate(candidates)]),
                MagicMock(fetchone=lambda: ["2026-09-09"]),
            ]
            with patch.object(qp, "connect", return_value=conn), patch.object(
                qp, "_resolve_sectors", return_value={"codes": ["T1"]}
            ), patch.object(qp, "_sector_candidates", return_value=candidates):
                result = qp._cached_candidate_pool({"stocks": [], "sectors": ["test"],
                    "max_positions": 8, "max_candidates": 2, "backtest_window_years": 1}, "2026-09-09")
            self.assertEqual(len(result["candidates"]), count)
