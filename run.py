import argparse
from app.server import serve

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='竞价与连板分析系统，本机服务')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--no-auto-start',action='store_true',help='只打开本机页面，不自动启动行情监测')
    args = parser.parse_args()
    serve(args.port, auto_start=not args.no_auto_start)
