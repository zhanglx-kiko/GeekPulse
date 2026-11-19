"""Official release discovery and the unprivileged end of the local update socket."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
from pathlib import Path
import re
import socket
import threading
import time

VERSION = '1.0.0'
UPDATE_EPOCH = 1
REPO = 'zhanglx-kiko/GeekPulse'
STATE = Path('/var/lib/geekpulse-updater')
SOCKET = Path('/run/geekpulse-updater/control.sock')
SHA = re.compile(r'[0-9a-f]{40}')
TAG = re.compile(r'v(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})')


def version_tuple(tag):
    match = TAG.fullmatch(tag) if isinstance(tag, str) else None
    if not match:
        raise ValueError('invalid release tag')
    return tuple(map(int, match.groups()))


def github(path, *, missing_ok=False):
    conn = http.client.HTTPSConnection('api.github.com', timeout=8)
    try:
        conn.request('GET', '/repos/' + REPO + path, headers={
            'User-Agent': 'GeekPulse-release-check', 'Accept': 'application/vnd.github+json'})
        response = conn.getresponse()
        if missing_ok and response.status == 404:
            return None
        if response.status != 200:
            raise ValueError('release source unavailable')
        body = response.read(512 * 1024 + 1)
        if len(body) > 512 * 1024:
            raise ValueError('release response too large')
        return json.loads(body)
    finally:
        conn.close()


def latest_release():
    release = github('/releases/latest', missing_ok=True)
    if release is None:
        return None
    tag = release['tag_name']
    version_tuple(tag)
    if release['draft'] is not False or release['prerelease'] is not False:
        raise ValueError('expected a stable release')
    obj = github('/git/ref/tags/' + tag)['object']
    for _ in range(4):
        sha = obj['sha']
        if not SHA.fullmatch(sha):
            break
        if obj['type'] == 'commit':
            return {'tag': tag, 'revision': sha}
        if obj['type'] != 'tag':
            break
        obj = github('/git/tags/' + sha)['object']
    raise ValueError('invalid release reference')


def installed_revision(directory):
    try:
        sha = (directory / 'REVISION').read_text(encoding='ascii').strip()
        return sha if SHA.fullmatch(sha) else None
    except (OSError, UnicodeError):
        return None


def _managed_revision():
    try:
        return (STATE / 'managed-revision').read_text(encoding='ascii').strip()
    except (OSError, UnicodeError):
        return None


def send_install(request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(4)
        conn.connect(str(SOCKET))
        conn.sendall(json.dumps(request).encode('ascii') + b'\n')
        with conn.makefile('rb') as stream:
            return json.loads(stream.readline(1024))


class ReleaseMonitor:
    def __init__(self, revision, hook_file=None):
        self.revision = revision
        self.hook_file = Path(hook_file) if hook_file else None
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.state = {'source': 'checking', 'latest': None, 'checked_at': None}
        self.thread = threading.Thread(target=self._loop, name='geekpulse-releases', daemon=True)

    def _loop(self):
        while not self.stop.is_set():
            self.wake.clear()
            try:
                release = latest_release()
                snapshot = {'source': 'ok', 'latest': release, 'checked_at': int(time.time())}
            except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
                snapshot = {'source': 'unavailable', 'latest': None, 'checked_at': int(time.time())}
            with self.lock:
                self.state = snapshot
            self.wake.wait(900)
            if self.wake.is_set() and not self.stop.is_set():
                self.stop.wait(max(0, 30 - (time.time() - snapshot['checked_at'])))

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread.is_alive():
            self.thread.join(1)

    def status(self):
        with self.lock:
            view = dict(self.state)
        newest = view['latest']
        view.update(current=VERSION, revision=self.revision,
                    available=bool(newest and version_tuple(newest['tag']) > version_tuple('v' + VERSION)),
                    managed=bool(self.revision and SOCKET.is_socket() and
                                 self.revision == _managed_revision()), operation=None)
        try:
            view['operation'] = json.loads((STATE / 'status.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        return view

    def install(self, target):
        from service import LabError
        view = self.status()
        if not view['managed']:
            raise LabError(409, 'updater_unavailable', '升级器未就绪，请检查 geekpulse-updater.service。')
        if not view['available'] or target != view['latest']:
            raise LabError(409, 'release_changed', '可用版本已变化，请重新检查。')
        try:
            answer = send_install({**target, 'from_revision': self.revision})
        except (OSError, ValueError):
            raise LabError(503, 'updater_unreachable', '升级确认暂时不可用，请先查询升级状态。') from None
        if answer.get('accepted') is not True:
            raise LabError(409, 'update_busy', '升级器正在处理其他操作，请查看状态。')
        return answer

    def webhook(self, headers, raw):
        if not self.hook_file:
            return False
        try:
            key = self.hook_file.read_bytes().rstrip(b'\r\n')
        except OSError:
            return False
        if len(key) < 32:
            return False
        signature = 'sha256=' + hmac.new(key, raw, hashlib.sha256).hexdigest()
        supplied = headers.get('X-Hub-Signature-256', '')
        if not supplied.isascii() or not hmac.compare_digest(signature, supplied):
            return False
        if headers.get('X-GitHub-Event') == 'release':
            event = json.loads(raw)
            if (isinstance(event, dict) and event.get('action') == 'published' and
                    isinstance(event.get('repository'), dict) and event['repository'].get('full_name') == REPO):
                self.wake.set()
        return True
