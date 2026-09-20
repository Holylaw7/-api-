"""Strict, local historical-data adapter; never guesses missing auction fields."""
import copy
from datetime import datetime, time

from .engine import CODE, NUMERIC_FIELDS, aware, finite_number
from .report_library import valid_date

MAX_IMPORT_BYTES = 5 * 1024 * 1024
POOL_FIELDS = {'thscode', 'name', 'context_date', 'continue_day_cnt',
               'continue_day_text', 'strict_consecutive', 'is_st', 'is_new'}


def instant(value):
    try:
        return aware(datetime.fromisoformat(value))
    except (ValueError, TypeError):
        raise ValueError('历史数据时间必须是带时区的 ISO 时间') from None


def pool_rows(value):
    if not isinstance(value, list) or len(value) > 6000:
        raise ValueError('涨停池必须为完整的记录数组，最多6000条')
    result, seen = [], set()
    for raw in value:
        code = raw.get('thscode') if isinstance(raw, dict) else None
        if not isinstance(code, str) or not CODE.fullmatch(code) or code in seen:
            raise ValueError('历史池含无效或重复的完整股票代码')
        seen.add(code)
        row = {key: copy.deepcopy(val) for key, val in raw.items() if key in POOL_FIELDS}
        if not isinstance(row.get('name', ''), str) or len(row.get('name', '')) > 100:
            raise ValueError('股票名称格式无效')
        if not isinstance(row.get('continue_day_text', ''), str) or len(row.get('continue_day_text', '')) > 100:
            raise ValueError('连板描述格式无效')
        for key in ('continue_day_cnt', 'strict_consecutive'):
            if key in row and row[key] is not None:
                value = finite_number(row[key])
                if value is None or value < 0 or value > 10000 or value != int(value):
                    raise ValueError('历史连板计数必须为有限非负整数')
                row[key] = int(value)
        for key in ('is_st', 'is_new'):
            if key in row and not isinstance(row[key], bool):
                raise ValueError('股票状态必须是布尔值')
        result.append(row)
    return result


def import_template():
    return {
        'schema_version': 1,
        'provenance': {'provider': '', 'dataset_id': '', 'timestamp_semantics': 'observed_at',
                       'amount_unit': 'CNY', 'pct_unit': 'percentage_points',
                       'point_in_time_attested': False},
        'sessions': [],
        'instructions': [
            '此文件为空模板，不包含行情。按 docs/BACKTEST.md 契约填入真实历史数据。',
            '每个 session 包含 manifest、batches、outcome；每个文件最多40日、5MB。',
            'manifest: date/mode=live/prepared_at/previous_date/calendar/context/codes/context_complete/point_in_time；时间带+08:00。',
            'context 是上一交易日完整涨停池，按完整股票代码映射，行须有context_date。prepared_at必须是当时真实准备时间。',
            'batches 为 [{received_at,stage,data}]；data含timestamp/auction_phase/data_status/item，item采用官方auction_*字段。',
            'outcome: {date,complete:true,retrieved_at,rows:[完整当日涨停池]}；只有15:10后取得的全池可作标签。',
            '必须真实声明观察时间、元金额、百分数单位和当时可见性，不能把事后下载时间回填为早盘接收时间。',
            'point_in_time_attested=true表示你已核实外部来源；本程序不能代替数据商证明历史时间真实性。',
        ],
    }


def validate_import(dataset, now):
    if not isinstance(dataset, dict) or type(dataset.get('schema_version')) is not int or dataset.get('schema_version') != 1:
        raise ValueError('请选择 schema_version=1 的历史数据文件')
    provenance = dataset.get('provenance')
    if not isinstance(provenance, dict):
        raise ValueError('导入必须声明真实数据来源与单位')
    for key in ('provider', 'dataset_id'):
        if not isinstance(provenance.get(key), str) or not 1 <= len(provenance[key].strip()) <= 160:
            raise ValueError('请提供数据提供者和来源批号（各1至160字符）')
    if provenance.get('point_in_time_attested') is not True:
        raise ValueError('请明确确认外部数据的历史当时可见性')
    for key, expected in (('timestamp_semantics', 'observed_at'), ('amount_unit', 'CNY'),
                          ('pct_unit', 'percentage_points')):
        if provenance.get(key) != expected:
            raise ValueError('请先核实历史观察时间、元金额、百分数单位与当时可见性声明')
    sessions = dataset.get('sessions')
    if not isinstance(sessions, list) or not 1 <= len(sessions) <= 40:
        raise ValueError('每次导入须有1至40个交易日，可分文件导入')
    result, dates = [], set()
    current = aware(now)
    for session in sessions:
        if not isinstance(session, dict) or not isinstance(session.get('manifest'), dict):
            raise ValueError('历史会话缺少 manifest')
        raw = session['manifest']
        day = valid_date(raw.get('date'))
        if day in dates or day > current.date().isoformat():
            raise ValueError('历史会话日期重复或晚于今天')
        dates.add(day)
        if raw.get('mode') != 'live':
            raise ValueError('历史导入仅接受真实数据，演示不能进入回测')
        calendar = raw.get('calendar')
        if not isinstance(calendar, list) or not 2 <= len(calendar) <= 1500:
            raise ValueError('历史会话必须提供来源交易日历')
        calendar = sorted(set(valid_date(d) for d in calendar))
        previous = next((d for d in reversed(calendar) if d < day), None)
        if day not in calendar or not previous or raw.get('previous_date') != previous:
            raise ValueError('上一交易日与来源交易日历不一致')
        prepared = instant(raw.get('prepared_at'))
        if prepared > current or prepared.date().isoformat() != day:
            raise ValueError('准备时间必须属于会话日且不晚于当前时间')
        context = raw.get('context')
        if not isinstance(context, dict):
            raise ValueError('manifest.context须按完整股票代码映射')
        previous_rows = pool_rows(list(context.values()))
        if set(context) != {r['thscode'] for r in previous_rows} or any(r.get('context_date') != previous for r in previous_rows):
            raise ValueError('历史上下文必须来自日历指定的上一交易日')
        for code, row in context.items():
            if not isinstance(row, dict) or row.get('thscode') != code:
                raise ValueError('上下文代码与映射键不一致')
        if raw.get('context_complete') is not True or not isinstance(raw.get('point_in_time'), bool):
            raise ValueError('请声明完整昨日池及其当时可见性')
        codes = raw.get('codes')
        if not isinstance(codes, list) or len(codes) > 6000 or any(not isinstance(c, str) or not CODE.fullmatch(c) for c in codes):
            raise ValueError('codes必须为完整股票代码数组')
        manifest = dict(date=day, mode='live', prepared_at=prepared.isoformat(), previous_date=previous,
                        calendar=calendar, context={r['thscode']:r for r in previous_rows},
                        codes=sorted(set(codes)), context_complete=True, point_in_time=raw['point_in_time'],
                        source='historical_import')
        batches = session.get('batches')
        if not isinstance(batches, list) or not 1 <= len(batches) <= 5000:
            raise ValueError('每个交易日须包含1至5000个真实竞价批次')
        clean_batches, last = [], None
        for batch in batches:
            if not isinstance(batch, dict):
                raise ValueError('批次格式无效')
            received = instant(batch.get('received_at'))
            if received > current or received.date().isoformat() != day or not time(9,15) <= received.time() <= time(9,26):
                raise ValueError('竞价批次须在会话日09:15至09:26真实收到')
            if last and received < last:
                raise ValueError('导入批次须按真实接收顺序排列，不能倒序重构')
            last = received
            stage, data = batch.get('stage'), batch.get('data')
            if stage not in ('live', 'final') or not isinstance(data, dict):
                raise ValueError('批次须包含stage=live/final与data对象')
            items = data.get('item')
            if not isinstance(items, list) or len(items) > 100:
                raise ValueError('每批item须为不超过100条的数组')
            clean_items, seen = [], set()
            for item in items:
                code = item.get('thscode') if isinstance(item, dict) else None
                if not isinstance(code, str) or not CODE.fullmatch(code) or code in seen:
                    raise ValueError('竞价记录含无效或重复的完整股票代码')
                seen.add(code)
                name = item.get('name', '')
                if not isinstance(name, str) or len(name) > 100:
                    raise ValueError('竞价股票名称无效')
                clean = {'thscode':code, 'name':name}
                for key in NUMERIC_FIELDS:
                    value = item.get(key)
                    parsed = finite_number(value)
                    if value is not None and parsed is None:
                        raise ValueError('竞价数值必须为有限数值或null，不能用空串代替缺失')
                    if key in item:
                        clean[key] = parsed
                clean_items.append(clean)
            clean_data = {'item':clean_items}
            for key in ('auction_phase', 'data_status'):
                if not isinstance(data.get(key), str) or len(data[key]) > 40:
                    raise ValueError('竞价批次缺少有效的上游阶段或状态')
                clean_data[key] = data[key]
            stamp = data.get('timestamp')
            if stamp is not None and finite_number(stamp) is None:
                raise ValueError('响应组装时间戳须为有限毫秒数或null')
            clean_data['timestamp'] = finite_number(stamp)
            clean_batches.append([received.isoformat(), stage, clean_data])
        outcome = session.get('outcome')
        if not isinstance(outcome, dict) or outcome.get('date') != day or outcome.get('complete') is not True:
            raise ValueError('历史导入须提供日期一致、明确完整的收盘涨停池')
        retrieved = instant(outcome.get('retrieved_at'))
        closed = datetime.fromisoformat(day+'T15:10:00+08:00')
        if not closed <= retrieved <= current:
            raise ValueError('结果池须在目标日15:10后取得，不能来自未来')
        result.append({'manifest':manifest, 'batches':clean_batches,
                       'outcome':{'date':day, 'complete':True, 'retrieved_at':retrieved.isoformat(),
                                  'rows':pool_rows(outcome.get('rows'))}})
    return {'schema_version':1, 'provenance':{k:provenance[k] for k in (
        'provider','dataset_id','timestamp_semantics','amount_unit','pct_unit','point_in_time_attested')},
            'sessions':result}
