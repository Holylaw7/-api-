"""Start or reuse the standalone local AI assistant, then open its browser UI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:8766'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('本机服务不应重定向')


def alive():
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(URL + '/api/health', timeout=1) as response:
            value = json.loads(response.read(4096))
        return value.get('application') == 'auction-ai-assistant' and value.get('ok') is True
    except Exception:
        return False


def main():
    if sys.version_info < (3, 10):
        raise RuntimeError('需要 Python 3.10 或更新版本')
    if not alive():
        (ROOT / 'data').mkdir(exist_ok=True)
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        with (ROOT / 'data' / 'ai-startup.log').open('ab') as log:
            subprocess.Popen([sys.executable, str(ROOT / 'run_ai.py')], cwd=ROOT,
                             stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             creationflags=flags, start_new_session=os.name != 'nt')
        for _ in range(50):
            if alive():
                break
            time.sleep(.2)
        else:
            raise RuntimeError('AI 助手未启动；请检查 data/ai-startup.log 或 8766 端口是否被占用')
    webbrowser.open(URL)
    print('AI 助手已启动：http://127.0.0.1:8766。选择 DeepSeek 或 ChatGPT，保存对应 API Key 即可。')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc))
        input('按回车关闭…')
