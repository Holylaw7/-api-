"""Local report lookup and atomic Markdown archives; no remote market requests."""
import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from .reporting import render_markdown, report_identity


def valid_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('请提供 YYYY-MM-DD 格式的报告日期')
    try:
        datetime.strptime(value, '%Y-%m-%d')
    except ValueError:
        raise ValueError('报告日期无效') from None
    return value


def valid_mode(value):
    if value not in ('live', 'demo'):
        raise ValueError('报告模式无效')
    return value


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.stem + '-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as target:
            target.write(text)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ReportLibrary:
    def __init__(self, store, data_dir):
        self.store = store
        self.data_dir = Path(data_dir)

    def folder(self, mode):
        valid_mode(mode)
        return self.data_dir / 'reports' if mode == 'live' else self.data_dir / 'reports' / 'demo'

    def list(self, mode='live'):
        valid_mode(mode)
        items = {row['date']: row for row in self.store.list_reports(mode)}
        for path in self.folder(mode).glob('????-??-??.json'):
            try:
                valid_date(path.stem)
            except ValueError:
                continue
            items.setdefault(path.stem, dict(date=path.stem, mode=mode, generated_at=None))
        return [items[date] for date in sorted(items, reverse=True)[:365]]

    def get(self, date, mode='live'):
        valid_date(date)
        valid_mode(mode)
        report = self.store.get_report(date, mode)
        if report is None:
            path = self.folder(mode) / (date + '.json')
            try:
                with path.open('rb') as source:
                    raw = source.read(64_000_001)
                if len(raw) > 64_000_000:
                    raise ValueError('报告文件超过读取上限')
                report = json.loads(raw.decode('utf-8-sig'))
            except FileNotFoundError:
                raise ValueError('该日期没有已保存报告，请先生成复盘') from None
            except (UnicodeError, json.JSONDecodeError):
                raise ValueError('已保存报告格式无效，请检查本机文件') from None
        if not isinstance(report, dict) or report.get('date') != date or report.get('mode', mode) != mode:
            raise ValueError('报告日期或真实/演示模式与所选记录不符')
        report = copy.deepcopy(report)
        report.setdefault('mode', mode)
        return report

    def markdown(self, report, ai=None, comparison=None):
        valid_date(report.get('date'))
        mode = valid_mode(report.get('mode', 'live'))
        if ai is not None and ai.get('scope') != 'review':
            raise ValueError('这份 AI 回答不是当前收盘报告的专属分析，请重新生成')
        filename = report['date']
        if ai is not None:
            provider = ai.get('provider')
            if provider not in ('deepseek', 'openai', 'custom'):
                raise ValueError('AI 服务商记录无效')
            filename += '-with-ai-' + provider
        return filename + '.md', render_markdown(report, ai=ai, comparison=comparison)

    def save(self, report, ai=None, comparison=None):
        filename, content = self.markdown(report, ai, comparison)
        path = self.folder(report.get('mode', 'live')) / filename
        atomic_text(path, content)
        return {'path': str(path.resolve()), 'filename': filename, 'review_id': report_identity(report),
                'date': report['date'], 'mode': report.get('mode', 'live'),
                'sha256':hashlib.sha256(content.encode('utf-8')).hexdigest()}

    def read_markdown(self, filename, mode, digest=None):
        if not isinstance(filename, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:-with-ai-(?:deepseek|openai|custom))?\.md', filename):
            raise ValueError('报告文件名无效')
        valid_date(filename[:10])
        if digest is not None and not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError('报告校验标记无效')
        try:
            with (self.folder(mode) / filename).open('rb') as source:
                data = source.read(4_000_001)
        except FileNotFoundError:
            raise ValueError('Markdown 文件不存在，请重新点击保存') from None
        if len(data) > 4_000_000:
            raise ValueError('Markdown 文件超过下载上限')
        if digest and hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('保存文件已经被新版本替换，请重新点击保存后下载')
        return data

    def ai_path(self, date, provider, mode):
        valid_date(date)
        valid_mode(mode)
        if provider not in ('deepseek', 'openai', 'custom'):
            raise ValueError('请选择有效的 AI 服务商')
        return self.data_dir / 'ai-reviews' / mode / (date + '-' + provider + '.json')

    def save_ai(self, result):
        path = self.ai_path(result['review_date'], result['provider'], result['mode'])
        # This adapter never receives the API config or credentials.
        fields = ('text', 'provider', 'label', 'model', 'mode', 'generated_at', 'review_date', 'review_id', 'scope',
                  'sector_evidence_id', 'sector_codes', 'sector_generated_at', 'question')
        record = {k: result.get(k) for k in fields}
        record['question'] = result['question'][:2000] if isinstance(result.get('question'), str) else None
        atomic_text(path, json.dumps(record, ensure_ascii=False, allow_nan=False, indent=2))

    def load_ai(self, report):
        results = {}
        for provider in ('deepseek', 'openai', 'custom'):
            path = self.ai_path(report['date'], provider, report.get('mode', 'live'))
            try:
                with path.open('rb') as source:
                    raw = source.read(1_000_001)
                if len(raw) > 1_000_000:
                    continue
                result = json.loads(raw)
                if (isinstance(result, dict) and result.get('review_id') == report_identity(report)
                        and result.get('mode') == report.get('mode', 'live') and result.get('review_date') == report['date']
                        and result.get('provider') == provider and result.get('scope') == 'review'
                        and isinstance(result.get('text'), str)):
                    results[provider] = result
            except (OSError, ValueError, TypeError):
                continue
        return results
