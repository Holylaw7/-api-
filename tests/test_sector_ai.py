"""Targeted sector evidence remains bounded, dated and separate from raw data."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.llm import analyze, build_summary
from app.report_library import ReportLibrary
from app.reporting import render_markdown, report_identity


def fixture():
    member = {'thscode': '000001.SZ', 'name': '测试成分', 'price': 12.5,
              'price_change_ratio_pct': 0, 'turnover': None, 'volume': 200,
              'source': 'report_market', 'date_basis': 'inferred_latest_closed_session',
              'date_verified': False, 'data_status': 'provisional', 'limit_up': False,
              'consecutive_days': None, 'consecutive_lower_bound': False}
    board = {
        'thscode': '886001.TI', 'name': 'PCB测试概念', 'category': 'cn_concept',
        'status': 'partial', 'membership_basis': 'current_members_view',
        'members_as_of': '2026-09-20T16:00:00+08:00', 'member_count': 80,
        'quote_scope': {'source': 'report_market', 'selection': 'current_members',
                        'max_stock_quotes': 100, 'max_historical_per_board': 10},
        'coverage': {'membership_complete': True, 'requested_count': 80, 'quoted_count': 45,
                     'quote_coverage_pct': 56.25, 'shown_count': 45, 'truncated': False,
                     'limit_pool_complete': True, 'limit_up_shown_count': 35,
                     'limit_up_truncated': False},
        'price_trend': {'status': 'partial', 'as_of': '2026-09-18',
                        'adjust': 'not_applicable_index', 'close': 100, 'change_pct': 0,
                        'ma5': None, 'warnings': ['均线周期不足']},
        'statistics': {'total_members': 80, 'quoted_count': 45, 'valid_change_count': 45, 'advancing': 20, 'declining': 20,
                       'unchanged': 5, 'mean_change_pct': 0, 'median_change_pct': 0,
                       'turnover_sum': None, 'turnover_known_count': 0,
                       'limit_up_count': 35, 'consecutive_count': 3, 'consecutive_known_count': 30},
        'members': [dict(member, thscode=f'{index:06}.SZ') for index in range(45)],
        'limit_up_members': [{'thscode': f'{index:06}.SZ', 'name': '测试涨停成员',
                              'seal_money': 0, 'max_seal_money': None,
                              'seal_retention_pct': None, 'limit_up_time': '09:25',
                              'limit_up_reason': '官方原因原文', 'consecutive_days': 2,
                              'consecutive_lower_bound': True} for index in range(35)],
        'warnings': ['当前成分不是历史成分'], 'net_flow': None, 'net_flow_status': 'unavailable',
    }
    return {
        'mode': 'live', 'review': {'date': '2026-09-18', 'sectors': {
            'rows': [{'thscode': f'881{index:03}.TI', 'name': '其他行业'} for index in range(60)]}},
        'sector_research': {
            'date': '2026-09-18', 'mode': 'live', 'status': 'partial',
            'review_id': 'report-fixture', 'evidence_id': 'evidence-fixture',
            'generated_at': '2026-09-20T16:00:00+08:00', 'definition': '官方指定板块证据',
            'coverage': {'business_calls': 3, 'member_calls': 1, 'quote_code_count': 45},
            'warnings': ['只能核实取得的样本'], 'boards': [board],
        },
    }


class SectorSummaryTests(unittest.TestCase):
    def test_targeted_board_survives_general_top_list_and_preserves_date_and_missing(self):
        result = build_summary(fixture())
        self.assertEqual(len(result['sectors_top30']['rows']), 60)
        self.assertNotIn('886001.TI', str(result['sectors_top30']))
        evidence = result['targeted_sectors']
        board = evidence['boards'][0]
        self.assertEqual(board['thscode'], '886001.TI')
        self.assertEqual(evidence['date'], '2026-09-18')
        self.assertEqual(board['members_as_of'], '2026-09-20T16:00:00+08:00')
        self.assertEqual(board['membership_basis'], 'current_members_view')
        self.assertIs(board['members'][0]['date_verified'], False)
        self.assertIsNone(board['net_flow'])
        self.assertIsNone(board['members'][0]['turnover'])
        self.assertEqual(board['members'][0]['price_change_ratio_pct'], 0)
        self.assertEqual(board['price_trend']['adjust'], 'not_applicable_index')

    def test_separate_explicit_allowlists_reject_unknown_and_nested_containers(self):
        state = fixture()
        evidence = state['sector_research']
        board = evidence['boards'][0]
        for record in (evidence, evidence['coverage'], board, board['coverage'],
                       board['quote_scope'], board['statistics'], board['price_trend'],
                       board['members'][0], board['limit_up_members'][0]):
            record.update(raw={'thscode': 'raw-private'}, config={'name': 'config-private'},
                          api_key='key-private', unknown_field='future-private')
        board['quote_scope']['source'] = {'name': 'container-private'}
        board['warnings'].append({'name': 'warning-private'})
        board['statistics']['median_change_pct'] = float('nan')
        serialized = json.dumps(build_summary(state), ensure_ascii=False, allow_nan=False)
        for marker in ('raw-private', 'config-private', 'key-private', 'future-private',
                       'container-private', 'warning-private'):
            self.assertNotIn(marker, serialized)
        board_result = build_summary(state)['targeted_sectors']['boards'][0]
        self.assertIsNone(board_result['statistics']['median_change_pct'])
        self.assertNotIn('source', board_result['quote_scope'])

    def test_each_list_cap_keeps_complete_counts_and_explicit_truncation(self):
        state = fixture()
        state['sector_research']['boards'] *= 4
        for board in state['sector_research']['boards']:
            board['members'] = board['members'] * 2          # 90 rows > the 60-row export cap
            board['limit_up_members'] = board['limit_up_members'] * 3   # 105 rows > the cap
        result = build_summary(state)['targeted_sectors']
        self.assertEqual(result['available_board_count'], 4)
        self.assertEqual(result['shown_board_count'], 3)
        self.assertIs(result['boards_truncated'], True)
        for board in result['boards']:
            self.assertLessEqual(len(board['members']), 60)
            self.assertLessEqual(len(board['limit_up_members']), 60)
            self.assertEqual(board['coverage']['shown_count'], len(board['members']))
            self.assertEqual(board['coverage']['limit_up_shown_count'], len(board['limit_up_members']))
            self.assertIs(board['coverage']['truncated'], True)
            self.assertIs(board['coverage']['limit_up_truncated'], True)
            self.assertEqual(board['member_count'], 80)
            self.assertEqual(board['coverage']['quoted_count'], 45)
            self.assertEqual(board['statistics']['limit_up_count'], 35)
            self.assertEqual(board['statistics']['consecutive_known_count'], 30)
            self.assertEqual(board['statistics']['total_members'], 80)
            self.assertEqual(board['statistics']['quoted_count'], 45)

    def test_bounded_lists_use_the_sixty_row_cap_before_any_recap(self):
        state = fixture()
        board = state['sector_research']['boards'][0]
        board['members'] = board['members'] * 2          # 90 rows > the 60-row export cap
        board['limit_up_members'] = board['limit_up_members'] * 3   # 105 rows > the cap
        summary = build_summary(state)
        self.assertLessEqual(len(json.dumps(summary, ensure_ascii=False)), 100_000)
        exported = summary['targeted_sectors']['boards'][0]
        self.assertEqual(len(exported['members']), 60)
        self.assertEqual(len(exported['limit_up_members']), 60)
        self.assertEqual(exported['coverage']['shown_count'], 60)
        self.assertEqual(exported['coverage']['limit_up_shown_count'], 60)
        self.assertIn('最多60项', summary['scope_note'])
        self.assertNotIn('进一步限制', summary['scope_note'])

    def test_preexisting_upstream_truncation_is_not_cleared_by_short_sample(self):
        state = fixture()
        board = state['sector_research']['boards'][0]
        board['members'] = board['members'][:1]
        board['limit_up_members'] = board['limit_up_members'][:1]
        board['coverage'].update(truncated=True, limit_up_truncated=True)
        result = build_summary(state)['targeted_sectors']['boards'][0]
        self.assertEqual(result['coverage']['shown_count'], 1)
        self.assertIs(result['coverage']['truncated'], True)
        self.assertIs(result['coverage']['limit_up_truncated'], True)

    def test_summary_does_not_mutate_source_or_create_evidence_for_absent_data(self):
        state = fixture()
        original = copy.deepcopy(state)
        build_summary(state)
        self.assertEqual(state, original)
        self.assertIsNone(build_summary({'mode': 'live'})['targeted_sectors'])
        self.assertEqual(build_summary({'sector_research': {'boards': 'bad'}})['targeted_sectors']['boards'], [])

    def test_analyze_sends_selected_evidence_once_and_does_not_send_model_key(self):
        config = {'provider': 'deepseek', 'model': 'fixture-model',
                  'base_url': 'https://example.test', 'api_key': 'model-secret-fixture'}
        with patch('app.llm.complete', return_value={'text': '研究结果'}) as complete:
            self.assertEqual(analyze(config, fixture(), '分析PCB'), '研究结果')
        complete.assert_called_once()
        messages = complete.call_args.args[1]
        self.assertIn('当前成员的历史表现不等于历史成分', messages[0]['content'])
        self.assertIn('覆盖不完整不能断言整个方向无资金或无涨停', messages[0]['content'])
        self.assertIn('targeted_sectors', messages[1]['content'])
        self.assertIn('886001.TI', messages[1]['content'])
        self.assertNotIn('model-secret-fixture', json.dumps(messages))


class SectorArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = ReportLibrary(None, self.temp.name)
        self.report = {'date': '2026-09-18', 'mode': 'live', 'status': 'partial',
                       'generated_at': '2026-09-18T15:10:00+08:00'}
        self.ai = {'provider': 'deepseek', 'label': 'DeepSeek', 'model': 'fixture',
                   'text': '按指定板块证据核验', 'review_date': self.report['date'],
                   'question': '分析PCB板块',
                   'review_id': report_identity(self.report), 'mode': 'live', 'scope': 'review',
                   'generated_at': '2026-09-20T16:02:00+08:00',
                   'sector_evidence_id': 'evidence-fixture', 'sector_codes': ['886001.TI'],
                   'sector_generated_at': '2026-09-20T16:00:00+08:00'}

    def test_archive_roundtrip_keeps_only_provenance_whitelist(self):
        record = {**self.ai, 'raw': 'private-raw', 'api_key': 'private-key',
                  'config': {'model': 'private-config'}, 'boards': ['unrequested-evidence']}
        self.library.save_ai(record)
        loaded = self.library.load_ai(self.report)['deepseek']
        self.assertEqual(loaded, self.ai)
        saved = (Path(self.temp.name) / 'ai-reviews' / 'live' / '2026-09-18-deepseek.json').read_text(encoding='utf-8')
        for marker in ('private-raw', 'private-key', 'private-config', 'unrequested-evidence'):
            self.assertNotIn(marker, saved)

    def test_markdown_displays_evidence_identity_codes_and_time(self):
        text = render_markdown(self.report, ai=self.ai)
        self.assertIn('指定板块补充证据编号：evidence-fixture', text)
        self.assertIn('板块代码：886001.TI', text)
        self.assertIn('取数时间：2026-09-20T16:00:00+08:00', text)
        self.assertIn('当前成员回看不代表历史成分', text)
        self.assertIn('本次问题：分析PCB板块', text)

    def test_provenance_and_model_text_are_escaped(self):
        malicious = {**self.ai, 'sector_evidence_id': '<script>bad</script>\n# 标题',
                     'sector_codes': ['[假链接](https://bad.test)'],
                     'sector_generated_at': '<img src=x onerror=bad>',
                     'question': '<script>bad question</script>\n# 用户问题',
                     'text': '<iframe src=x></iframe>\n# 模型标题'}
        text = render_markdown(self.report, ai=malicious)
        self.assertNotIn('<script>', text)
        self.assertNotIn('<img ', text)
        self.assertNotIn('<iframe ', text)
        self.assertNotIn('\n# 标题', text)
        self.assertNotIn('[假链接](', text)
        self.assertIn('&lt;script&gt;', text)

    def test_archived_question_is_bounded_and_does_not_accept_nested_data(self):
        self.library.save_ai({**self.ai, 'question': '研究' * 2000})
        self.assertEqual(len(self.library.load_ai(self.report)['deepseek']['question']), 2000)
        self.library.save_ai({**self.ai, 'question': {'api_key': 'nested-question-private'}})
        self.assertIsNone(self.library.load_ai(self.report)['deepseek']['question'])

    def test_provenance_cannot_bypass_report_identity_or_scope_checks(self):
        for key, invalid in (('scope', 'market'), ('review_id', 'another-version'),
                             ('review_date', '2026-09-17'), ('mode', 'demo')):
            with self.subTest(key=key), self.assertRaises(ValueError):
                render_markdown(self.report, ai={**self.ai, key: invalid})
        self.library.save_ai(self.ai)
        revised = {**self.report, 'generated_at': '2026-09-18T15:11:00+08:00'}
        self.assertEqual(self.library.load_ai(revised), {})


if __name__ == '__main__':
    unittest.main()
