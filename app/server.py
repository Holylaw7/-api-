import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlencode

from .config import ROOT, finance_key, save_finance_key
from .ai_gateway import save_profile, select_provider
from .service import Service, now_sh
from .research_data import MAX_IMPORT_BYTES, import_template
from .report_library import valid_date


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service):
        self.service = service
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass  # Never log request bodies, secrets, or externally supplied query strings.

    def _valid_local(self, mutation=False):
        port = self.server.server_port
        allowed = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if self.headers.get('Host') not in allowed:
            self._json({'ok':False,'message':'仅允许本机访问'},403)
            return False
        origin = self.headers.get('Origin')
        if origin and origin not in {'http://'+h for h in allowed}:
            self._json({'ok':False,'message':'跨站请求已拒绝'},403)
            return False
        if mutation and self.headers.get('X-Local-App') != 'auction-lab':
            self._json({'ok':False,'message':'缺少本机操作标记'},403)
            return False
        return True

    def _json(self, value, status=200, attachment=None):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        if attachment:
            self.send_header('Content-Disposition',f'attachment; filename="{attachment}"')
        self.end_headers()
        self.wfile.write(data)

    def _markdown(self, content, filename):
        data = content.encode('utf-8') if isinstance(content, str) else content
        self.send_response(200)
        self.send_header('Content-Type', 'text/markdown; charset=utf-8')
        self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._valid_local():
            return
        path = urlsplit(self.path).path
        service = self.server.service
        try:
            if path == '/api/health':
                self._json({'ok':True,'application':'auction-lab','version':'1.5.0'})
            elif path == '/api/state':
                self._json(service.snapshot())
            elif path == '/api/events':
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream; charset=utf-8')
                self.send_header('Cache-Control','no-cache')
                self.send_header('Connection','close')
                self.end_headers()
                seen = -1
                last_full = 0
                self.wfile.write(b'retry: 2000\n\n')
                while not service.shutdown.is_set():
                    with service.condition:
                        if seen == service.version:
                            service.condition.wait(timeout=3)
                        version = service.version
                    # Publish every changed batch immediately. Idle connections
                    # receive cheap keepalives instead of resending large reports.
                    elapsed = time.monotonic() - last_full
                    interval = 3 if service._auction_priority() else 15
                    if version != seen or elapsed >= interval:
                        payload = json.dumps(service.snapshot(),ensure_ascii=False,allow_nan=False)
                        self.wfile.write((f'id: {version}\nevent: state\ndata: '+payload+'\n\n').encode('utf-8'))
                        seen, last_full = version, time.monotonic()
                    else:
                        self.wfile.write(b': keepalive\n\n')
                    self.wfile.flush()
                self.close_connection = True
            elif path == '/api/history':
                code = parse_qs(urlsplit(self.path).query).get('symbol',[''])[0]
                self._json(service.history(code))
            elif path == '/api/report':
                query = parse_qs(urlsplit(self.path).query)
                date = query.get('date', [None])[0]
                format_name = query.get('format', ['json'])[0]
                if format_name == 'markdown':
                    include = query.get('include_ai', ['0'])[0]
                    if include not in ('0','1'):
                        raise ValueError('include_ai 须为 0 或 1')
                    filename, content = service.markdown_report(date, include == '1', query.get('provider',[None])[0],
                        query.get('review_id',[None])[0], query.get('baseline',[None])[0])
                    self._markdown(content, filename)
                elif format_name == 'json':
                    report = service.saved_report(date) if date else service.export_report()
                    self._json(report, attachment=('market-review-'+report['date']+'.json') if report.get('date') else 'market-review.json')
                else:
                    raise ValueError('仅支持 json 或 markdown 导出格式')
            elif path == '/api/reports':
                self._json(service.report_catalog())
            elif path == '/api/reports/compare':
                query = parse_qs(urlsplit(self.path).query)
                self._json(service.compare_report(query.get('date',[None])[0], query.get('baseline',[None])[0]))
            elif path == '/api/reports/download':
                query = parse_qs(urlsplit(self.path).query)
                filename = query.get('filename',[None])[0]
                data = service.report_library.read_markdown(filename, query.get('mode',['live'])[0], query.get('sha256',[None])[0])
                self._markdown(data, filename)
            elif path == '/api/diagnostics':
                self._json(service.diagnostics())
            elif path == '/api/research':
                self._json(service.research_library.latest())
            elif path == '/api/research/template':
                self._json(import_template(),attachment='auction-history-template.json')
            elif path == '/api/research/proposals':
                self._json(service.research_library.proposals())
            elif path == '/api/research/ai-dataset':
                query = parse_qs(urlsplit(self.path).query)
                self._json(service.research_library.ai_dataset(query.get('id',[None])[0]),attachment='auction-development-data.json')
            elif path == '/api/research/history':
                if service._auction_priority():
                    raise ValueError('原始历史序列导出请避开09:10–09:26竞价保护时段')
                query = parse_qs(urlsplit(self.path).query)
                day = valid_date(query.get('date',[None])[0])
                self._json(service.research_library.historical_dataset(day,now_sh()),attachment='auction-history-'+day+'.json')
            elif path == '/api/research/export':
                query = parse_qs(urlsplit(self.path).query)
                format_name = query.get('format',['markdown'])[0]
                filename, content = service.research_library.export(query.get('id',[None])[0],format_name)
                if format_name == 'markdown':
                    self._markdown(content,filename)
                else:
                    self._json(content,attachment=filename)
            elif path == '/api/research/daily':
                query = parse_qs(urlsplit(self.path).query)
                date = query.get('date',[None])[0]
                self._json(service.daily_validation.get(valid_date(date)) if date else {'items':service.daily_validation.list()})
            elif path == '/api/research/daily/export':
                query = parse_qs(urlsplit(self.path).query)
                date = valid_date(query.get('date',[None])[0])
                format_name = query.get('format',['markdown'])[0]
                filename, content = service.daily_validation.export(date,format_name)
                if format_name == 'markdown':
                    self._markdown(content,filename)
                else:
                    self._json(content,attachment=filename)
            else:
                files = {'/':'index.html','/index.html':'index.html','/style.css':'style.css','/app.js':'app.js',
                         '/static/style.css':'style.css','/static/app.js':'app.js'}
                if path not in files:
                    self._json({'ok':False,'message':'页面不存在'},404)
                    return
                file = ROOT/'static'/files[path]
                data = file.read_bytes()
                self.send_response(200)
                mime = mimetypes.guess_type(str(file))[0] or 'application/octet-stream'
                if path.endswith('.js'):
                    mime = 'application/javascript'
                self.send_header('Content-Type',mime+'; charset=utf-8')
                self.send_header('Content-Length',str(len(data)))
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
                self.end_headers()
                self.wfile.write(data)
        except (ConnectionError, BrokenPipeError, TimeoutError):
            pass
        except ValueError as exc:
            self._json({'ok':False,'message':str(exc)},400)
        except OSError:
            self._json({'ok':False,'message':'本机报告文件读取失败，请检查目录权限'},500)

    def do_POST(self):
        if not self._valid_local(mutation=True):
            return
        service = self.server.service
        try:
            path = urlsplit(self.path).path
            length = int(self.headers.get('Content-Length','0'))
            if length < 0 or length > (MAX_IMPORT_BYTES if path == '/api/research/import' else 65536):
                self._json({'ok':False,'message':'请求过大'},413)
                return
            body = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(body,dict):
                raise ValueError('请求必须是 JSON 对象')
            path = urlsplit(self.path).path
            message = '已完成'
            started = None
            if path == '/api/start':
                service.start()
                message = '自动采集已启动'
            elif path == '/api/stop':
                service.stop()
                message = '采集已停止'
            elif path == '/api/prepare':
                service.prepare()
                message = '正在准备交易日与股票池'
            elif path == '/api/demo':
                service.demo()
                message = '正在加速演示，所有数据均为合成示例'
            elif path == '/api/review':
                started = service.run_review(body.get('date') or None)
                message = '复盘任务已开始' if started else '已有复盘任务正在运行'
            elif path == '/api/research/run':
                started = service.run_research()
                message = '正在本机重放历史竞价并对照收盘结果' if started else '已有回测任务正在运行'
            elif path == '/api/research/import':
                result = service.import_research(body.get('dataset'))
                self._json(dict(result,ok=True))
                return
            elif path == '/api/research/proposals':
                if service.mode != 'live' or service._auction_priority():
                    raise ValueError('参数建议归档须在实盘模式与竞价保护时段外进行')
                result = service.research_library.save_proposal(body,now_sh())
                self._json(dict(result,ok=True,message='参数建议已归档，未更改实盘权重；需要新的日期验证'))
                return
            elif path == '/api/reports/load':
                service.load_report(body.get('date'))
                message = '已读取本机历史报告，未重新请求行情'
            elif path == '/api/reports/enrich':
                started = service.enrich_report(body.get('date'), body.get('review_id'))
                message = '正在补充所选日期的官方观察' if started else '已有官方观察任务，请等待完成'
            elif path == '/api/reports/save':
                saved = service.markdown_report(body.get('date'), body.get('include_ai',False), body.get('provider'),
                    body.get('review_id'), body.get('baseline'), save=True)
                query = {k:saved[k] for k in ('filename','mode','sha256')}
                self._json(dict(saved, ok=True, message='Markdown 已保存在本机报告目录',
                                download_url='/api/reports/download?'+urlencode(query)))
                return
            elif path == '/api/config':
                service.update_config(body)
            elif path == '/api/watchlist/add':
                started = service.add_watchlist(body.get('codes'))
                message = '正在核对代码，完成后从下一轮开始跟踪' if started else '已有添加关注任务，请等待完成'
            elif path == '/api/watchlist/remove':
                service.remove_watchlist(body.get('code'))
                message = '已移除自选；同时属于自动重点池的股票仍会跟踪，历史观察保留'
            elif path == '/api/stocks/analyze':
                started = service.query_stock(body.get('code'),body.get('date') or None)
                message = '个股查询已开始' if started else '已有个股查询任务，请等待完成'
            elif path == '/api/trends/refresh':
                started = service.refresh_trends()
                message = '走势筛选已开始' if started else '已有走势筛选任务，请等待完成'
            elif path == '/api/credentials':
                if service.running or any(j.get('status') == 'running' for j in service.jobs.values()):
                    raise ValueError('请先停止采集并等待任务结束后更新凭据')
                save_finance_key(body.get('api_key',''))
                service.provider = None
                service.start()
                message = 'Key 已保存在本机用户凭据文件，自动服务已启动'
            elif path == '/api/llm-config':
                save_profile(dict(body, provider=body.get('provider', 'custom')))
                if 'provider' not in body:
                    select_provider('custom')
                service.touch()
                message = '当前服务商配置已保存，Key 不会回显'
            elif path == '/api/llm-select':
                select_provider(body.get('provider'))
                service.touch()
                message = 'AI 服务已切换'
            elif path == '/api/llm-test':
                started = service.run_llm_test(body.get('provider'))
                message = '正在检查授权与模型列表，不生成回答' if started else '连接测试正在进行'
            elif path == '/api/llm':
                started = service.run_llm(str(body.get('question','')), body.get('provider'), body.get('review_date'), body.get('review_id'))
                message = '正在调用所选模型，结构化排名不受模型速度影响' if started else '已有 AI 分析正在进行'
            else:
                self._json({'ok':False,'message':'操作不存在'},404)
                return
            self._json({'ok':True,'message':message,**({'started':started} if started is not None else {})})
        except (ValueError, TypeError, KeyError) as exc:
            # json decoder excerpts could contain credentials; do not emit those.
            safe = '输入格式无效' if isinstance(exc,(json.JSONDecodeError,TypeError,KeyError)) else str(exc)
            self._json({'ok':False,'message':safe},400)
        except OSError:
            self._json({'ok':False,'message':'本机文件写入或网络访问失败，请检查目录权限'},500)
        except Exception:
            self._json({'ok':False,'message':'操作未完成，请检查服务状态'},500)


def serve(port=8765):
    service = Service()
    server = LocalServer(('127.0.0.1',port),service)
    if finance_key():
        service.start()
    try:
        server.serve_forever(poll_interval=.5)
    finally:
        service.close()
        server.server_close()
