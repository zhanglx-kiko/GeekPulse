"""GeekPulse: isolated HTTP adapter for Sub2API v0.2.23.

Run behind the supplied loopback-only Nginx location. No credentials are logged.
"""
from __future__ import annotations

import argparse
import html
import http.client
import ipaddress
import json
import os
import signal
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from urllib.parse import parse_qs, urlsplit

from runner import REASONING_EFFORT_BLOCKS
from service import LabError, ModelLab, normalize_multiplier
from site_config import SiteConfig

from release_updates import ReleaseMonitor, STATE, VERSION, installed_revision
SUB2API_ADAPTER = '0.2.23'
PREFIX = '/model-lab'
MAX_BODY = 131072
WEB = Path(__file__).resolve().parent / 'web'


class Sub2Adapter:
    """Fixed destination; login Bearer never enters the arbitrary relay transport."""

    def __init__(self, site):
        self.site = site

    def fetch(self, path, bearer, client_ip, user_agent):
        if path not in ('/api/v1/auth/me', '/api/v1/groups/available', '/api/v1/admin/groups/all'):
            raise LabError(502, 'ADAPTER_PATH', '登录适配接口不受支持')
        connection = http.client.HTTPConnection('127.0.0.1', self.site.sub2api_port, timeout=12)
        try:
            connection.request('GET', path, headers={
                'Authorization': bearer,
                'User-Agent': user_agent,
                'Host': self.site.authority,
                'X-Real-IP': client_ip,
                'X-Forwarded-For': client_ip,
                'X-Forwarded-Proto': 'https',
                'X-Forwarded-Host': self.site.authority,
                'Accept': 'application/json',
            })
            response = connection.getresponse()
            raw = response.read(2 * 1024 * 1024 + 1)
            if response.status == 401:
                raise LabError(401, 'LOGIN_REQUIRED', '登录已过期，请返回控制台重新登录')
            if response.status == 403:
                raise LabError(403, 'FORBIDDEN', '当前账号没有此权限')
            if response.status != 200 or len(raw) > 2 * 1024 * 1024:
                raise LabError(503, 'AUTH_UNAVAILABLE', '登录服务暂时不可用，请稍后重试')
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get('code') != 0 or 'data' not in result:
                raise LabError(503, 'ADAPTER_INCOMPATIBLE', '登录接口格式已变化，请联系管理员检查版本兼容')
            return result['data']
        except LabError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise LabError(503, 'AUTH_UNAVAILABLE', '登录服务暂时不可用，请稍后重试') from None
        finally:
            connection.close()

    def authenticate(self, headers):
        bearer = headers.get('Authorization', '')
        if not bearer.startswith('Bearer ') or len(bearer) > 8192 or len(bearer) < 16:
            raise LabError(401, 'LOGIN_REQUIRED', '请先登录本站控制台')
        client_ip = headers.get('X-GeekPulse-Client-IP', '')
        try:
            ipaddress.ip_address(client_ip)
        except ValueError:
            raise LabError(503, 'PROXY_CONFIGURATION', '入口代理配置不完整，请联系管理员') from None
        user_agent = headers.get('User-Agent', '')
        if len(user_agent) > 4096:
            raise LabError(400, 'INVALID_REQUEST', '请求头过长')
        user = self.fetch('/api/v1/auth/me', bearer, client_ip, user_agent)
        if (not isinstance(user, dict) or type(user.get('id')) is not int
                or user['id'] <= 0 or user.get('role') not in ('admin', 'user')):
            raise LabError(503, 'ADAPTER_INCOMPATIBLE', '账号接口已变化，请检查版本兼容')
        path = '/api/v1/admin/groups/all' if user['role'] == 'admin' else '/api/v1/groups/available'
        groups = self.fetch(path, bearer, client_ip, user_agent)
        if not isinstance(groups, list):
            raise LabError(503, 'ADAPTER_INCOMPATIBLE', '分组接口已变化，请检查版本兼容')
        safe_groups = [{'id': g['id'], 'name': str(g.get('name', ''))[:120],
                        'rate_multiplier': normalize_multiplier(g.get('rate_multiplier'))}
                       for g in groups if isinstance(g, dict) and type(g.get('id')) is int
                       and g['id'] > 0 and g.get('status', 'active') == 'active']
        ctx = {'id': user['id'], 'role': user['role'], 'group_ids': [g['id'] for g in safe_groups],
               'group_rates': {g['id']: g['rate_multiplier'] for g in safe_groups if g['rate_multiplier'] is not None}}
        return ctx, {'id': user['id'], 'role': user['role'],
                     'name': str(user.get('username') or '用户')[:100]}, safe_groups


class LabHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, lab, adapter=None, max_handlers=24, site_groups=None, *, site, releases=None):
        self.releases = releases
        self.lab = lab
        self.site = site
        self.adapter = adapter or Sub2Adapter(site)
        self.site_groups = site_groups
        self._handlers = threading.BoundedSemaphore(max_handlers)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self._handlers.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\nRetry-After: 2\r\n\r\n')
                request.shutdown(socket.SHUT_WR)
                # Drain a bounded request after the response; closing with unread
                # bytes can reset TCP before the client receives the 503.
                request.settimeout(0.25)
                remaining = MAX_BODY + 8192
                while remaining > 0:
                    chunk = request.recv(min(8192, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._handlers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._handlers.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'GeekPulse'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *args):
        # BaseHTTPRequestHandler includes the entire URL (possibly a legacy token query).
        # The supplied proxy snippet also disables request logging here.
        pass

    def headers_common(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; frame-src 'self' about:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.headers_common()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self, *, raw_json=False):
        if self.headers.get('Transfer-Encoding'):
            raise LabError(400, 'INVALID_REQUEST', '不支持此请求编码')
        if self.headers.get_content_type() != 'application/json':
            raise LabError(415, 'INVALID_CONTENT_TYPE', '请求必须使用 JSON')
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise LabError(400, 'INVALID_REQUEST', '请求长度无效') from None
        if size < 2 or size > MAX_BODY:
            raise LabError(413, 'REQUEST_TOO_LARGE', '请求内容过大或为空')
        try:
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError()
            if raw_json:
                return raw
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (UnicodeError, ValueError):
            raise LabError(400, 'INVALID_JSON', 'JSON 内容无效') from None

    def save_site_probe(self, ctx, groups, probe_id=None):
        # Validate before preparing a site Key; never hold the scheduler lock
        # during an original-site network operation.
        if self.server.site_groups is None:
            raise LabError(503, 'SITE_GROUPS_UNAVAILABLE', '本站分组服务暂时不可用，请稍后重试。')
        def credential_for(config, check_effort):
            return self.server.site_groups.credential(
                config['group_id'], ctx, self.headers, config['model'], config['effort'] if check_effort else None)
        return self.server.lab.save_site_probe(self.body(), probe_id, groups, credential_for,
                                              lambda group: self.server.site_groups.check_binding(group, ctx, self.headers))

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def do_PUT(self):
        self.dispatch('PUT')

    def do_DELETE(self):
        self.dispatch('DELETE')

    def do_OPTIONS(self):
        self.send_json({'error': {'code': 'SAME_ORIGIN_ONLY', 'message': '请从本站访问'}}, 403)

    def dispatch(self, method):
        try:
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                raise LabError(403, 'PROXY_REQUIRED', '请从本站入口访问')
            parsed = urlsplit(self.path)
            path = parsed.path
            if path in (PREFIX, PREFIX + '/') and method == 'GET':
                if parsed.query or path == PREFIX:
                    self.send_response(302)
                    self.headers_common()
                    self.send_header('Location', PREFIX + '/')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                return self.static('index.html')
            if path in (PREFIX + '/app.js', PREFIX + '/theme.js', PREFIX + '/styles.css', PREFIX + '/pulse.svg') and method == 'GET':
                return self.static(path.rsplit('/', 1)[-1])
            if not path.startswith(PREFIX + '/api/'):
                raise LabError(404, 'NOT_FOUND', '页面不存在')
            if self.headers.get('Origin') not in (None, self.server.site.origin):
                raise LabError(403, 'SAME_ORIGIN_ONLY', '请从本站访问')
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise LabError(403, 'SAME_ORIGIN_ONLY', '请从本站访问')
            route = path[len(PREFIX + '/api/'):]
            if route == 'health' and method == 'GET':
                health = {'status': 'ok', 'version': VERSION, 'sub2api_adapter': SUB2API_ADAPTER}
                if self.server.releases:
                    health['revision'] = self.server.releases.revision
                return self.send_json(health)
            if route == 'release-webhook' and method == 'POST':
                monitor = self.server.releases
                if not monitor or not monitor.hook_file:
                    raise LabError(404, 'NOT_FOUND', '接口不存在')
                if not monitor.webhook(self.headers, self.body(raw_json=True)):
                    raise LabError(403, 'INVALID_SIGNATURE', '发布通知签名无效。')
                return self.send_json({'ok': True}, 202)
            ctx, user, groups = self.server.adapter.authenticate(self.headers)
            lab = self.server.lab
            query = parse_qs(parsed.query)
            if route == 'bootstrap' and method == 'GET':
                return self.send_json({'user': user, 'groups': groups, 'public_origin': self.server.site.origin,
                                       'receipt_namespace': self.server.site.receipt_namespace,
                                       'presets': lab.bootstrap_presets(),
                                       'reasoning_effort_blocks': REASONING_EFFORT_BLOCKS,
                                       'limits': {'max_output_tokens': 32768, 'max_timeout_seconds': 900},
                                       'version': VERSION})
            if route == 'dashboard' and method == 'GET':
                hours = query.get('hours', ['24'])[0]
                if hours not in ('1', '24'):
                    raise LabError(400, 'INVALID_RANGE', '仅支持近1小时或24小时')
                return self.send_json(lab.dashboard(ctx, int(hours)))
            if route == 'runs':
                if method == 'GET':
                    return self.send_json(lab.list_runs(ctx, query.get('scope', ['mine'])[0], query.get('kind', [None])[0]))
                if method == 'POST':
                    return self.send_json(lab.start_self(ctx['id'], self.body()), 202)
            parts = route.split('/')
            if len(parts) == 2 and parts[0] == 'requests' and method == 'GET':
                return self.send_json(lab.get_request(parts[1], ctx))
            if len(parts) == 2 and parts[0] == 'runs' and method == 'GET':
                return self.send_json(lab.get_run(parts[1], ctx))
            if len(parts) == 2 and parts[0] == 'runs' and method == 'DELETE':
                return self.send_json(lab.delete_run(parts[1], ctx))
            if len(parts) == 3 and parts[0] == 'runs' and parts[2] == 'cancel' and method == 'POST':
                return self.send_json(lab.cancel_run(parts[1], ctx))
            if parts[0] == 'admin':
                if ctx['role'] != 'admin':
                    raise LabError(403, 'ADMIN_REQUIRED', '此操作仅限管理员')
                if route in ('admin/updates', 'admin/updates/check', 'admin/updates/install'):
                    monitor = self.server.releases
                    if not monitor:
                        raise LabError(503, 'updates_unavailable', '版本检查尚未就绪。')
                    if route == 'admin/updates' and method == 'GET':
                        return self.send_json(monitor.status())
                    if route == 'admin/updates/check' and method == 'POST':
                        self.body()
                        monitor.wake.set()
                        return self.send_json({'checking': True}, 202)
                    if route == 'admin/updates/install' and method == 'POST':
                        return self.send_json(monitor.install(self.body()), 202)
                if route == 'admin/probes':
                    if method == 'GET':
                        return self.send_json(lab.list_probes())
                    if method == 'POST':
                        return self.send_json(self.save_site_probe(ctx, groups), 201)
                if len(parts) == 4 and parts[:2] == ['admin', 'groups'] and parts[3] == 'models' and method == 'GET':
                    if not parts[2].isascii() or not parts[2].isdigit() or not 0 < int(parts[2]) < 2**63:
                        raise LabError(400, 'INVALID_GROUP', '分组编号无效。')
                    if self.server.site_groups is None:
                        raise LabError(503, 'SITE_GROUPS_UNAVAILABLE', '本站分组服务暂时不可用，请稍后重试。')
                    return self.send_json(self.server.site_groups.catalog(int(parts[2]), ctx, self.headers))
                if len(parts) == 3 and parts[:2] == ['admin', 'probes']:
                    if method == 'PUT':
                        return self.send_json(self.save_site_probe(ctx, groups, parts[2]))
                    if method == 'DELETE':
                        lab.delete_probe(parts[2])
                        return self.send_json({'ok': True})
                if len(parts) == 4 and parts[:2] == ['admin', 'probes'] and parts[3] == 'run' and method == 'POST':
                    return self.send_json(lab.start_probe(ctx['id'], parts[2], self.body().get('request_id')), 202)
            raise LabError(404, 'NOT_FOUND', '接口不存在')
        except LabError as exc:
            self.close_connection = True
            self.send_json({'error': {'code': exc.code, 'message': exc.message}}, exc.status)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception as exc:
            self.close_connection = True
            print('request_failure class=' + type(exc).__name__, flush=True)
            self.send_json({'error': {'code': 'INTERNAL_ERROR', 'message': '服务暂时不可用，请稍后重试'}}, 500)

    def static(self, name):
        try:
            content = (WEB / name).read_bytes()
        except OSError:
            raise LabError(503, 'ASSET_UNAVAILABLE', '页面正在更新，请稍后刷新') from None
        if name == 'index.html':
            site = self.server.site
            content = Template(content.decode('utf-8')).substitute(
                site_name=html.escape(site.site_name, quote=True), logo_path=html.escape(site.logo_path, quote=True)
            ).encode('utf-8')
        mime = {'index.html': 'text/html', 'app.js': 'text/javascript', 'theme.js': 'text/javascript',
                'styles.css': 'text/css', 'pulse.svg': 'image/svg+xml'}[name]
        self.send_response(200)
        self.headers_common()
        self.send_header('Content-Type', mime + '; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8091)
    parser.add_argument('--data', default='/var/lib/geekpulse/lab.sqlite3')
    parser.add_argument('--key', default='/etc/geekpulse/master.key')
    args = parser.parse_args()
    try:
        site = SiteConfig.from_env(os.environ)
    except ValueError as exc:
        parser.error(str(exc))
    os.umask(0o077)
    monitor = ReleaseMonitor(installed_revision(WEB.parent), os.getenv('GEEKPULSE_WEBHOOK_KEY_FILE'))
    lab = ModelLab(args.data, args.key, site_origin=site.origin, update_gate=STATE / 'maintenance')
    from site_groups import SiteGroups
    server = LabHTTPServer(('127.0.0.1', args.port), lab, site=site, site_groups=SiteGroups(args.data, args.key, site), releases=monitor)
    monitor.thread.start()
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        print('GeekPulse started version=' + VERSION, flush=True)
        server.serve_forever(poll_interval=0.5)
    finally:
        monitor.close()
        server.server_close()
        lab.close()


if __name__ == '__main__':
    main()
