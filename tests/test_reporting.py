import copy
import unittest

from app.reporting import render_markdown, report_identity


def report_fixture():
    return {
        'date': '2026-09-18', 'mode': 'live', 'status': 'partial',
        'generated_at': '2026-09-19T16:00:00+08:00',
        'completed_at': '2026-09-19T16:01:00+08:00', 'source': '同花顺 Financial API',
        'warnings': ['市场行情部分缺失'],
        'market': {
            'status': 'partial', 'data_status': 'provisional', 'snapshot_date': '2026-09-19',
            'snapshot_timestamp': 1789804800000, 'effective_trade_date': '2026-09-18',
            'date_basis': 'inferred_latest_closed_session', 'date_verified': False,
            'total': 100, 'valid_count': 99, 'missing_change': 1, 'advancing': 60,
            'declining': 39, 'unchanged': 0, 'total_turnover': 0,
            'turnover_known_count': 99, 'turnover_coverage_pct': 99,
            'limit_down_count': 0, 'limit_break_count': None, 'seal_rate_pct': None,
        },
        'limit_up': {
            'count': 1, 'consecutive_count': 1, 'max_consecutive': 2,
            'method': '早封、封单留存、封单额分位、严格连板',
            'promotion': {'previous_count': 0, 'promoted_count': 0, 'rate_pct': None,
                          'by_height': [{'height': 1, 'previous_count': 0, 'promoted_count': 0,
                                         'rate_pct': None, 'lower_bound_count': 0}]},
            'rows': [{'thscode': '000001.SZ', 'name': '平安测试', 'score': 0,
                      'consecutive_days': 2, 'consecutive_lower_bound': True,
                      'factor_coverage_pct': 75, 'limit_up_time': '09:35',
                      'seal_money': 0, 'max_seal_money': None,
                      'factors': {'seal_retention': None}, 'quality': ['连板为可验证下限']}],
        },
        'trend': {
            'rows': [{'date': '2026-09-18', 'limit_up_count': 1, 'consecutive_count': 1,
                      'max_consecutive': 2, 'promoted_count': 0, 'promotion_rate_pct': None,
                      'status': 'ready'}],
            'price_leaders': [{'thscode': '000001.SZ', 'name': '平安测试', 'as_of': '2026-09-18',
                              'close': 10, 'ma5': 9, 'ma10': 8, 'ma20': None, 'return_5d_pct': 0,
                              'volume_ratio_5d': 0, 'max_drawdown_20d_pct': 0,
                              'status': 'partial', 'warnings': ['二十日周期缺失']}],
            'price_coverage': {'requested': 1, 'available': 1, 'fully_computed': 0,
                               'start': '2026-07-21', 'end': '2026-09-18'},
        },
        'sectors': {
            'status': 'partial', 'data_status': 'provisional', 'snapshot_dates': ['2026-09-19'],
            'effective_trade_date': '2026-09-18', 'date_basis': 'inferred_latest_closed_session',
            'date_verified': False,
            'coverage': {'ranked_count': 1, 'catalog_count': 2, 'member_attribution_count': 1,
                         'member_attribution_limit': 30, 'inferred_date_count': 1, 'historical_sample': False},
            'rows': [{'thscode': '881001.TI', 'name': '测试板块', 'score': 0,
                      'price_change_ratio_pct': 0, 'turnover': 0, 'limit_up_count': 0,
                      'consecutive_count': 0, 'factor_coverage_pct': 100,
                      'effective_trade_date': '2026-09-18', 'date_basis': 'inferred_latest_closed_session',
                      'quality': ['板块日期推断需核验']}],
        },
        'raw': {'private': 'raw-evidence-never-exported'},
    }


class ReportingTests(unittest.TestCase):
    def test_report_preserves_missing_and_zero_without_nan(self):
        report = report_fixture()
        report['trend']['price_leaders'][0]['ma20'] = float('nan')
        text = render_markdown(report)
        self.assertIn('跌停 **0** 只，炸板池 **缺失**', text)
        self.assertIn('上涨 60、下跌 39、平盘 0', text)
        self.assertIn('有效成交额合计 0 亿元', text)
        self.assertIn('| 平安测试 | 000001.SZ | 0 | 至少 2 | 75%', text)
        self.assertIn('| 平安测试 | 000001.SZ | 2026-09-18 | 10 | 9 | 8 | 缺失 | 0% | 0 | 0% | partial |', text)
        self.assertNotIn('| nan |', text)

    def test_dates_coverage_and_all_quality_are_preserved(self):
        text = render_markdown(report_fixture())
        self.assertIn('2026-09-19', text)
        self.assertIn('2026-09-18', text)
        self.assertIn('休市日推断归属最近收盘日（未独立核实）', text)
        self.assertIn('原始时间戳（毫秒）', text)
        self.assertIn('全市场行情覆盖 99/100', text)
        self.assertIn('板块有效排名 1/2', text)
        self.assertIn('完整计算 0', text)
        self.assertIn('二十日周期缺失', text)
        self.assertIn('板块日期推断需核验', text)
        self.assertIn('**不代表主力净资金流入**', text)

    def test_demo_is_unmistakable_and_empty_report_is_not_zero(self):
        text = render_markdown({'date': '2026-09-18', 'mode': 'demo'})
        self.assertIn('**DEMO 演示报告：合成数据', text)
        self.assertIn('涨停 **缺失**', text)
        self.assertIn('不等同于当日数量为零', text)
        self.assertNotIn('模式：实盘来源', text)

    def test_external_text_cannot_inject_markdown_html_or_table_rows(self):
        report = report_fixture()
        report['limit_up']['rows'][0]['name'] = '股票|第二列\n# 伪造标题\n<script>alert(1)</script>[点我](https://bad.test)'
        report['warnings'].append('<img src=x onerror=evil>\n## 假结论')
        text = render_markdown(report)
        self.assertIn('股票&#124;第二列 / \\# 伪造标题', text)
        self.assertIn('&lt;script&gt;', text)
        self.assertNotIn('<script>', text)
        self.assertNotIn('<img', text)
        self.assertNotIn('\n# 伪造标题', text)
        self.assertNotIn('\n## 假结论', text)
        self.assertNotIn('[点我](', text)

    def test_all_warning_rows_survive_display_caps(self):
        report = report_fixture()
        report['sectors']['rows'] = [dict(report['sectors']['rows'][0], thscode=f'881{index:03}.TI', quality=[]) for index in range(35)]
        report['sectors']['rows'][-1]['quality'] = ['排在表格之后的缺失也必须保留']
        text = render_markdown(report)
        self.assertIn('排在表格之后的缺失也必须保留', text)
        self.assertNotIn('| 测试板块 | 881034.TI |', text)
        self.assertIn('881034.TI', text)

    def test_raw_configs_and_credentials_are_not_rendered_or_fingerprinted(self):
        report = report_fixture()
        before = report_identity(report)
        report['raw']['nested'] = {'api_key': 'raw-key-secret'}
        report['config'] = {'api_key': 'private-config-secret'}
        report['api_key'] = 'top-key-secret'
        report['market']['raw'] = {'anything': 'nested-raw-secret'}
        report['market']['credentials'] = {'key': 'nested-key-secret'}
        self.assertEqual(before, report_identity(report))
        text = render_markdown(report)
        for secret in ('raw-evidence-never-exported', 'raw-key-secret', 'private-config-secret',
                       'top-key-secret', 'nested-raw-secret', 'nested-key-secret'):
            self.assertNotIn(secret, text)

    def test_identity_is_canonical_and_changes_with_public_evidence(self):
        report = report_fixture()
        original = report_identity(report)
        reordered = dict(reversed(list(report.items())))
        self.assertEqual(original, report_identity(reordered))
        report['review_id'] = original
        report['report_id'] = original
        report['ai'] = {'text': 'analysis'}
        report['comparison'] = {'status': 'partial'}
        self.assertEqual(original, report_identity(report))
        report['market']['date_verified'] = True
        self.assertNotEqual(original, report_identity(report))
        self.assertEqual(report_identity({'date': '2026-09-18'}), report_identity({'date': '2026-09-18', 'mode': 'live'}))

    def test_ai_only_attaches_to_exact_report_and_review_scope(self):
        report = report_fixture()
        ai = {'review_id': report_identity(report), 'review_date': report['date'], 'scope': 'review',
              'mode': 'live', 'text': '# AI结论\n<script>bad</script>\n继续核验',
              'provider': 'deepseek', 'label': 'DeepSeek', 'model': 'test-model', 'generated_at': '2026-09-19T17:00:00+08:00'}
        text = render_markdown(report, ai=ai)
        self.assertIn('## AI 辅助研判（对应本报告版本）', text)
        self.assertIn('> \\# AI结论', text)
        self.assertIn('> &lt;script&gt;', text)
        for key, value in [('review_id', 'other'), ('review_date', '2026-09-17'),
                           ('mode', 'demo'), ('scope', 'market')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                render_markdown(report, ai={**ai, key: value})
        revised = copy.deepcopy(report)
        revised['generated_at'] = '2026-09-19T17:00:00+08:00'
        with self.assertRaises(ValueError):
            render_markdown(revised, ai=ai)

    def test_render_is_deterministic_and_does_not_mutate_report(self):
        report = report_fixture()
        original = copy.deepcopy(report)
        self.assertEqual(render_markdown(report), render_markdown(report))
        self.assertEqual(report, original)

    def test_invalid_source_encoding_is_marked_without_guessing_name(self):
        report = report_fixture()
        report['limit_up']['rows'][0]['name'] = '坏\ufffd名称'
        text = render_markdown(report)
        self.assertIn('源文本存在替代字符', text)
        self.assertIn('坏\ufffd名称', text)
        self.assertIn('000001.SZ', text)

    def test_comparison_preserves_dates_quality_and_neutral_pool_labels(self):
        report = report_fixture()
        comparison = {
            'status': 'partial', 'mode': 'live', 'current_date': report['date'],
            'previous_date': '2026-09-16', 'adjacent_sessions': False,
            'warnings': ['不是相邻交易日'],
            'market': {'rows': [{'label': '上涨家数', 'current': 60, 'previous': None,
                                'delta': None, 'unit': '家', 'status': 'unavailable'}]},
            'limit_up': {'current_count': 1, 'previous_count': 0, 'retained_count': 0,
                         'new_count': 1, 'exited_count': 0,
                         'new': [{'thscode': '000001.SZ', 'name': '平安测试',
                                  'current': {'score': 0, 'consecutive_days': 2, 'consecutive_lower_bound': True},
                                  'previous': None}]},
            'sectors': {'coverage': {'common_count': 1, 'comparable_count': 1, 'returned_count': 1},
                        'warnings': ['日期推断只作暂定比较'],
                        'rows': [{'thscode': '881001.TI', 'name': '测试板块', 'current_rank': 1,
                                  'previous_rank': 3, 'rank_change': 2, 'current_change_pct': 2,
                                  'previous_change_pct': 1, 'change_delta_pp': 1,
                                  'turnover_change_pct': 0, 'status': 'provisional'}]},
        }
        text = render_markdown(report, comparison=comparison)
        self.assertIn('当期 2026-09-18，基准 2026-09-16', text)
        self.assertIn('未确认相邻交易日', text)
        self.assertIn('从对比池退出 0 只', text)
        self.assertIn('日期推断只作暂定比较', text)
        self.assertIn('| 上涨家数 | 60 | 缺失 | 缺失 | 家 | unavailable |', text)
        self.assertIn('| 000001.SZ | 平安测试 | 0 | 缺失 | 至少 2 | 缺失 |', text)
        for key, value in [('current_date', '2026-09-17'), ('mode', 'demo')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                render_markdown(report, comparison={**comparison, key: value})


if __name__ == '__main__':
    unittest.main()
