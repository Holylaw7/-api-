"""One click starts/reuses the local service, then opens its dashboard."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:8765'


def alive():
    try:
        with urllib.request.urlopen(URL+'/api/health',timeout=1) as res:
            return json.load(res).get('application') == 'auction-lab'
    except Exception:
        return False


def main():
    if sys.version_info < (3,10):
        raise RuntimeError('需要 Python 3.10 或更新版本')
    if not alive():
        (ROOT/'data').mkdir(exist_ok=True)
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        with (ROOT/'data'/'startup.log').open('ab') as log:
            subprocess.Popen([sys.executable,str(ROOT/'run.py')],cwd=ROOT,
                             stdout=log,stderr=log,stdin=subprocess.DEVNULL,
                             creationflags=flags,start_new_session=os.name!='nt')
        for _ in range(50):
            if alive():
                break
            time.sleep(.2)
        else:
            raise RuntimeError('服务未启动；请检查 data/startup.log 或 8765 端口是否被占用')
    webbrowser.open(URL)
    print('系统已启动：http://127.0.0.1:8765。浏览器可关闭，服务仍在本机运行。')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc))
        input('按回车关闭…')
