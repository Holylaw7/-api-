"""Auditable local replay experiments, isolated from the live collection loop."""
import copy
import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path

from .config import atomic_json
from .engine import AuctionEngine, DEFAULT_WEIGHTS, NUMERIC_FIELDS, finite_number
from .report_library import atomic_text, valid_date
from .research_data import instant, pool_rows, validate_import, import_template
from .replay import replay_session, pool_transitions
from .optimization import optimize_sessions

VERSION = '1.5.0'
CHECKPOINTS = ('09:24:50', '09:26:00')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _check(should_stop):
    if should_stop and should_stop():
        raise ValueError('回测已中止；竞价保护时段外可重新运行，已完成的历史实验保留')


def evaluation(rows, top_k=10):
    # This descriptive baseline includes all scorable rows; optimizer explicitly
    # uses the smaller common seven-factor sample for fair weight comparisons.
    known = [r for r in rows if isinstance(r.get('label'), bool)]
    scored = sorted([r for r in known if finite_number(r.get('score')) is not None],
                    key=lambda r:(-r['score'], -r.get('quality',{}).get('factor_coverage',0), r['thscode']))
    selected = scored[:top_k]
    return {'top_k':top_k, 'selected_count':len(selected), 'hits':sum(r['label'] for r in selected),
            'precision_pct':round(100*sum(r['label'] for r in selected)/len(selected),2) if selected else None,
            'scored_labeled_count':len(scored), 'known_label_count':len(known),
            'pool_base_pct':round(100*sum(r['label'] for r in known)/len(known),2) if known else None,
            'selected_codes':[r['thscode'] for r in selected]}


def _cell(value):
    if value is None:
        return '—'
    return str(value).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('|','\\|').replace('\n',' ')


def render_research(report):
    lines = ['# 竞价历史回放与因子实验', '', f"实验编号：{report['id']}  ",
             f"生成时间：{report['generated_at']}  ", f"状态：{report['status']}", '',
             report['definition'], '', '## 数据与边界', '']
    for key, value in report['availability'].items():
        lines.append(f'- {_cell(key)}：{_cell(value)}')
    lines += ['', *['- '+_cell(w) for w in report['warnings']], '', '## 昨日涨停延续基准', '',
              '此表仅描述历史涨停池延续，不是竞价算法命中率。', '',
              '| 日期 | 上一交易日 | 昨日涨停 | 当日继续在池 | 延续率 |', '|---|---|---:|---:|---:|']
    for row in report['transitions'].get('rows', []):
        lines.append('| '+' | '.join(_cell(row.get(k)) for k in ('date','previous_date','yesterday_count','repeat_count','repeat_rate_pct'))+' |')
    lines += ['', '## 当前算法逐日回放', '', '09:24:50 只使用当时已收到的批次；09:26 为事后终态核验，不能冒充09:25之前的信号。', '']
    for session in report['sessions']:
        lines += [f"### {_cell(session['date'])} / {_cell(session['checkpoint'])}", '',
                  f"来源：{_cell(session.get('origin',session.get('mode')))}；可作严格优化：{session.get('quality',{}).get('eligible_for_optimization',False)}", '',
                  '质量：`'+json.dumps(session.get('quality',{}),ensure_ascii=False,sort_keys=True)+'`', '',
                  '当前算法评价：`'+json.dumps(session.get('evaluation',{}),ensure_ascii=False,sort_keys=True)+'`', '',
                  '| 股票代码 | 名称 | 原算法分数 | 当日涨停池成员 |', '|---|---|---:|---|']
        for row in session.get('rows', []):
            label = '未知' if row.get('label') is None else '是' if row['label'] else '否'
            lines.append('| '+' | '.join(_cell(v) for v in (row.get('thscode'),row.get('name'),row.get('score'),label))+' |')
        lines += ['', *['- '+_cell(w) for w in session.get('warnings',[])], '']
    opt = report['optimization']
    lines += ['## 因子实验与时间检验', '', _cell(opt.get('recommendation',{}).get('reason','数据不足，尚不能选择新权重。')), '',
              '参数建议不自动替换实盘配置。命中率不是收益率，未模拟成交、手续费、滑点或涨停无法买入。', '',
              '```json', json.dumps({k:opt.get(k) for k in ('status','sample_summary','baseline','search','candidate_weights','recommendation','holdout','factor_diagnostics','warnings')},
                                    ensure_ascii=False,indent=2,allow_nan=False), '```', '',
              '## 复现依据', '', '```json', json.dumps(report['reproducibility'],ensure_ascii=False,indent=2), '```', '']
    return '\n'.join(lines)


class ResearchLibrary:
    def __init__(self, store, data_dir):
        self.store = store
        self.root = Path(data_dir) / 'research'
        self.lock = threading.RLock()

    def _read(self, path, default=None):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return copy.deepcopy(default)

    def latest(self):
        pointer = self._read(self.root/'latest.json', {})
        return self.get(pointer.get('id')) if pointer.get('id') else {'status':'not_run'}

    def get(self, experiment_id):
        if not isinstance(experiment_id,str) or not re.fullmatch('[a-f0-9]{24}', experiment_id):
            raise ValueError('实验编号无效')
        report = self._read(self.root/'experiments'/(experiment_id+'.json'))
        if not report or report.get('id') != experiment_id:
            raise ValueError('实验档案不存在，请先运行历史回测')
        return report

    def export(self, experiment_id=None, format_name='markdown'):
        report = self.get(experiment_id) if experiment_id else self.latest()
        if not report.get('id'):
            raise ValueError('尚无实验可保存，请先运行回测')
        if format_name not in ('markdown','json'):
            raise ValueError('仅支持markdown或json导出')
        name = 'auction-research-'+report['id']+('.md' if format_name=='markdown' else '.json')
        return name, render_research(report) if format_name=='markdown' else report

    def ai_dataset(self, experiment_id=None):
        """Export development rows only; test-period rows/labels are withheld."""
        report = self.get(experiment_id) if experiment_id else self.latest()
        if not report.get('id'):
            raise ValueError('请先生成历史实验，再取得开发数据')
        available = [s for s in report['sessions'] if s.get('mode')=='live' and s.get('checkpoint')==CHECKPOINTS[0]
                     and s.get('quality',{}).get('eligible_for_optimization')]
        search = report.get('optimization',{}).get('search',{})
        dates = sorted({s['date'] for s in available})
        development = search.get('development_dates')
        if development is None:
            reserved = max(5, (len(dates)+4)//5)
            development = dates[:-reserved] if len(dates)>reserved else []
        return {'schema_version':1,'experiment_id':report['id'],'scope':'development_only',
                'dataset_sha256':report['reproducibility']['dataset_sha256'],
                'baseline_weights':report['baseline_weights'],'definition':report['definition'],
                'sessions':[s for s in available if s['date'] in development],
                'excluded_date_count':len(set(dates)-set(development)),
                'proposal_endpoint':'POST /api/research/proposals',
                'rules':['仅提出七因子权重建议，不改原始数据或回填未知因子。',
                         '不得用保留日期结果调参；提交建议不会自动启用，需要新的未使用日期检验。',
                         '空sessions表示开发样本不足，不能声称已获得更好参数。']}

    def historical_dataset(self, date, now):
        day = valid_date(date)
        manifest = self.store.manifest(day)
        batches = self.store.batches(day)
        if not manifest or not batches:
            raise ValueError('该日没有本机冻结清单和真实竞价批次，不能导出不存在的历史')
        if len(batches)>5000:
            raise ValueError('该日超过5000批次单次导出上限，请使用本机SQLite做离线研究')
        pools, _ = self._pool_evidence(now,None)
        clean = []
        for received, stage, payload in batches:
            data = {k:payload.get(k) for k in ('timestamp','auction_phase','data_status')}
            data['item'] = [{k:r[k] for k in ('thscode','name',*NUMERIC_FIELDS) if k in r}
                            for r in payload.get('item',[]) if isinstance(r,dict)]
            clean.append({'received_at':received,'stage':stage,'data':data})
        safe_manifest = {k:copy.deepcopy(manifest.get(k)) for k in ('date','mode','prepared_at','previous_date',
                         'calendar','context','codes','context_complete','point_in_time')}
        return {'schema_version':1,'provenance':{'provider':'HiThink Financial-API / local collection',
                    'dataset_id':digest({'manifest':safe_manifest,'batches':clean}),
                    'timestamp_semantics':'observed_at','amount_unit':'CNY','pct_unit':'percentage_points',
                    'point_in_time_attested':True},
                'sessions':[{'manifest':safe_manifest,'batches':clean,'outcome':pools.get(day)}],
                'instructions':['审计用全日原始序列；含已知收盘标签，不应把保留检验日期交给调参模型。',
                                '缺少结果池时outcome=null，此时只可审计，完整导入前需补齐真实结果池。']}

    def save_proposal(self, proposal, now):
        if not isinstance(proposal,dict):
            raise ValueError('参数建议须为对象')
        report = self.get(proposal.get('experiment_id'))
        weights = proposal.get('weights')
        if not isinstance(weights,dict) or set(weights)!=set(DEFAULT_WEIGHTS) or any(isinstance(v,bool) or finite_number(v) is None for v in weights.values()):
            raise ValueError('参数建议须包含七个有限非负权重')
        weights = AuctionEngine(weights).weights
        source = proposal.get('source_model','external')
        rationale = proposal.get('rationale','')
        if not isinstance(source,str) or not 1 <= len(source) <= 100 or not isinstance(rationale,str) or len(rationale)>4000:
            raise ValueError('模型来源最多100字，建议理由最多4000字')
        record = {'experiment_id':report['id'],'dataset_sha256':report['reproducibility']['dataset_sha256'],
                  'weights':weights,'source_model':source,'rationale':rationale,
                  'status':'awaiting_future_validation','automatically_applied':False,
                  'reason':'参数建议仅归档，需用新的未参与选参的日期验证后，由用户决定是否应用。'}
        proposal_id = digest(record)[:24]
        with self.lock:
            path = self.root/'proposals'/(proposal_id+'.json')
            if not path.exists():
                atomic_json(path,dict(record,id=proposal_id,submitted_at=now.isoformat()))
        return {'proposal_id':proposal_id,'status':record['status'],'automatically_applied':False}

    def proposals(self):
        paths = sorted((self.root/'proposals').glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:100]
        return {'items':[value for path in paths if re.fullmatch('[a-f0-9]{24}\\.json',path.name)
                         if (value:=self._read(path)) and value.get('status')=='awaiting_future_validation'],
                'limit':100,'automatic_application':False}

    def import_dataset(self, dataset, now):
        clean = validate_import(dataset, now)
        import_id = digest(clean)[:24]
        with self.lock:
            existing = self._read(self.root/'imports.json', {'sessions':{}})
            local_days = {r['date'] for r in self.store.batch_dates()}
            incoming = {s['manifest']['date']:s for s in clean['sessions']}
            if local_days.intersection(incoming):
                raise ValueError('导入日期已有本机真实竞价，不能覆盖或混合来源')
            for day, session in incoming.items():
                record = dict(session, provenance=clean['provenance'], import_id=import_id)
                old = existing['sessions'].get(day)
                if old is not None and old != record:
                    raise ValueError('该日期已导入另一数据版本，拒绝覆盖既有证据')
                existing['sessions'][day] = record
            if len(existing['sessions']) > 120:
                raise ValueError('历史导入上限120个交易日；请另建研究目录管理更长区间')
            atomic_json(self.root/'imports.json', existing)
        return {'import_id':import_id, 'imported_days':len(incoming),
                'message':'已保存外部历史数据声明；仍需运行回测，导入不会改写实盘数据库'}

    def _pool_evidence(self, now, should_stop):
        pools, calendar = {}, set()
        for entry in reversed(self.store.list_reports('live',120)):
            _check(should_stop)
            report = self.store.get_report(entry['date'],'live')
            if not report or report.get('mode','live') != 'live' or report.get('date') != entry['date']:
                continue
            raw = report.get('raw',{})
            if not isinstance(raw,dict) or not isinstance(raw.get('calendar'),list):
                continue
            try:
                retrieved = instant(report.get('generated_at'))
                days = [valid_date(d) for d in raw.get('calendar',[])]
                if retrieved > now or report.get('date') not in days or len(days) != len(set(days)) or any(d > retrieved.date().isoformat() for d in days):
                    continue
            except (ValueError,TypeError):
                continue
            calendar.update(days)
            source = raw.get('pools_by_date',{})
            if not isinstance(source,dict):
                continue
            for day, rows in source.items():
                try:
                    valid_date(day)
                    if day not in days or day > report['date'] or retrieved < instant(day+'T15:10:00+08:00'):
                        continue
                    clean = pool_rows(rows)
                except (ValueError,TypeError):
                    continue
                evidence = {'date':day, 'complete':True, 'retrieved_at':retrieved.isoformat(),
                            'rows':clean, 'source':'official_retained_full_pool'}
                old = pools.get(day)
                if old is None or retrieved >= instant(old['retrieved_at']):
                    pools[day] = evidence
        return pools, sorted(calendar)

    def run(self, weights, now, should_stop=None, progress=None):
        weights = AuctionEngine(weights).weights
        _check(should_stop)
        with self.lock:
            imports = self._read(self.root/'imports.json', {'sessions':{}})['sessions']
        pools, calendar = self._pool_evidence(now, should_stop)
        local = {r['date']:r for r in self.store.batch_dates()}
        dates = sorted(set(local)|set(imports))[-120:]
        inputs, warnings = [], []
        for index, day in enumerate(dates):
            _check(should_stop)
            if progress:
                progress(f'核对本机历史数据 {index+1}/{len(dates)}：{day}')
            if day in local and day in imports:
                warnings.append(f'{day}同时存在本机和导入来源，已排除整日以免混用')
                continue
            if day in imports:
                record = imports[day]
                inputs.append(copy.deepcopy(record))
                continue
            if local[day]['batch_count'] > 5000:
                warnings.append(f'{day}超过5000批次研究上限，未截断冒充完整会话')
                continue
            manifest = self.store.manifest(day)
            batches = self.store.batches(day)
            if manifest is None:
                previous = next((d for d in reversed(calendar) if d < day),None)
                previous_pool = pools.get(previous,{})
                if day not in calendar or not previous or not previous_pool.get('complete'):
                    warnings.append(f'{day}缺少原冻结清单及可核验的上一交易日完整池，无法重建，已排除')
                    continue
                manifest = dict(date=day,mode='live',prepared_at=day+'T23:59:59+08:00',
                    previous_date=previous, calendar=calendar,
                    context={r['thscode']:dict(r,context_date=previous) for r in previous_pool.get('rows',[])},
                    codes=sorted({r['thscode'] for _,_,data in batches for r in data.get('item',[]) if isinstance(r,dict) and isinstance(r.get('thscode'),str)}),
                    context_complete=True,point_in_time=False,source='local_preparation')
                warnings.append(f'{day}未冻结盘前清单；仅作回溯描述，不进入参数优化')
            inputs.append({'manifest':manifest, 'batches':batches, 'outcome':pools.get(day)})
        source_hash = digest({name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
                              for name in ('engine.py','replay.py','optimization.py','research.py','research_data.py')})
        data_hash = digest({'inputs':inputs, 'pools':pools, 'calendar':calendar})
        experiment_id = digest({'version':VERSION,'source_hash':source_hash,'dataset':data_hash,'weights':weights})[:24]
        cached = self._read(self.root/'experiments'/(experiment_id+'.json'))
        if cached:
            atomic_json(self.root/'latest.json', {'id':experiment_id})
            return cached
        sessions = []
        for index, record in enumerate(inputs):
            _check(should_stop)
            manifest = record['manifest']
            for checkpoint in CHECKPOINTS:
                if progress:
                    progress(f'按原算法重放 {index+1}/{len(inputs)}：{manifest["date"]} {checkpoint}')
                session = replay_session(manifest,record['batches'],record.get('outcome'),weights,
                                         checkpoint=checkpoint,should_stop=should_stop)
                session['evaluation'] = evaluation(session.get('rows',[]))
                if record.get('provenance'):
                    session['provenance'] = record['provenance']
                sessions.append(session)
        _check(should_stop)
        if progress:
            progress('固定共同样本，按交易日期检验因子与权重')
        optimization = optimize_sessions([s for s in sessions if s['checkpoint']==CHECKPOINTS[0]],
                                         weights,top_k=10,should_stop=should_stop)
        _check(should_stop)
        warnings += [
            '本轮目标仅为当日收盘后完整涨停池成员识别；不代表收益率或可成交性。',
            '09:26终态仅作收尾核验，不参与09:24:50参数选择。没有历史轨迹时不能用日线开盘价或风向标补造七因子。',
            '外部导入的时间、单位、完整性由提供者声明；程序仅核验结构，无法独立证明历史当时可见性。',
            '股票日样本存在同日和跨日相关性；置信区间仅作描述，小样本高命中率不能证明稳定有效。',
            '只研究昨日涨停的固定候选；缺观察、缺因子的股票披露覆盖，不能推广为所有A股的表现。',
        ]
        if not local and not imports:
            warnings.insert(0,'未找到任何真实历史竞价批次，现阶段只能计算涨停池延续基准，无法完成七因子回测和调参。')
        result = {'id':experiment_id,'version':VERSION,'generated_at':now.isoformat(),
                  'status':optimization.get('status','insufficient_data'),'baseline_weights':weights,
                  'availability':{'live_batch_days':len(local),'imported_days':len(imports),
                                  'pool_days':len(pools),'session_days':len(inputs)},
                  'transitions':pool_transitions(pools,calendar),'sessions':sessions,
                  'optimization':optimization,'warnings':warnings,
                  'definition':'用上一交易日完整涨停池固定候选，以当前七因子重放09:24:50前真实收到的竞价，再对照同日15:10后取得的完整涨停池。先训练、再逐日验证，最后保留一段日期检验；权重建议不自动应用。',
                  'reproducibility':{'dataset_sha256':data_hash,'source_sha256':source_hash,
                      'engine':'AuctionEngine/current seven factors','checkpoints':list(CHECKPOINTS),
                      'primary_checkpoint':CHECKPOINTS[0],'weights':weights,'top_k':10,
                      'session_dates':[r['manifest']['date'] for r in inputs],
                      'origin':'local batches + explicitly declared historical imports',
                      'label':'membership in dated complete official/user-declared pool retrieved after 15:10'}}
        with self.lock:
            # The same test dates are not a fresh holdout after another experiment
            # has exposed their outcome. Re-runs of identical data reuse the archive.
            ledger = self._read(self.root/'holdouts.json', {'dates':[]})
            holdout = optimization.get('holdout') or {}
            holdout_dates = optimization.get('search',{}).get('holdout_dates',[]) if holdout else []
            used = sorted(set(holdout_dates).intersection(ledger['dates']))
            if used:
                optimization['recommendation']['accepted'] = False
                optimization['recommendation']['reason'] = '检验日期已在其他实验中使用，本轮仅作探索；需要新的未参与选参的交易日检验。'
                optimization['holdout_reuse_dates'] = used
                result['status'] = 'exploratory_holdout_reuse'
                result['warnings'].append('重复使用了检验区间，不能称为新的独立样本外验证。')
            _check(should_stop)
            # Mark exposed test dates before publishing; interrupted publication
            # remains conservative and cannot silently grant a fresh holdout.
            if holdout_dates:
                atomic_json(self.root/'holdouts.json', {'dates':sorted(set(ledger['dates'])|set(holdout_dates))})
            atomic_json(self.root/'experiments'/(experiment_id+'.json'),result)
            atomic_text(self.root/'experiments'/(experiment_id+'.md'),render_research(result))
            atomic_json(self.root/'latest.json', {'id':experiment_id})
        return result
