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
MIN_VERSION = (1, 7, 0)


def alive():
    try:
        with urllib.request.urlopen(URL+'/api/health',timeout=1) as res:
            health = json.load(res)
    except Exception:
        return False
    if not isinstance(health, dict) or health.get('application') != 'auction-lab':
        return False
    try:
        version = tuple(int(part) for part in str(health.get('version', '')).split('.'))
    except ValueError:
        version = ()
    if len(version) != 3 or version < MIN_VERSION:
        raise RuntimeError('8765 端口上的竞价研究台仍是旧版本。请关闭旧服务或重启电脑，'
                           '再双击新版启动文件，才能使用「同花顺接入」。'
                           '\n如需保留旧服务，可运行 python run.py --port 8768 --no-auto-start，'
                           '并打开 http://127.0.0.1:8768。')
    return True


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
