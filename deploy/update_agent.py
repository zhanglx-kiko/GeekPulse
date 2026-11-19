"""Linux/systemd updater, installed separately from the application in a root-owned directory."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
from datetime import datetime, timezone
import socket
import subprocess
import sys
import tempfile
import threading
import time

# The installed pair lives outside the switchable releases. Never import candidate code.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_updates import REPO, SHA, SOCKET, STATE, UPDATE_EPOCH, installed_revision, latest_release, version_tuple

ROOT = Path('/opt/geekpulse')
RUNTIME = 'deploy/runtime-files.txt'
RUNTIME_NAME = re.compile(r'[a-z][a-z0-9_]*\.py|web/[a-z][a-z0-9_-]*\.(?:html|css|js|svg)')


class Refused(Exception):
    """Only fixed, public reason codes cross the privilege boundary."""


def atomic(path, data, mode=0o644):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(data.encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())
            os.chmod(tmp, mode)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    os.replace(tmp, path)
    sync_dir(path.parent)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def git(repo, *args):
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0', 'LC_ALL': 'C'}
    options = ['-c', 'core.hooksPath=/dev/null', '-c', 'credential.helper=', '-c', 'protocol.allow=never',
               '-c', 'protocol.https.allow=always', '-c', 'http.followRedirects=false', '-c', 'fetch.fsckObjects=true']
    return subprocess.run(['git', '--no-replace-objects', *options, '-C', str(repo), *args], env=env,
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          timeout=120, check=True).stdout


def valid_request(value):
    if not isinstance(value, dict) or value.keys() != {'tag', 'revision', 'from_revision'}:
        return False
    if not all(isinstance(v, str) for v in value.values()):
        return False
    try:
        version_tuple(value['tag'])
    except ValueError:
        return False
    return bool(SHA.fullmatch(value['revision']) and SHA.fullmatch(value['from_revision']))


def program(repo, revision):
    entries = {}
    for record in git(repo, 'ls-tree', '-rz', revision).split(b'\0'):
        if record:
            meta, name = record.split(b'\t', 1)
            entries[name] = meta.split()

    def blob(name):
        entry = entries.get(name.encode('ascii'))
        if not entry or entry[0] not in (b'100644', b'100755') or entry[1] != b'blob':
            raise Refused('invalid_artifact')
        oid = entry[2].decode('ascii')
        if int(git(repo, 'cat-file', '-s', oid)) > 4 * 1024 * 1024:
            raise Refused('invalid_artifact')
        data = git(repo, 'cat-file', 'blob', oid)
        if hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != oid:
            raise Refused('invalid_artifact')
        return data

    manifest = blob(RUNTIME)
    names = manifest.decode('ascii').splitlines()
    if (not {'server.py', 'release_updates.py', 'web/index.html'}.issubset(names) or
            len(names) != len(set(names)) or len(names) > 64 or not all(RUNTIME_NAME.fullmatch(n) for n in names)):
        raise Refused('invalid_artifact')
    files = {n: blob(n) for n in names}
    files[RUNTIME] = manifest
    files['REVISION'] = (revision + '\n').encode('ascii')
    return files, blob('requirements.txt')


def matches(directory, files):
    if directory.is_symlink() or any(p.is_symlink() for p in directory.rglob('*')):
        return False
    if {p.relative_to(directory).as_posix() for p in directory.rglob('*') if p.is_file()} != set(files):
        return False
    return all(not (directory / n).is_symlink() and (directory / n).is_file() and
               (directory / n).read_bytes() == data for n, data in files.items())


def materialize(repo, request, current, releases):
    tag, sha, base = request['tag'], request['revision'], request['from_revision']
    if git(repo, 'rev-parse', '--verify', 'refs/tags/' + tag + '^{commit}').decode().strip() != sha:
        raise Refused('release_changed')
    git(repo, 'merge-base', '--is-ancestor', sha, 'refs/heads/main')
    git(repo, 'merge-base', '--is-ancestor', base, sha)
    old, requirements = program(repo, base)
    files, next_requirements = program(repo, sha)
    if not matches(current, old):
        raise Refused('local_changes')
    code = files['release_updates.py'].decode('utf-8')
    if (requirements != next_requirements or not re.search(r'^UPDATE_EPOCH = ' + str(UPDATE_EPOCH) + '$', code, re.M)
            or not re.search(r"^VERSION = '" + re.escape(tag[1:]) + "'$", code, re.M)):
        raise Refused('manual_upgrade')
    destination = releases / sha
    if destination.exists():
        if destination.is_symlink() or not matches(destination, files):
            raise Refused('local_changes')
        return destination
    with tempfile.TemporaryDirectory(dir=releases) as temp:
        stage = Path(temp) / 'app'
        stage.mkdir(mode=0o755)
        for name, data in files.items():
            p = stage / name
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            with p.open('xb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            p.chmod(0o644)
        for d in [stage / 'deploy', stage / 'web', stage]:
            sync_dir(d)
        stage.rename(destination)
        sync_dir(releases)
    return destination


class Installer:
    def __init__(self, root, state, unit, port):
        self.root, self.state, self.unit, self.port = root, state, unit, port
        self.current = root / 'current'
        self.gate = state / 'maintenance'
        self.journal = state / 'recovery.json'
        self.lock = threading.Lock()

    def status(self, phase, tag, reason=None):
        status = {'phase': phase, 'tag': tag, 'reason': reason, 'updated_at': datetime.now(timezone.utc).isoformat()}
        atomic(self.state / 'status.json', json.dumps(status))

    def receipt(self, phase, record):
        receipt = {key: record[key] for key in ('tag', 'revision', 'from_revision')}
        receipt.update(phase=phase, finished_at=datetime.now(timezone.utc).isoformat())
        folder = self.state / 'receipts'
        folder.mkdir(mode=0o700, exist_ok=True)
        atomic(folder / (str(time.time_ns()) + '-' + record['revision'] + '.json'), json.dumps(receipt), 0o600)

    def health(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            conn.request('GET', '/model-lab/api/health')
            r = conn.getresponse()
            if r.status != 200:
                raise ValueError('health')
            return json.loads(r.read(4096))
        finally:
            conn.close()

    def restart(self):
        subprocess.run(['/usr/bin/systemctl', 'restart', self.unit], check=True, timeout=50,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def healthy(self, version, sha):
        for _ in range(20):
            try:
                h = self.health()
                if h.get('status') == 'ok' and (h.get('version'), h.get('revision')) == (version, sha):
                    return True
            except (ValueError, OSError, http.client.HTTPException):
                pass
            time.sleep(0.5)
        return False

    def switch(self, target):
        link = self.root / '.geekpulse-next'
        link.unlink(missing_ok=True)
        link.symlink_to(target, target_is_directory=True)
        os.replace(link, self.current)
        sync_dir(self.root)

    def stage(self, request, old):
        if latest_release() != {'tag': request['tag'], 'revision': request['revision']}:
            raise Refused('release_changed')
        with tempfile.TemporaryDirectory() as temp:
            git(temp, 'init', '--bare', '--quiet')
            git(temp, 'fetch', '--atomic', '--no-tags', 'https://github.com/' + REPO + '.git',
                'refs/heads/main:refs/heads/main', 'refs/tags/{0}:refs/tags/{0}'.format(request['tag']))
            return materialize(temp, request, old, self.root / 'releases')

    def clear(self):
        self.journal.unlink(missing_ok=True)
        sync_dir(self.state)
        self.gate.unlink(missing_ok=True)
        sync_dir(self.state)

    def recover(self):
        record = json.loads(self.journal.read_text(encoding='utf-8'))
        old = Path(record['old'])
        target = self.current.resolve(strict=True)
        if target not in (old, Path(record['new'])) or installed_revision(old) != record['from_revision']:
            raise Refused('manual_recovery')
        atomic(self.gate, record['revision'])
        if target != old:
            self.switch(old)
        # A failed restart may have left the service stopped even with the old link restored.
        self.restart()
        if not self.healthy(record['old_version'], record['from_revision']):
            raise Refused('manual_recovery')
        atomic(self.state / 'managed-revision', record['from_revision'])
        self.receipt('rolled_back', record)
        self.status('rolled_back', record['tag'])
        self.clear()

    def run(self, request):
        tag, sha = request['tag'], request['revision']
        try:
            if self.journal.exists() or self.gate.exists():
                raise Refused('manual_recovery')
            if not self.current.is_symlink():
                raise Refused('manual_upgrade')
            old = self.current.resolve(strict=True)
            before = self.health()
            if (before.get('status') != 'ok' or before.get('revision') != request['from_revision'] or
                    installed_revision(old) != request['from_revision'] or
                    version_tuple(tag) <= version_tuple('v' + before['version'])):
                raise Refused('release_changed')
            self.status('verifying', tag)
            candidate = self.stage(request, old)
            # Keep new jobs out of the switchover; existing jobs do not veto a restart.
            atomic(self.gate, sha)
            current = self.health()
            if current.get('status') != 'ok' or current.get('revision') != request['from_revision']:
                raise Refused('release_changed')
            record = {**request, 'old': str(old), 'new': str(candidate), 'old_version': before['version']}
            atomic(self.journal, json.dumps(record), mode=0o600)
            self.status('installing', tag)
            self.switch(candidate)
            self.restart()
            if not self.healthy(tag[1:], sha):
                raise Refused('health_check')
            atomic(self.state / 'managed-revision', sha)
            self.receipt('installed', record)
            self.status('installed', tag)
            self.clear()
        except Exception as exc:
            if self.journal.exists():
                try:
                    self.recover()
                except Exception:
                    self.status('failed', tag, 'manual_recovery')
            else:
                self.gate.unlink(missing_ok=True)
                self.status('failed', tag, str(exc) if isinstance(exc, Refused) else 'update_failed')

    def submit(self, request):
        if not valid_request(request) or not self.lock.acquire(blocking=False):
            return False
        if self.journal.exists() or self.gate.exists():
            self.lock.release()
            return False

        def work():
            try:
                self.run(request)
            finally:
                self.lock.release()

        try:
            self.status('verifying', request['tag'])
            threading.Thread(target=work, name='install-release').start()
        except BaseException:
            self.lock.release()
            raise
        return True


def main():
    import fcntl
    import grp
    unit = os.environ.get('GEEKPULSE_UPDATE_UNIT', 'geekpulse.service')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+\.service', unit):
        raise SystemExit('invalid unit')
    installer = Installer(ROOT, STATE, unit, int(os.environ.get('GEEKPULSE_UPDATE_PORT', '8091')))
    group = grp.getgrnam(os.environ.get('GEEKPULSE_UPDATE_GROUP', 'geekpulse')).gr_gid
    with (STATE / 'agent.lock').open('a') as lock, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if installer.journal.exists():
            installer.recover()
        elif installer.gate.exists():
            installer.gate.unlink()  # switch had not started, or was already verified
        status = STATE / 'status.json'
        if status.exists():
            saved = json.loads(status.read_text())
            if saved.get('phase') in ('verifying', 'installing'):
                installer.status('failed', saved.get('tag'), 'interrupted')
        base = installed_revision(installer.current) if installer.current.is_symlink() else None
        if base:
            atomic(STATE / 'managed-revision', base)
        else:
            (STATE / 'managed-revision').unlink(missing_ok=True)
        SOCKET.unlink(missing_ok=True)
        listener.bind(str(SOCKET))
        os.chown(SOCKET, 0, group)
        SOCKET.chmod(0o660)
        listener.listen(4)
        while True:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(3)
                try:
                    with conn.makefile('rb') as stream:
                        raw = stream.readline(512)
                    accepted = raw.endswith(b'\n') and installer.submit(json.loads(raw))
                    conn.sendall(json.dumps({'accepted': bool(accepted)}).encode('ascii') + b'\n')
                except (ValueError, OSError):
                    continue


if __name__ == '__main__':
    main()
