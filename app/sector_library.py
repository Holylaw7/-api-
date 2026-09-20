"""Version-bound supplemental sector evidence, separate from immutable reports."""
import copy
import hashlib
import json
import re
import threading

from .config import atomic_json
from .report_library import valid_date


class SectorLibrary:
    def __init__(self, data_dir):
        self.root = data_dir / 'sector-research'
        self.lock = threading.RLock()

    @staticmethod
    def _binding(date, review_id):
        valid_date(date)
        if not isinstance(review_id, str) or not re.fullmatch(r'[a-f0-9]{64}', review_id):
            raise ValueError('板块数据需要有效的报告版本')
        return date + '-' + review_id

    def save(self, value):
        result = copy.deepcopy(value)
        binding = self._binding(result.get('date'), result.get('review_id'))
        if result.get('mode') != 'live':
            raise ValueError('板块取数只保存真实数据')
        result.pop('evidence_id', None)
        digest = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True,
            allow_nan=False).encode('utf-8')).hexdigest()[:24]
        result['evidence_id'] = digest
        with self.lock:
            path = self.root / 'evidence' / (digest + '.json')
            if not path.exists():
                atomic_json(path, result)
            atomic_json(self.root / 'latest' / (binding + '.json'), {'evidence_id':digest})
        return result

    def get(self, evidence_id):
        if not isinstance(evidence_id, str) or not re.fullmatch(r'[a-f0-9]{24}', evidence_id):
            raise ValueError('板块证据编号无效')
        path = self.root / 'evidence' / (evidence_id + '.json')
        if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('板块证据不存在或文件过大')
        try:
            result = json.loads(path.read_text(encoding='utf-8'))
            if result.get('evidence_id') != evidence_id or result.get('mode') != 'live':
                raise ValueError()
            body = {k:v for k,v in result.items() if k != 'evidence_id'}
            digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                allow_nan=False).encode('utf-8')).hexdigest()[:24]
            if digest != evidence_id:
                raise ValueError()
        except (ValueError, AttributeError, TypeError):
            raise ValueError('板块证据校验失败，请重新取数') from None
        return result

    def latest(self, date, review_id):
        binding = self._binding(date, review_id)
        path = self.root / 'latest' / (binding + '.json')
        if not path.exists():
            return {'status':'not_run','date':date,'review_id':review_id}
        with self.lock:
            try:
                pointer = json.loads(path.read_text(encoding='utf-8'))
                result = self.get(pointer.get('evidence_id'))
            except (ValueError, AttributeError):
                raise ValueError('板块缓存损坏，请重新取数') from None
        if result.get('date') != date or result.get('review_id') != review_id:
            raise ValueError('板块缓存与报告版本不符')
        return result
