"""Dated evidence stays separated in Markdown and model-facing summaries."""
import copy
import json
import unittest

from app.llm import build_summary
from app.official_context import build_official_context
from app.reporting import render_markdown, report_identity
from app.sentiment import build_sentiment
from test_official_context import DATE, FakeProvider, payloads, stock


class OfficialExportTests(unittest.TestCase):
    def report(self):
        data = payloads()
        data['all']['stock_items'] = [stock(days=1, net_value=123_000_000),
                                     stock(days=3, net_value=-42_000_000)]
        data['all']['count'] = 2
        data['all']['stock_count'] = 1
        context = build_official_context(FakeProvider(data), DATE)
        row = {'thscode':'000001.SZ', 'name':'测试证券', 'seal_money':25, 'max_seal_money':100,
               'limit_up_reason':'官方<原因>|原文', 'continue_day_text':'首板'}
        report = {'date':DATE, 'mode':'live', 'status':'partial', 'official_context':context,
                  'limit_up':{'rows':[copy.deepcopy(row)],'count':1},
                  'raw':{'calendar':[DATE], 'pools_by_date':{DATE:[row]}}}
        report['sentiment'] = build_sentiment(report)
        return report

    def test_markdown_preserves_periods_amount_units_and_escaped_reason(self):
        report = self.report()
        before = report_identity(report)
        text = render_markdown(report)
        self.assertIn('#### 1日榜', text)
        self.assertIn('#### 3日榜', text)
        self.assertIn('1.23 亿元 | 1 | 测试原因', text)
        self.assertIn('-0.42 亿元 | 3 | 测试原因', text)
        self.assertIn('官方&lt;原因&gt;&#124;原文', text)
        self.assertIn('中位数 25%', text)
        self.assertIn('不是全市场或板块主力资金流', text)
        self.assertEqual(before, report_identity(report))

    def test_llm_whitelist_keeps_dates_units_periods_and_anomalies(self):
        report = self.report()
        report['official_context']['raw'] = {'api_key':'private-sentinel'}
        board = report['official_context']['dragon_tiger']['all']
        board['groups']['one_day'][0]['api_key'] = 'private-sentinel'
        report['sentiment']['retention']['unknown_sensitive'] = 'private-sentinel'
        value = build_summary({'mode':'live', 'review':report})
        self.assertNotIn('private-sentinel', json.dumps(value))
        self.assertEqual(value['sentiment']['date'], DATE)
        self.assertIn('1', value['sentiment']['matrix']['rows'][0]['buckets'])
        self.assertEqual(value['sentiment']['retention']['median_pct'], 25)
        observed = value['official_observations']
        self.assertEqual(observed['date'], DATE)
        self.assertEqual(observed['benchmark']['mean_auction_pct'], .35)
        self.assertEqual(observed['dragon_tiger']['all']['groups']['three_day'][0]['range_days'], 3)
        self.assertEqual(observed['dragon_tiger']['all']['groups']['one_day'][0]['change_pct'], 2.5)
        self.assertTrue(observed['dragon_tiger']['all']['timestamp_matches_date'])

    def test_llm_truncation_keeps_reported_counts_and_never_mutates_saved_context(self):
        report = self.report()
        board = report['official_context']['dragon_tiger']['all']
        board['groups']['one_day'] = board['groups']['one_day'] * 30
        board['received_rows'] = 31
        board['reported_count'] = 31
        value = build_summary({'review':report})['official_observations']['dragon_tiger']['all']
        self.assertEqual(len(value['groups']['one_day']), 10)
        self.assertEqual(value['shown_count'], 11)
        self.assertEqual(value['reported_count'], 31)
        self.assertTrue(value['truncated'])
        self.assertEqual(len(board['groups']['one_day']), 30)

    def test_legacy_sentiment_view_is_allowed_without_changing_report(self):
        report = self.report()
        view = report.pop('sentiment')
        value = build_summary({'review':report,'review_sentiment':view})
        self.assertEqual(value['sentiment']['date'], DATE)
        self.assertNotIn('sentiment', report)


if __name__ == '__main__':
    unittest.main()
