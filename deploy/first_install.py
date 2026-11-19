"""Interactive first install on a new Linux/systemd host. Launched from a verified release checkout."""
import argparse
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from deploy.update_agent import git, program
from release_updates import SHA, VERSION
from site_config import SiteConfig

APP = Path('/opt/geekpulse')
ETC = Path('/etc/geekpulse')
HELPER = Path('/usr/local/lib/geekpulse-updater')
SYSTEMD = Path('/etc/systemd/system')
UNITS = ('geekpulse.service', 'geekpulse-updater.service')
DATA = Path('/var/lib/geekpulse')
STATE = Path('/var/lib/geekpulse-updater')
SOCKET = Path('/run/geekpulse-updater/control.sock')
PYTHON = '/usr/bin/python3'


class InstallError(Exception):
    pass


def fresh(paths):
    for path in paths:
        if path.exists() or path.is_symlink():
            raise InstallError(f'Already present: {path}. Nothing overwritten. Check systemctl status geekpulse geekpulse-updater.')


def system(*args, output=False, timeout=60):
    return subprocess.run(args, check=True, timeout=timeout, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE if output else subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).stdout


def request(port, path, host=None):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        headers = {'Host': host, 'X-Forwarded-Proto': 'https'} if host else {}
        conn.request('GET', path, headers=headers)
        response = conn.getresponse()
        return response.status, response.read(4096)
    finally:
        conn.close()


def site_settings(domain, port):
    value = domain.strip()
    origin = value if '://' in value else 'https://' + value
    site = SiteConfig.from_env({'GEEKPULSE_PUBLIC_ORIGIN': origin, 'GEEKPULSE_SUB2API_PORT': port.strip() or '8080'})
    if site.sub2api_port == 8091:
        raise InstallError('Port 8091 is reserved for GeekPulse. Enter the local HTTP port of Sub2API.')
    return site


def existing_account():
    import grp
    import pwd
    for lookup in (pwd.getpwnam, grp.getgrnam):
        try:
            lookup('geekpulse')
            return True
        except KeyError:
            continue
    return False


def create_account():
    import grp
    system('useradd', '--system', '--user-group', '--no-create-home', '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', 'geekpulse')
    return grp.getgrnam('geekpulse').gr_gid


def preflight(site):
    fresh([APP, ETC, HELPER, STATE, SOCKET.parent, DATA,
           *(SYSTEMD / name for name in UNITS), *(SYSTEMD / (name + '.d') for name in UNITS)])
    if existing_account():
        raise InstallError('The geekpulse user or group already exists. Nothing installed.')
    # A filtered list exits 1 when nothing matches, which is the normal first install.
    listing = system('systemctl', 'list-unit-files', '--no-legend', '--no-pager', '--type=service', output=True)
    names = {line.split()[0] for line in listing.splitlines() if line.strip()}
    if any(unit.encode() in names for unit in UNITS):
        raise InstallError('An existing monitoring service uses a GeekPulse unit name. Nothing installed.')
    try:
        system('systemctl', 'is-active', '--quiet', 'nginx.service')
        system('nginx', '-t')
    except subprocess.CalledProcessError as exc:
        raise InstallError('Fix the running Nginx service/configuration first: sudo nginx -t') from exc
    try:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 8091))
    except OSError as exc:
        raise InstallError('Port 8091 is already in use. No services were changed.') from exc
    try:
        status, _ = request(site.sub2api_port, '/api/v1/auth/me', site.authority)
        if status == 401:
            return
    except (OSError, http.client.HTTPException):
        pass
    raise InstallError('Expected anonymous HTTP 401 from Sub2API on 127.0.0.1. Check the local port (not the public HTTPS port).')


def write_new(path, data, mode=0o644, group=None):
    # Exclusive creation; start private even if the caller has a permissive umask.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
        stream.write(data)
    if group is not None:
        os.chown(path, 0, group)
    path.chmod(mode)


def ready(sha, version):
    for _ in range(20):
        try:
            status, raw = request(8091, '/model-lab/api/health')
            h = json.loads(raw) if status == 200 else {}
            if (isinstance(h, dict) and h.get('status') == 'ok' and h.get('revision') == sha and h.get('version') == version
                    and request(8091, '/model-lab/api/bootstrap')[0] == 401 and SOCKET.is_socket()
                    and (STATE / 'managed-revision').read_text(encoding='ascii').strip() == sha):
                return True
        except (ValueError, OSError, http.client.HTTPException):
            pass
        time.sleep(0.5)
    return False


def install(site, sha):
    # Read program and packaging from the selected Git object, not an editable site directory.
    files, requirements = program(REPO, sha)
    packaging = {name: git(REPO, 'show', sha + ':deploy/' + name)
                 for name in (*UNITS, 'nginx-location.conf', 'update_agent.py')}
    with tempfile.TemporaryDirectory(prefix='geekpulse-wheels-') as directory:
        work = Path(directory)
        req = work / 'requirements.txt'
        req.write_bytes(requirements)
        # Resolve/download wheels before the first durable change. No global pip install.
        print('Preparing the Python dependency… / 正在准备 Python 依赖…', flush=True)
        system(PYTHON, '-I', '-m', 'venv', str(work / 'env'))
        wheels = work / 'wheels'
        try:
            system(str(work / 'env/bin/python'), '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check',
                   'download', '--only-binary=:all:', '--index-url', 'https://pypi.org/simple', '-d', str(wheels), '-r', str(req), timeout=240)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise InstallError('Cannot download compatible wheels from PyPI. Check network, Python and platform, then retry. Nothing installed.') from exc
        APP.mkdir(mode=0o755)  # Also excludes a second installer which passed preflight concurrently.
        release = APP / 'releases' / sha
        release.mkdir(parents=True)
        for name, data in files.items():
            path = release / name
            path.parent.mkdir(exist_ok=True, parents=True)
            write_new(path, data)
        system(PYTHON, '-I', '-m', 'venv', str(APP / '.venv'))
        python = str(APP / '.venv/bin/python')
        system(python, '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check', 'install',
               '--no-index', '--find-links', str(wheels), '-r', str(req), timeout=180)
        # Never print the key; it stays in memory until written to its new private file.
        key = system(python, '-I', '-c', 'from cryptography.fernet import Fernet; import sys; sys.stdout.buffer.write(Fernet.generate_key())', output=True)
    gid = create_account()
    ETC.mkdir(mode=0o750)
    os.chown(ETC, 0, gid)
    write_new(ETC / 'master.key', key, 0o640, gid)
    write_new(ETC / 'geekpulse.env', f'GEEKPULSE_PUBLIC_ORIGIN={site.origin}\nGEEKPULSE_SUB2API_PORT={site.sub2api_port}\n'.encode(), 0o640, gid)
    write_new(ETC / 'nginx-location.conf', packaging['nginx-location.conf'])
    HELPER.mkdir(mode=0o755)
    write_new(HELPER / 'update_agent.py', packaging['update_agent.py'])
    write_new(HELPER / 'release_updates.py', files['release_updates.py'])
    (APP / 'current').symlink_to(release, target_is_directory=True)
    for name in UNITS:
        write_new(SYSTEMD / name, packaging[name])
    system('systemctl', 'daemon-reload')
    try:
        system('systemctl', 'enable', '--now', *UNITS)
        if not ready(sha, VERSION):
            raise InstallError('Local health or updater check failed.')
    except (OSError, InstallError, subprocess.SubprocessError) as exc:
        try:
            system('systemctl', 'disable', '--now', *UNITS)
        except (OSError, subprocess.SubprocessError):
            pass
        raise InstallError('Startup failed. Check systemctl status geekpulse geekpulse-updater. Files, key and data were kept; follow deployment.md for recovery.') from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tag'); parser.add_argument('revision'); parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    if sys.platform != 'linux' or os.geteuid() != 0 or not Path('/run/systemd/system').is_dir():
        raise InstallError('Use sudo bash geekpulse-install.sh on the Sub2API Linux/systemd host.')
    if (not SHA.fullmatch(args.revision) or args.tag != 'v' + VERSION or
            git(REPO, 'rev-parse', '--verify', 'HEAD').decode().strip() != args.revision):
        raise InstallError('Release tag, version and checkout do not match; nothing installed.')
    wizard(args.tag, args.revision, args.check)


def wizard(tag, revision, check):
    print(f'GeekPulse {tag} | Setup / 安装')
    site = site_settings(input('Sub2API domain / 站点域名: '), input('Local Sub2API HTTP port / 本机端口 [8080]: '))
    preflight(site)
    print(f'{site.origin}/model-lab/ <- GeekPulse 127.0.0.1:8091; Sub2API 127.0.0.1:{site.sub2api_port}')
    if check:
        print('Preflight passed; nothing installed. / 预检通过，未安装。'); return
    if input('Install GeekPulse and its updater? / 安装监控和升级器？ [y/N]: ').strip().lower() not in ('y', 'yes'):
        print('Cancelled; nothing installed. / 已取消，未安装。'); return
    install(site, revision)
    print('Local services ready. / 本机服务就绪。\n'
          '1. In the existing Sub2API HTTPS server block, add / 在原站 HTTPS server 块内加一行：\n'
          '   include /etc/geekpulse/nginx-location.conf;\n'
          '   sudo nginx -t && sudo systemctl reload nginx\n'
          f'2. Open while signed in, or add this menu URL / 登录后打开，可加到原站菜单：\n   {site.origin}/model-lab/')


if __name__ == '__main__':
    try:
        main()
    except (InstallError, OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt, EOFError) as exc:
        detail = str(exc) if isinstance(exc, (InstallError, ValueError)) else 'Check prerequisites or incomplete install (docs/deployment.md). Existing data was not removed.'
        sys.exit('GeekPulse: ' + detail)
