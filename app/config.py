import json
import math
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
DEFAULT_WEIGHTS = dict(gap=.15, amount=.15, turnover=.1, volume_ratio=.1,
                       late_momentum=.2, retention=.15, continuity=.15)
DEFAULT = dict(poll_seconds=3, batch_size=100, universe='focus', watchlist=[],
               weights=DEFAULT_WEIGHTS, request_interval=.5, request_timeout=6,
               auto_review=True, review_time='15:10', final_grace_seconds=60)


def load_config():
    cfg = dict(DEFAULT, weights=dict(DEFAULT_WEIGHTS))
    path = DATA / 'config.json'
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding='utf-8')))
    return validate_config(cfg)


def validate_config(raw):
    cfg = dict(DEFAULT, **{k: v for k, v in raw.items() if k in DEFAULT})
    cfg['poll_seconds'] = max(1, min(60, float(cfg['poll_seconds'])))
    cfg['batch_size'] = max(1, min(100, int(cfg['batch_size'])))
    if cfg['universe'] not in ('focus', 'all', 'watchlist'):
        raise ValueError('股票池须为 focus、all 或 watchlist')
    w = cfg['watchlist']
    if isinstance(w, str):
        w = re.split(r'[,，\s]+', w.strip())
    cfg['watchlist'] = list(dict.fromkeys(str(x).strip().upper() for x in w if str(x).strip()))
    if len(cfg['watchlist']) > 10000:
        raise ValueError('股票池超过 10000 只')
    for code in cfg['watchlist']:
        if not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', code):
            raise ValueError('请使用完整证券代码，例如 600519.SH；不自动猜测交易所')
    weights = cfg['weights']
    if set(weights) != set(DEFAULT_WEIGHTS):
        raise ValueError('请保留全部七个因子；不使用的因子权重设为 0')
    weights = {k: float(v) for k, v in weights.items()}
    if any(not math.isfinite(v) or v < 0 for v in weights.values()) or not math.isfinite(sum(weights.values())) or sum(weights.values()) <= 0:
        raise ValueError('因子权重须为有限非负数且总和大于 0')
    cfg['weights'] = {k: v / sum(weights.values()) for k, v in weights.items()}
    for key in ('request_interval', 'request_timeout', 'poll_seconds'):
        if not math.isfinite(float(cfg[key])):
            raise ValueError('时间参数必须为有限数')
    cfg['request_interval'] = max(.25, min(10, float(cfg['request_interval'])))
    cfg['request_timeout'] = max(2, min(20, float(cfg['request_timeout'])))
    cfg['final_grace_seconds'] = max(10, min(120, int(cfg['final_grace_seconds'])))
    if not re.fullmatch(r'15:[1-5]\d', cfg['review_time']):
        raise ValueError('自动复盘时间须在 15:10 至 15:59 之间')
    return cfg


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def credential_dir():
    # User-level credentials are intentionally outside the deliverable and Git tree.
    if os.name == 'nt':
        return Path(os.environ.get('APPDATA', str(Path.home() / 'AppData/Roaming'))) / 'hithink-finance'
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'hithink-finance'


def _windows_finance_key():
    if os.name == 'nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as reg:
                value = winreg.QueryValueEx(reg, 'HITHINK_FINANCE_API_KEY')[0]
                return value.strip() if isinstance(value, str) else ''
        except OSError:
            pass
    return ''


def _file_finance_key():
    path = credential_dir() / 'credentials.env'
    if path.exists():
        if path.stat().st_size > 1_048_576:
            raise ValueError('用户凭据文件过大，请检查本机配置')
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if line.startswith('HITHINK_FINANCE_API_KEY='):
                return line.partition('=')[2].strip().strip('\"').strip("'")
    return ''


def _finance_credential():
    # A key explicitly saved in the webpage survives restarts even when the
    # launcher inherited an older environment value. Never mutate user env.
    key = _file_finance_key()
    if key:
        return key, 'user_file', True
    key = os.environ.get('HITHINK_FINANCE_API_KEY', '').strip()
    if key:
        return key, 'process', False
    key = _windows_finance_key()
    return (key, 'user_environment', True) if key else ('', 'missing', False)


def finance_key():
    return _finance_credential()[0]


def validate_finance_key(key):
    if (not isinstance(key, str) or not key or len(key) > 512
            or any(ord(c) < 33 or ord(c) > 126 or c in '\"\'' for c in key)):
        raise ValueError('API Key 须为 1–512 位文本，不能包含空格、换行、控制字符或引号')
    return key


def save_finance_key(key):
    key = validate_finance_key(key)
    folder = credential_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'credentials.env'
    other = []
    if path.exists():
        other = [line for line in path.read_text(encoding='utf-8-sig').splitlines()
                 if not line.startswith('HITHINK_FINANCE_API_KEY=')]
    temp = path.with_suffix('.tmp')
    temp.write_text('\n'.join(other + ['HITHINK_FINANCE_API_KEY=' + key]) + '\n', encoding='utf-8')
    if os.name != 'nt':
        temp.chmod(0o600)
    temp.replace(path)


def credential_status():
    _, source, persisted = _finance_credential()
    return {'credential_persisted':persisted,'credential_source':source}


def load_llm():
    path = credential_dir() / 'auction-lab-llm.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def save_llm(value):
    from urllib.parse import urlsplit
    base = str(value.get('base_url', '')).rstrip('/')
    url = urlsplit(base)
    if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('请输入兼容 Chat Completions 的 API 基地址，如 https://服务域名/v1')
    if url.scheme == 'http' and url.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('远程模型 API 必须使用 HTTPS；本机模型可使用 HTTP')
    old = load_llm()
    # Never silently forward the old provider's key to a changed host.
    key = str(value.get('api_key', '')).strip()
    if not key and base == old.get('base_url'):
        key = old.get('api_key', '')
    model = str(value.get('model', '')).strip()
    if not model:
        raise ValueError('请填写模型名称')
    path = credential_dir() / 'auction-lab-llm.json'
    atomic_json(path, dict(base_url=base, model=model, api_key=key))
    if os.name != 'nt':
        path.chmod(0o600)
