"""Pure, auditable Markdown exports. No filesystem, network or credential reads.

The report fingerprint covers public evidence, not large raw payloads or export
metadata. An optional AI appendix must identify that exact dated report.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import re

from .sentiment import build_sentiment


_PRIVATE_KEYS = frozenset({
    'raw', 'config', 'credentials', 'api_key', 'apikey', 'api-key',
    'authorization', 'access_token', 'token', 'password', 'secret',
    'report_id', 'review_id', 'exported_at', 'export_path', 'ai', 'comparison',
})
_TOP_KEYS = frozenset({
    'date', 'generated_at', 'completed_at', 'source', 'status', 'warnings',
    'mode', 'market', 'limit_up', 'ladder', 'trend', 'sectors',
    'validation_note', 'sentiment', 'official_context', 'enriched_at',
})
_DATE_BASIS = {
    'inferred_latest_closed_session': '休市日推断归属最近收盘日（未独立核实）',
    'snapshot_timestamp': '接口快照日期',
    'historical_bar_date': '历史日线日期',
    'unavailable': '无法确认日期',
}


def _public(value):
    """Canonical JSON-compatible evidence with recursive private-key removal."""
    if isinstance(value, dict):
        return {str(key): _public(item) for key, item in value.items()
                if isinstance(key, str) and key.lower() not in _PRIVATE_KEYS}
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if isinstance(value, (str, int, float, bool)) or value is None else None


def report_identity(report: dict) -> str:
    """Stable identity of the dated, public report, independent of raw storage.

    Missing mode is legacy live. Export/AI/comparison metadata is excluded so
    attaching the identity or exporting again does not change it. Generated and
    completed timestamps ARE included: two runs on the same day are different.
    """
    if not isinstance(report, dict):
        raise ValueError('复盘报告格式不正确')
    public = _public(report)
    public['mode'] = report.get('mode') or 'live'
    body = json.dumps(public, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def _dict(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _number(value):
    return value if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) else None


def _fmt(value, suffix=''):
    number = _number(value)
    if number is None:
        return '缺失'
    return f'{number:,.2f}'.rstrip('0').rstrip('.') + suffix


def _money(value):
    number = _number(value)
    return _fmt(number / 100_000_000, ' 亿元') if number is not None else '缺失'


def _text(value):
    """Escape untrusted inline Markdown/HTML; never let text add table rows."""
    if value is None or value == '':
        return '缺失'
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, (int, float)):
        return _fmt(value)
    if not isinstance(value, str):
        return '缺失'
    text = re.sub(r'[\r\n\t]+', ' / ', str(value))
    text = ''.join(char for char in text if ord(char) >= 32)
    text = html.escape(text, quote=True).replace('\\', '\\\\')
    text = re.sub(r'([`*_\[\]#!~])', r'\\\1', text)
    return text.replace('|', '&#124;')


def _table(headers, rows):
    rows = list(rows)
    if not rows:
        return ['暂无可用记录；不等同于当日数量为零。']
    return ['| ' + ' | '.join(headers) + ' |', '|' + '|'.join(['---'] * len(headers)) + '|'] + [
        '| ' + ' | '.join(_text(value) for value in row) + ' |' for row in rows]


def _consecutive(row):
    value = _number(row.get('consecutive_days'))
    if value is None:
        return '缺失'
    return ('至少 ' if row.get('consecutive_lower_bound') else '') + _fmt(value)


def _quality(value):
    return '；'.join(str(item) for item in value) if isinstance(value, list) and value else '无附加提示'


def _warnings(report):
    """Every public warning/quality entry, including rows below displayed top N."""
    collected = []

    def walk(value, path):
        if isinstance(value, dict):
            label = value.get('thscode') or value.get('date')
            location = f'{path} [{label}]' if label is not None else path
            for key, item in value.items():
                if not isinstance(key, str) or key.lower() in _PRIVATE_KEYS:
                    continue
                if key in ('warnings', 'quality') and isinstance(item, list):
                    collected.extend((location, warning if isinstance(warning, str) else '来源提示格式异常，请核对原始数据') for warning in item)
                elif key == 'validation_note' and isinstance(item, str) and item:
                    collected.append((location, item))
                else:
                    walk(item, f'{location}.{key}')
        elif isinstance(value, list):
            for item in value:
                walk(item, path)

    walk({key: value for key, value in report.items() if key in _TOP_KEYS}, '报告')
    unique = list(dict.fromkeys(collected))
    if '\ufffd' in json.dumps({key: _public(value) for key, value in report.items() if key in _TOP_KEYS}, ensure_ascii=False):
        unique.insert(0, ('源文本', '源文本存在替代字符，请按股票代码核对原始数据；报告不猜测修复证券名称或来源说明。'))
    return unique


def _alignment(label, value):
    return [label, value.get('status'), value.get('data_status'),
            value.get('snapshot_date') or ('、'.join(str(item) for item in (value.get('snapshot_dates') or [])) or None),
            value.get('snapshot_timestamp'),
            value.get('effective_trade_date'), _DATE_BASIS.get(value.get('date_basis'), value.get('date_basis')),
            value.get('date_verified')]


def _observation_lines(limits):
    lines = ['', '## 下一交易日观察清单', '',
             '以下仅据本报告已取得证据整理；下一交易日日期由交易日历确认。未回测，不输出买卖指令或未来涨停概率。', '',
             '- 开始观察前核对上一交易日、关注池、缺失项和终态标记；不要把本报告盘后结果回填成早盘已知数据。',
             '- 09:15–09:20 观察可撤单阶段；09:20 后对照价格动量与已观察金额留存，09:25 后核对终态。',
             '- 下表只是当日涨停强度榜的前 10 只研究对象；未自动加入自选，也不是全市场最优标的。', '']
    lines += _table(['代码', '股票', '已知证据', '下一次需核验'], [
        [row.get('thscode'), row.get('name'),
         f"当日强度 {_fmt(row.get('score'))}；严格连板 {_consecutive(row)}；因子覆盖 {_fmt(row.get('factor_coverage_pct'), '%')}",
         '竞价覆盖、09:20 后动量/金额留存、终态、所属板块同步性']
        for row in _rows(limits.get('rows'))[:10]])
    return lines


def _ai_lines(report, ai):
    if ai is None:
        return []
    if not isinstance(ai, dict) or any((
        ai.get('review_id') != report_identity(report),
        ai.get('review_date') != report.get('date'),
        ai.get('mode') != (report.get('mode') or 'live'),
        ai.get('scope') != 'review',
    )):
        raise ValueError('AI 分析与这份复盘的日期、模式或报告版本不匹配，请重新分析后保存')
    text = ai.get('text')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('没有可附加的 AI 分析正文')
    lines = ['', '## AI 辅助研判（对应本报告版本）', '',
             f"服务商：{_text(ai.get('label') or ai.get('provider'))}；模型：{_text(ai.get('model'))}；生成时间：{_text(ai.get('generated_at'))}。", '',
             '以下是模型解释，尚未独立验证；不修改上述数据、评分或数据边界。', '']
    # Treat the model output as quoted text, never executable HTML or Markdown.
    lines.extend('> ' + (_text(line) if line else '') for line in text.splitlines())
    return lines


def _sentiment_lines(value):
    matrix, retention, reasons = (_dict(value.get(key)) for key in ('matrix', 'retention', 'reasons'))
    lines = ['', '## 情绪结构观察', '', _text(value.get('definition')), '',
             '### 近十日连板梯队矩阵', '', _text(matrix.get('definition')), '']
    lines += _table(['交易日', '涨停数', '1板', '2板', '3板', '4板', '5板及以上', '高度未知', '高度仅为下限', '状态'], [
        [row.get('date'), row.get('limit_up_count'), *[_dict(row.get('buckets')).get(key) for key in ('1','2','3','4','5+','unknown')],
         row.get('lower_bound_count'), row.get('status')]
        for row in _rows(matrix.get('rows'))])
    lines += ['', '下限高度暂列入已观察到的梯队；不能据此认定最终连板高度。缺失池不是零涨停。', '',
              '### 封单留存分布', '', _text(retention.get('definition')), '',
              f"有效样本 {_fmt(retention.get('valid_count'))}/{_fmt(retention.get('total_count'))}；覆盖 {_fmt(retention.get('coverage_pct'), '%')}；中位数 {_fmt(retention.get('median_pct'), '%')}；低于50% {_fmt(retention.get('below_50_count'))} 只（有效样本占比 {_fmt(retention.get('below_50_pct'), '%')}）。", '',
              f"缺失 {_fmt(retention.get('missing_count'))}；异常 {_fmt(retention.get('invalid_count'))}，其中超过100% {_fmt(retention.get('above_100_count'))}；代码冲突剔除 {_fmt(retention.get('excluded_conflict_count'))}。异常值不参与分布统计。", '',
              '### 官方涨停原因原文分布', '', _text(reasons.get('definition')), '',
              f"有原因 {_fmt(reasons.get('known_count'))}/{_fmt(reasons.get('total_count'))}；覆盖 {_fmt(reasons.get('coverage_pct'), '%')}；共 {_fmt(reasons.get('group_count'))} 组、展示 {_fmt(reasons.get('displayed_count'))} 组。不是行业分类或因果验证。", '']
    lines += _table(['官方原因原文', '只数', '有原因样本占比', '代码样本', '未展示代码数'], [
        [row.get('reason'), row.get('count'), _fmt(row.get('share_pct'), '%'), '、'.join(row.get('codes') or []), row.get('other_code_count')]
        for row in _rows(reasons.get('rows'))])
    for part in (value, matrix, retention, reasons):
        lines += ['- ' + _text(warning) for warning in (part.get('warnings') or [])]
    return lines


def _official_lines(value):
    if not value:
        return ['', '## 官方补充观察', '', '尚未手动补充短线竞价风向标和龙虎榜。可在本机收盘复盘页补充后重新保存。']
    lines = ['', '## 官方补充观察', '',
             f"日期 {_text(value.get('date'))}；状态 {_text(value.get('status'))}；补充时间 {_text(value.get('generated_at'))}。", '',
             _text(value.get('definition')), '']
    benchmark = _dict(value.get('benchmark'))
    lines += ['### 短线竞价风向标', '']
    if benchmark:
        lines += [_text(benchmark.get('definition')), '',
                  f"官方样本 {_fmt(benchmark.get('sample_count'))} 只；涨幅有效 {_fmt(benchmark.get('valid_auction_count'))} 只；平均 {_fmt(benchmark.get('mean_auction_pct'), '%')}；中位 {_fmt(benchmark.get('median_auction_pct'), '%')}；正涨幅占有效样本 {_fmt(benchmark.get('positive_ratio_pct'), '%')}。", '']
        lines += _table(['代码', '股票', '竞价涨幅', '官方标签'], [
            [row.get('thscode'), row.get('name'), _fmt(row.get('auction_pct'), '%'), '、'.join(row.get('tags') or [])]
            for row in _rows(benchmark.get('rows'))])
    else:
        lines += ['该分项不可用；不能视为零。']
    for key, label in (('all','全部龙虎榜'), ('org','机构龙虎榜'), ('hot_money','游资龙虎榜')):
        board = _dict(_dict(value.get('dragon_tiger')).get(key))
        lines += ['', '### ' + label, '']
        if not board:
            lines += ['该分项不可用；不能视为空榜。']
            continue
        lines += [_text(board.get('definition')), '',
                  f"交易日 {_text(board.get('trade_date'))}；上游记录数 {_fmt(board.get('reported_count'))}、股票数 {_fmt(board.get('reported_stock_count'))}；展开收到 {_fmt(board.get('received_rows'))} 条、可识别 {_fmt(board.get('observed_stock_count'))} 只股票，重复记录 {_fmt(board.get('duplicate_record_count'))} 条。每个区间最多展示30条，金额均为亿元；不加总跨榜、跨区间或重复记录。", '']
        if key == 'hot_money':
            lines += ['上游计数与按游资展开样本范围不同，不能直接用两者相除计算完整覆盖率。', '']
        if _dict(board.get('coverage')).get('definition'):
            lines += [_text(board['coverage']['definition']), '']
        for group, period in (('one_day','1日榜'), ('three_day','3日榜'), ('other','其他或未知区间')):
            rows = _rows(_dict(board.get('groups')).get(group))
            count = _dict(board.get('group_counts')).get(group)
            lines += ['', '#### ' + period, '', f"收到 {_fmt(count)} 条；展示 {len(rows)} 条。", '']
            if count == 0:
                lines += ['该区间返回空列表。']
                continue
            net = {'all':'net_value', 'org':'org_net_value', 'hot_money':'hot_money_item_net_value'}[key]
            lines += _table(['代码', '股票', '游资', '涨跌幅', '对应榜单净买入', '区间天数', '涨跌停原因', '质量'], [
                [row.get('thscode'), row.get('name'), row.get('hot_money_name'), _fmt(row.get('change_pct'), '%'),
                 _money(row.get(net)), row.get('range_days'), row.get('limit_reason'), _quality(row.get('quality'))]
                for row in rows])
    lines += ['- ' + _text(warning) for warning in (value.get('warnings') or [])]
    return lines


def render_markdown(report: dict, ai: dict | None = None, comparison: dict | None = None) -> str:
    """Render human-readable, safe Markdown without doing IO or market work."""
    if not isinstance(report, dict) or not isinstance(report.get('date'), str):
        raise ValueError('还没有可保存的有效收盘报告')
    market, limits = _dict(report.get('market')), _dict(report.get('limit_up'))
    trend, sectors = _dict(report.get('trend')), _dict(report.get('sectors'))
    promotion, sector_coverage = _dict(limits.get('promotion')), _dict(sectors.get('coverage'))
    price_coverage = _dict(trend.get('price_coverage'))
    mode = report.get('mode') or 'live'
    lines = ['# 收盘复盘 ' + _text(report['date']), '',
             '**DEMO 演示报告：合成数据，不能作为真实收盘结论。**' if mode == 'demo' else '模式：实盘来源研究报告。', '',
             '数据源：' + _text(report.get('source') or '同花顺 Financial API') + '。研究观察，非投资建议。', '',
             '生成时间：' + _text(report.get('generated_at')) + '；完成时间：' + _text(report.get('completed_at')) + '。', '',
             '报告状态：' + ('部分数据需核验' if report.get('status') == 'partial' else _text(report.get('status'))) + '。', '',
             '报告指纹：`' + report_identity(report) + '`。相同日期重新生成的报告可能有不同版本。', '',
             '## 数据边界', '',
             '缺失不当作零；部分日期可能由休市日推断，必须结合下列日期归属和覆盖阅读。', '']
    lines += _table(['模块', '状态', '数据状态', '原始快照日期', '原始时间戳（毫秒）', '有效交易日期', '日期依据', '日期已核实'],
                    [_alignment('全市场行情', market), _alignment('板块行情', sectors)])
    lines += ['', f"全市场行情覆盖 {_fmt(market.get('valid_count'))}/{_fmt(market.get('total'))}；涨跌幅缺失 {_fmt(market.get('missing_change'))} 只。成交额覆盖 {_fmt(market.get('turnover_known_count'))}/{_fmt(market.get('total'))}（{_fmt(market.get('turnover_coverage_pct'), '%')}）。", '',
              f"板块有效排名 {_fmt(sector_coverage.get('ranked_count'))}/{_fmt(sector_coverage.get('catalog_count'))}；成分归属已取 {_fmt(sector_coverage.get('member_attribution_count'))} 个，最多 {_fmt(sector_coverage.get('member_attribution_limit'))} 个；日期推断 {_fmt(sector_coverage.get('inferred_date_count'))} 个；历史固定样本：{_text(sector_coverage.get('historical_sample'))}。", '',
              f"日线覆盖：请求 {_fmt(price_coverage.get('requested'))}、目标日有效 {_fmt(price_coverage.get('available'))}、完整计算 {_fmt(price_coverage.get('fully_computed'))}；请求区间 {_text(price_coverage.get('start'))} 至 {_text(price_coverage.get('end'))}；仅强度榜前 20 名，前复权。", '',
              '### 来源警告摘要', '']
    warnings = _warnings(report)
    warning_groups = {}
    for location, message in warnings:
        warning_groups.setdefault(message, []).append(location)
    lines += ['- ' + _text(message) + f'（涉及 {len(locations)} 处记录）'
              for message, locations in warning_groups.items()] if warnings else ['- 来源未附加警告；这不表示全部字段均已取得。']
    if warnings:
        lines += ['', '同一提示合并展示；对应代码与字段位置完整保留在文末“来源提示明细”，不受前 30 名展示范围限制。']
    lines += ['', '## 盘面观察', '',
              f"涨停 **{_fmt(limits.get('count'))}** 只，其中连板 **{_fmt(limits.get('consecutive_count'))}** 只，最高 **{_fmt(limits.get('max_consecutive'))}** 板；跌停 **{_fmt(market.get('limit_down_count'))}** 只，炸板池 **{_fmt(market.get('limit_break_count'))}** 只。", '',
              f"昨日涨停池 {_fmt(promotion.get('previous_count'))} 只中，今日再次涨停 {_fmt(promotion.get('promoted_count'))} 只，延续率 **{_fmt(promotion.get('rate_pct'), '%')}**。该比例描述已经发生的结果，不是明日涨停概率。", '',
              f"上涨 {_fmt(market.get('advancing'))}、下跌 {_fmt(market.get('declining'))}、平盘 {_fmt(market.get('unchanged'))}。有效成交额合计 {_money(market.get('total_turnover'))}；成交额不是资金净流入。", '',
              f"封板比例 {_fmt(market.get('seal_rate_pct'), '%')}：涨停池数量 / 涨停池与炸板池代码并集数量，不是逐笔封板成功概率。", '',
              '## 近十日涨停与连板趋势', '', _text(trend.get('method') or '最近十个交易日完整涨停池，缺失日保持为空。'), '']
    lines += _table(['交易日', '涨停家数', '连板家数', '最高板', '再次涨停家数', '晋级率', '状态'], [
        [row.get('date'), row.get('limit_up_count'), row.get('consecutive_count'), row.get('max_consecutive'),
         row.get('promoted_count'), _fmt(row.get('promotion_rate_pct'), '%'), row.get('status')]
        for row in _rows(trend.get('rows'))[-10:]])
    lines += ['', '### 昨日梯队晋级', '', '严格连板下限单列；官方每层最多 4 只的天梯不作全市场晋级分母。', '']
    lines += _table(['昨日高度', '昨日数量', '今日再次涨停', '晋级率', '高度仅为下限的数量'], [
        [row.get('height'), row.get('previous_count'), row.get('promoted_count'), _fmt(row.get('rate_pct'), '%'), row.get('lower_bound_count')]
        for row in _rows(promotion.get('by_height'))])
    lines += ['', '## 涨停强弱前 30', '', _text(limits.get('method')), '',
              '“至少 N”表示连续记录到达窗口边界或缺失日，不能当作已确定的最终板数；涨停时间沿用接口字段，不擅自解释为首次或末次。', '']
    lines += _table(['股票', '代码', '强度分', '严格连板', '因子覆盖', '涨停时间', '封单额', '峰值封单额', '封单留存分', '质量'], [
        [row.get('name'), row.get('thscode'), row.get('score'), _consecutive(row), _fmt(row.get('factor_coverage_pct'), '%'),
         row.get('limit_up_time'), _money(row.get('seal_money')), _money(row.get('max_seal_money')),
         _dict(row.get('factors')).get('seal_retention'), _quality(row.get('quality'))]
        for row in _rows(limits.get('rows'))[:30]])
    lines += ['', '## 板块量价强度前 30', '',
              '以下为成交参与度和价格强度，**不代表主力净资金流入**。真实净资金流不可用；板块成分可重叠，不跨板块加总。', '',
              _text(sectors.get('method')), '']
    lines += _table(['板块', '代码', '强度分', '涨幅', '成交额', '涨停归属数', '连板归属数', '因子覆盖', '有效日期', '日期依据', '质量'], [
        [row.get('name'), row.get('thscode'), row.get('score'), _fmt(row.get('price_change_ratio_pct'), '%'),
         _money(row.get('turnover')), row.get('limit_up_count'), row.get('consecutive_count'), _fmt(row.get('factor_coverage_pct'), '%'),
         row.get('effective_trade_date'), _DATE_BASIS.get(row.get('date_basis'), row.get('date_basis')), _quality(row.get('quality'))]
        for row in _rows(sectors.get('rows'))[:30]])
    lines += ['', '## 重点股日线趋势', '',
              '目标日涨停强度前 20 名；前复权日线。缺失的周期指标保持为空，后续公司行动可能修订前复权历史。', '',
              _text(trend.get('price_method')), '']
    lines += _table(['股票', '代码', '日线截止日', '收盘', 'MA5', 'MA10', 'MA20', '近5日涨幅', '相对5日量能', '20日最大回撤', '状态'], [
        [row.get('name'), row.get('thscode'), row.get('as_of'), row.get('close'), row.get('ma5'), row.get('ma10'), row.get('ma20'),
         _fmt(row.get('return_5d_pct'), '%'), row.get('volume_ratio_5d'), _fmt(row.get('max_drawdown_20d_pct'), '%'), row.get('status')]
        for row in _rows(trend.get('price_leaders'))[:20]])
    lines += _sentiment_lines(report.get('sentiment') or build_sentiment({**report, 'mode':mode}))
    lines += _official_lines(_dict(report.get('official_context')))
    lines += _observation_lines(limits)
    if comparison is not None:
        lines += _comparison_lines(report, comparison)
    lines += _ai_lines(report, ai)
    if warnings:
        lines += ['', '## 来源提示明细', '']
        for message, locations in warning_groups.items():
            lines += ['- ' + _text(message), '  - 涉及位置：' + '；'.join(_text(location) for location in locations)]
    lines += ['', '## 复用', '',
              '完整因子、质量说明和原始证据保存在对应日期与模式的 JSON 报告。AI 读取时应同时读取 warnings、date_basis、date_verified、coverage；只按需读取原始证据，不读取密钥或配置。', '',
              'Markdown 仅包含公开研究结果，不包含凭据、配置和原始大体积响应。复盘与次日观察内容不等于收益回测或交易建议。', '']
    return '\n'.join(lines)


def _comparison_lines(report, comparison):
    """Render insights.compare_reports' output without recomputing evidence."""
    if (not isinstance(comparison, dict) or comparison.get('current_date') != report.get('date')
            or comparison.get('mode') != (report.get('mode') or 'live')):
        raise ValueError('复盘对比与待保存报告的日期或模式不匹配')
    market, limits = _dict(comparison.get('market')), _dict(comparison.get('limit_up'))
    sectors = _dict(comparison.get('sectors'))
    coverage = _dict(sectors.get('coverage'))
    lines = ['', '## 历史复盘对比', '',
             f"当期 {_text(comparison.get('current_date'))}，基准 {_text(comparison.get('previous_date'))}；状态 {_text(comparison.get('status'))}。", '',
             '已确认相邻交易日。' if comparison.get('adjacent_sessions') is True else '未确认相邻交易日；本节仅比较两期观察，不推断期间持续连板或已证实断板。', '']
    for warning in [*(comparison.get('warnings') or []), *(sectors.get('warnings') or [])]:
        lines.append('- ' + _text(warning))
    lines += ['', '### 盘面指标变化', '', '变化值为当期减基准；比例之差单位为百分点。provisional 表示日期未独立核实的暂定比较。', '']
    lines += _table(['指标', '当期', '基准', '变化', '单位', '状态'], [
        [row.get('label'), row.get('current'), row.get('previous'), row.get('delta'), row.get('unit'), row.get('status')]
        for row in _rows(market.get('rows'))])
    lines += ['', '### 涨停池变化', '',
              f"当期 {_fmt(limits.get('current_count'))} 只，基准 {_fmt(limits.get('previous_count'))} 只；共同两期 {_fmt(limits.get('retained_count'))} 只、当期新出现 {_fmt(limits.get('new_count'))} 只、从对比池退出 {_fmt(limits.get('exited_count'))} 只。", '',
              '只有两期完整池均可用时才计算；“当期新出现”不等于首板，“退出”不等于已经证实断板。', '']
    for key, label in (('retained', '共同两期'), ('new', '当期新出现'), ('exited', '从对比池退出')):
        lines += ['#### ' + label, '']
        lines += _table(['代码', '股票', '当期分', '基准分', '当期严格连板', '基准严格连板'], [
            [row.get('thscode'), row.get('name'), _dict(row.get('current')).get('score'),
             _dict(row.get('previous')).get('score'), _consecutive(_dict(row.get('current'))),
             _consecutive(_dict(row.get('previous')))]
            for row in _rows(limits.get(key))])
        lines.append('')
    lines += ['### 板块变化', '',
              f"两期共同板块 {_fmt(coverage.get('common_count'))} 个，可比较 {_fmt(coverage.get('comparable_count'))} 个，展示 {_fmt(coverage.get('returned_count'))} 个。先按完整代码匹配，再展示涨幅变化绝对值前 30 项。", '',
              '名次变化为基准名次减当期名次，正数表示名次上升；样本覆盖变化会影响排名。成交额及名次变化不是净资金流。', '']
    lines += _table(['板块', '代码', '当期名次', '基准名次', '名次变化', '当期涨幅', '基准涨幅', '涨幅差（百分点）', '成交额变化', '状态'], [
        [row.get('name'), row.get('thscode'), row.get('current_rank'), row.get('previous_rank'), row.get('rank_change'),
         _fmt(row.get('current_change_pct'), '%'), _fmt(row.get('previous_change_pct'), '%'), row.get('change_delta_pp'),
         _fmt(row.get('turnover_change_pct'), '%'), row.get('status')]
        for row in _rows(sectors.get('rows'))[:30]])
    return lines
