"""Offline release discovery, admission and recovery. All release sources and services are fixtures."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import release_updates as releases
from deploy import update_agent as agent
from cryptography.fernet import Fernet
from service import LabError, ModelLab

OLD = 'a' * 40
NEW = 'b' * 40
TARGET = {'tag': 'v1.4.0', 'revision': NEW}
REQUEST = {**TARGET, 'from_revision': OLD}


class DiscoveryTests(unittest.TestCase):
    def test_published_stable_tag_peels_to_exact_commit(self):
        responses = [{'tag_name': TARGET['tag'], 'draft': False, 'prerelease': False},
                     {'object': {'type': 'tag', 'sha': OLD}}, {'object': {'type': 'commit', 'sha': NEW}}]
        with patch.object(releases, 'github', side_effect=responses) as read:
            self.assertEqual(releases.latest_release(), TARGET)
            self.assertEqual([c.args[0] for c in read.call_args_list],
                             ['/releases/latest', '/git/ref/tags/v1.4.0', '/git/tags/' + OLD])

    def test_empty_invalid_and_prerelease_not_installable(self):
        with patch.object(releases, 'github', return_value=None):
            self.assertIsNone(releases.latest_release())
        for tag, draft, prerelease in [('nightly', False, False), ('v1.4.0', True, False), ('v1.4.0', False, True)]:
            with self.subTest(tag=tag), patch.object(releases, 'github', return_value=dict(tag_name=tag, draft=draft, prerelease=prerelease)), self.assertRaises(ValueError):
                releases.latest_release()
        self.assertGreater(releases.version_tuple('v1.10.0'), releases.version_tuple('v1.9.11'))

    def test_signed_repo_events_only_wake_discovery(self):
        with tempfile.TemporaryDirectory() as d:
            key_file = Path(d) / 'hook'
            key = os.urandom(32).hex().encode()
            key_file.write_bytes(key + b'\n')
            monitor = releases.ReleaseMonitor(OLD, key_file)
            def deliver(repo, action, valid=True):
                raw = json.dumps({'action': action, 'repository': {'full_name': repo}}).encode()
                signature = hmac.new(key, raw, hashlib.sha256).hexdigest() if valid else 'unverified'
                return monitor.webhook({'X-Hub-Signature-256': 'sha256=' + signature, 'X-GitHub-Event': 'release'}, raw)
            self.assertFalse(deliver(releases.REPO, 'published', False))
            self.assertTrue(deliver('other/project', 'published'))
            self.assertTrue(deliver(releases.REPO, 'edited'))
            self.assertFalse(monitor.wake.is_set())
            self.assertTrue(deliver(releases.REPO, 'published'))
            self.assertTrue(monitor.wake.is_set())

    def test_managed_status_requires_matching_installed_revision_and_socket(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(releases, 'STATE', Path(tmp)), patch.object(releases, 'SOCKET') as sock:
            sock.is_socket.return_value = True
            monitor = releases.ReleaseMonitor(OLD)
            self.assertFalse(monitor.status()['managed'])
            (Path(tmp) / 'managed-revision').write_text(NEW)
            self.assertFalse(monitor.status()['managed'])
            (Path(tmp) / 'managed-revision').write_text(OLD)
            self.assertTrue(monitor.status()['managed'])
            sock.is_socket.return_value = False
            self.assertFalse(monitor.status()['managed'])

    def test_unmanaged_and_stale_selection_never_send_a_request(self):
        monitor = releases.ReleaseMonitor(OLD)
        with patch.object(releases, 'send_install', return_value={'accepted': True}) as send:
            with patch.object(monitor, 'status', return_value=dict(managed=False)), self.assertRaises(LabError):
                monitor.install(TARGET)
            with patch.object(monitor, 'status', return_value=dict(managed=True, available=True, latest=TARGET)):
                for target in ({**TARGET, 'revision': OLD}, {**TARGET, 'url': 'https://example.invalid'}):
                    with self.assertRaises(LabError):
                        monitor.install(target)
                send.assert_not_called()
                self.assertEqual(monitor.install(TARGET), {'accepted': True})
                send.assert_called_once_with(REQUEST)

    def test_closed_or_failing_source_still_produces_a_status(self):
        monitor = releases.ReleaseMonitor(OLD)
        def unavailable():
            monitor.stop.set()
            raise OSError()
        with patch.object(releases, 'latest_release', side_effect=unavailable), patch.object(monitor.wake, 'wait'):
            monitor._loop()
        self.assertEqual(monitor.status()['source'], 'unavailable')
        self.assertFalse(monitor.status()['available'])


class SnapshotTests(unittest.TestCase):
    """Actual Git objects from an isolated two-release repository, no network or published commits."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'git'
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.repo)], check=True)
        files = {'release_updates.py': b"VERSION = '1.3.0'\nUPDATE_EPOCH = 1\n", 'server.py': b'# OLD\n',
                 'web/index.html': b'old page\n', 'deploy/runtime-files.txt': b'server.py\nweb/index.html\nrelease_updates.py\n',
                 'requirements.txt': b'# fixture\n'}
        for name, data in files.items():
            p = self.repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        self.base = self.commit()
        self.old = self.root / 'old'
        for name, data in agent.program(self.repo, self.base)[0].items():
            p = self.old / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        (self.repo / 'release_updates.py').write_text("VERSION = '1.4.0'\nUPDATE_EPOCH = 1\n", encoding='ascii', newline='\n')
        (self.repo / 'server.py').write_bytes(b'# NEW\n')
        self.releases = self.root / 'releases'
        self.releases.mkdir()
        if os.name == 'nt':
            # Directory fsync is Linux-specific; file writes/renames and Git validation remain real.
            mock = patch.object(agent, 'sync_dir')
            mock.start()
            self.addCleanup(mock.stop)

    def commit(self):
        args = ['git', '-C', str(self.repo), '-c', 'core.autocrlf=false', '-c', 'core.hooksPath=' + os.devnull,
                '-c', 'user.name=Fixture', '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgSign=false']
        subprocess.run(args + ['add', '--', '.'], stdout=subprocess.DEVNULL, check=True)
        subprocess.run(args + ['commit', '-qm', 'fixture'], check=True)
        return subprocess.check_output(args + ['rev-parse', 'HEAD']).decode().strip()

    def prepare(self):
        sha = self.commit()
        subprocess.run(['git', '-C', str(self.repo), 'update-ref', 'refs/tags/v1.4.0', sha], check=True)
        return {'tag': 'v1.4.0', 'revision': sha, 'from_revision': self.base}

    def test_exact_tag_main_integrity_and_idempotent_materialization(self):
        request = self.prepare()
        stage = agent.materialize(self.repo, request, self.old, self.releases)
        self.assertEqual((stage / 'server.py').read_bytes(), b'# NEW\n')
        self.assertEqual(releases.installed_revision(stage), request['revision'])
        self.assertFalse((stage / 'requirements.txt').exists())
        self.assertEqual(agent.materialize(self.repo, request, self.old, self.releases), stage)
        (stage / 'server.py').write_text('changed')
        with self.assertRaisesRegex(agent.Refused, 'local_changes'):
            agent.materialize(self.repo, request, self.old, self.releases)

    def test_local_drift_and_moved_tag_refused(self):
        request = self.prepare()
        with self.assertRaisesRegex(agent.Refused, 'release_changed'):
            agent.materialize(self.repo, {**request, 'revision': self.base}, self.old, self.releases)
        (self.old / 'extra.py').write_bytes(b'# local')
        with self.assertRaisesRegex(agent.Refused, 'local_changes'):
            agent.materialize(self.repo, request, self.old, self.releases)

    def test_dependency_change_requires_manual_upgrade(self):
        (self.repo / 'requirements.txt').write_bytes(b'# new dependency contract\n')
        with self.assertRaisesRegex(agent.Refused, 'manual_upgrade'):
            agent.materialize(self.repo, self.prepare(), self.old, self.releases)

    def test_manifest_cannot_include_data_or_parent_paths(self):
        with (self.repo / agent.RUNTIME).open('ab') as f:
            f.write(b'../local.sqlite3\n')
        with self.assertRaisesRegex(agent.Refused, 'invalid_artifact'):
            agent.materialize(self.repo, self.prepare(), self.old, self.releases)

    def test_target_off_main_never_materializes(self):
        request = self.prepare()
        subprocess.run(['git', '-C', str(self.repo), 'update-ref', 'refs/heads/main', self.base], check=True)
        with self.assertRaises(subprocess.CalledProcessError):
            agent.materialize(self.repo, request, self.old, self.releases)
        self.assertFalse(any(self.releases.iterdir()))


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old, self.new = self.root / OLD, self.root / NEW
        for d in (self.old, self.new):
            d.mkdir()
            (d / 'REVISION').write_text(d.name + '\n')
        self.state = self.root / 'state'
        self.state.mkdir()
        self.app = agent.Installer(self.root, self.state, 'fixture.service', 8091)
        if os.name == 'nt':
            mock = patch.object(agent, 'sync_dir')
            mock.start()
            self.addCleanup(mock.stop)
        try:
            self.app.current.symlink_to(self.old, target_is_directory=True)
        except OSError:
            # Windows runners without symlink privilege still exercise the full state machine.
            self.app.current = Mock()
            self.app.current.is_symlink.return_value = True
            self.app.current.resolve.return_value = self.old
            self.app.switch = lambda dest: setattr(self.app.current.resolve, 'return_value', dest)
        self.app.restart = Mock()
        self.app.stage = Mock(return_value=self.new)
        def health():
            working = self.app.current.resolve()
            return dict(status='ok', version='1.3.0' if working == self.old else '1.4.0', revision=working.name)
        self.app.health = health

    def status(self):
        return json.loads((self.state / 'status.json').read_bytes())

    def test_success_keeps_old_and_data(self):
        database = self.root / 'existing.sqlite3'
        database.write_bytes(b'fixture contents')
        self.app.run(REQUEST)
        self.assertEqual(self.status()['phase'], 'installed')
        self.assertEqual(self.app.current.resolve(), self.new)
        receipts = list((self.state / 'receipts').glob('*.json'))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_bytes())['from_revision'], OLD)
        self.assertTrue(self.old.exists())
        self.assertEqual(database.read_bytes(), b'fixture contents')
        self.assertFalse(self.app.gate.exists())
        self.app.restart.assert_called_once()
        self.app.run(REQUEST)
        self.assertEqual(self.status()['reason'], 'release_changed')
        self.app.restart.assert_called_once()

    def test_busy_instance_installs_on_first_confirmation(self):
        db, key = self.root / 'lab.sqlite3', self.root / 'master.key'
        key.write_bytes(Fernet.generate_key())
        started = {name: threading.Event() for name in ('model-a', 'model-b')}
        def running(spec, cancelled):
            started[spec['model']].set()
            cancelled.wait(8)
            return {'status': 'cancelled'}
        def open_lab(runner):
            lab = ModelLab(db, key, runner, site_origin='https://gateway.example.test', update_gate=self.app.gate)
            self.addCleanup(lab.close)
            return lab
        lab = open_lab(running)
        for model in started:
            lab.save_probe(dict(name=model, model=model, enabled=True, api_key='fixture-only',
                                base_url='https://gateway.example.test/v1'))
        with lab._lock, lab._db:
            lab._db.execute('UPDATE probes SET next_run_at=0')
        lab._scheduler_tick()
        self.assertTrue(all(event.wait(2) for event in started.values()))
        schedules = {p['id']: p['next_run_at'] for p in lab.list_probes()['probes']}
        fresh = []
        def restart():
            self.assertTrue(self.app.gate.exists())
            self.assertEqual(len(lab._tasks), 2)  # Both upstream calls overlap the requested restart.
            lab.close()
            fresh.append(open_lab(Mock(return_value={'status': 'passed'})))
        self.app.restart.side_effect = restart
        self.app.run(REQUEST)
        self.assertEqual(self.status()['phase'], 'installed')
        self.assertEqual(self.app.current.resolve(), self.new)
        self.app.restart.assert_called_once()
        self.assertFalse(self.app.gate.exists())
        live, = fresh
        self.assertEqual({p['id']: p['next_run_at'] for p in live.list_probes()['probes'] if p['enabled']}, schedules)
        live._scheduler_tick()
        live._runner.assert_not_called()  # No replay of paid calls from the interrupted interval.
        self.assertEqual([r[0] for r in live._db.execute('SELECT status FROM requests')], ['cancelled'] * 2)
        self.assertEqual(live._db.execute("SELECT count(*) FROM channel_metrics WHERE status='failed'").fetchone()[0], 0)

    def test_changed_base_during_download_cannot_restart_or_modify_schedules(self):
        old_health = self.app.health()
        self.app.health = Mock(side_effect=[old_health, {**old_health, 'revision': 'c' * 40}])
        self.app.run(REQUEST)
        self.assertEqual(self.status()['reason'], 'release_changed')
        self.app.restart.assert_not_called()
        self.assertFalse(self.app.gate.exists())

    def test_failed_health_rolls_back_and_checks_old_version(self):
        original = self.app.healthy
        self.app.healthy = lambda version, sha: sha != NEW and original(version, sha)
        self.app.run(REQUEST)
        self.assertEqual(self.status()['phase'], 'rolled_back')
        self.assertEqual(self.app.current.resolve(), self.old)
        self.assertFalse(self.app.journal.exists())
        self.assertEqual(self.app.restart.call_count, 2)
        self.assertEqual(json.loads(next((self.state / 'receipts').glob('*.json')).read_bytes())['phase'], 'rolled_back')

    def test_failed_rollback_keeps_fence_until_recovery(self):
        original = self.app.healthy
        self.app.healthy = lambda *_: False
        self.app.run(REQUEST)
        self.assertEqual(self.status()['reason'], 'manual_recovery')
        self.assertTrue(self.app.journal.exists())
        self.assertTrue(self.app.gate.exists())
        self.assertFalse(self.app.submit(REQUEST))
        self.app.healthy = original
        self.app.recover()
        self.assertFalse(self.app.gate.exists())
        self.assertEqual(self.status()['phase'], 'rolled_back')

    def test_one_install_even_when_confirmed_twice(self):
        started, done, release = threading.Event(), threading.Event(), threading.Event()
        def apply_once(request):
            started.set()
            release.wait(4)
            done.set()
        with patch.object(self.app, 'run', side_effect=apply_once) as run:
            self.assertTrue(self.app.submit(REQUEST))
            try:
                self.assertTrue(started.wait(2))
                self.assertFalse(self.app.submit(REQUEST))
            finally:
                release.set()
            self.assertTrue(done.wait(2))
            run.assert_called_once_with(REQUEST)
        self.assertFalse(self.app.submit({**REQUEST, 'unit': 'different.service'}))


if __name__ == '__main__':
    unittest.main()
