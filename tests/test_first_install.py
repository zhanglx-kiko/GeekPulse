"""First-install wizard against an isolated layout. No host services, package downloads or model calls."""
import contextlib
import html
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy import first_install as setup

SHA = 'a' * 40
SITE = setup.site_settings('gateway.example.com', '')


class ReleaseNoticeTests(unittest.TestCase):
    def test_installer_and_runtime_include_the_complete_mit_notice(self):
        root = Path(__file__).resolve().parents[1]
        license_text = (root / 'LICENSE').read_text(encoding='utf-8')
        comment = '\n'.join('# ' + line if line else '#' for line in license_text.splitlines()) + '\n'
        self.assertIn(comment, (root / 'deploy/geekpulse-install.sh').read_text(encoding='utf-8'))
        manifest = (root / 'deploy/runtime-files.txt').read_text(encoding='ascii').splitlines()
        self.assertIn('web/license.html', manifest)
        page = (root / 'web/license.html').read_text(encoding='utf-8')
        self.assertEqual(html.unescape(page.partition('<pre>')[2].partition('</pre>')[0]), license_text)


class FirstInstallTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, suffix in dict(APP='app', ETC='etc', HELPER='helper', DATA='data', SYSTEMD='systemd',
                                 STATE='state', SOCKET='run/control.sock').items():
            self.stack.enter_context(patch.object(setup, name, self.root / suffix))
        setup.SYSTEMD.mkdir()
        self.output = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def test_settings_validate_before_writing_an_environment_file(self):
        self.assertEqual((SITE.origin, SITE.sub2api_port), ('https://gateway.example.com', 8080))
        site = setup.site_settings('https://gateway.example.com:8443/', '18080')
        self.assertEqual((site.origin, site.sub2api_port), ('https://gateway.example.com:8443', 18080))
        for host, port in [('http://example.com', ''), ('user:pw@example.com', '80'), ('example.com/x', ''),
                           ('example.com\nBAD=value',''), ('example.com', '0'), ('example.com', '8091')]:
            with self.subTest(host=host), self.assertRaises((ValueError, setup.InstallError)):
                setup.site_settings(host, port)

    def test_existing_db_key_units_and_partial_installs_stop_before_system_changes(self):
        for target in (setup.APP, setup.ETC, setup.DATA, setup.HELPER, setup.STATE, setup.SOCKET.parent,
                       *(setup.SYSTEMD / n for n in setup.UNITS), setup.SYSTEMD / 'geekpulse.service.d'):
            with self.subTest(target=target.name), patch.object(setup, 'system') as system:
                target.mkdir()
                data = target / 'preserve'; data.write_bytes(b'original')
                with self.assertRaisesRegex(setup.InstallError, 'Already present'):
                    setup.preflight(SITE)
                self.assertEqual(data.read_bytes(), b'original')
                system.assert_not_called()
                data.unlink(); target.rmdir()

    def test_unit_name_collision_or_wrong_upstream_cannot_pass_preflight(self):
        with patch.object(setup, 'existing_account', return_value=False), patch.object(setup, 'system', return_value=b'geekpulse.service disabled') as listing:
            with self.assertRaisesRegex(setup.InstallError, 'existing monitoring service'):
                setup.preflight(SITE)
            self.assertNotIn('geekpulse.service', listing.call_args.args)
        with patch.object(setup, 'existing_account', return_value=False), patch.object(setup, 'system', return_value=b'nginx.service enabled\n'), patch.object(setup.socket, 'socket'), patch.object(setup, 'request', return_value=(401, b'')):
            setup.preflight(SITE)
        with patch.object(setup, 'existing_account', return_value=False), patch.object(setup, 'system', return_value=b''), patch.object(setup.socket, 'socket'), patch.object(setup, 'request', return_value=(200, b'')) as request:
            with self.assertRaisesRegex(setup.InstallError, '401'):
                setup.preflight(SITE)
            request.assert_called_once_with(8080, '/api/v1/auth/me', SITE.authority)

    def test_preview_decline_and_confirmation(self):
        for check, answers, applies in ((True, ['', ''], False), (False, ['', '', ''], False), (False, ['', '', 'y'], True)):
            answers[0] = 'gateway.example.com'
            with self.subTest(check=check, applies=applies), patch('builtins.input', side_effect=answers), patch.object(setup, 'preflight') as preflight, patch.object(setup, 'install') as apply:
                setup.wizard('v' + setup.VERSION, SHA, check)
                preflight.assert_called_once_with(SITE)
                if applies:
                    apply.assert_called_once_with(SITE, SHA)
                else:
                    apply.assert_not_called()
        self.assertIn('include /etc/geekpulse/nginx-location.conf;', self.output.getvalue())
        self.assertIn(SITE.origin + '/model-lab/', self.output.getvalue())

    def prepare_install(self):
        fake_key = b'offline fixture, never a credential'
        system = self.stack.enter_context(patch.object(setup, 'system', return_value=fake_key))
        self.stack.enter_context(patch.object(setup, 'create_account', return_value=123))
        self.stack.enter_context(patch.object(setup.os, 'chown', create=True))
        files = {'REVISION': (SHA+'\n').encode(), 'release_updates.py': b'# release', 'web/index.html': b'page'}
        self.stack.enter_context(patch.object(setup, 'program', return_value=(files, b'fixture\n')))
        self.stack.enter_context(patch.object(setup, 'git', side_effect=lambda repo, op, blob: blob.encode()))
        if os.name == 'nt':  # Do not need Windows symlink privileges to exercise the installation sequence.
            self.stack.enter_context(patch.object(Path, 'symlink_to', lambda p, dest, **kwargs: p.write_text(str(dest))))
        return fake_key, system

    def test_install_starts_both_units_and_does_not_edit_the_gateway(self):
        key, system = self.prepare_install()
        with patch.object(setup, 'ready', return_value=True) as health:
            setup.install(SITE, SHA)
        health.assert_called_once_with(SHA, setup.VERSION)
        self.assertEqual((setup.APP / 'releases' / SHA / 'REVISION').read_text().strip(), SHA)
        self.assertEqual((setup.ETC / 'master.key').read_bytes(), key)
        self.assertNotIn(key.decode(), self.output.getvalue())
        self.assertIn('GEEKPULSE_SUB2API_PORT=8080', (setup.ETC / 'geekpulse.env').read_text())
        self.assertEqual(sorted(p.name for p in setup.SYSTEMD.iterdir()), sorted(setup.UNITS))
        self.assertTrue((setup.HELPER / 'update_agent.py').exists())
        self.assertEqual([c.args for c in system.call_args_list if c.args[0] == 'systemctl'],
                         [('systemctl','daemon-reload'), ('systemctl','enable','--now',*setup.UNITS)])
        with self.assertRaisesRegex(setup.InstallError, 'Already present'):
            setup.preflight(SITE)
        self.assertEqual((setup.ETC / 'master.key').read_bytes(), key)

    def test_startup_failure_stops_only_own_units_and_preserves_key(self):
        key, system = self.prepare_install()
        with patch.object(setup, 'ready', return_value=False), self.assertRaisesRegex(setup.InstallError, 'Startup failed'):
            setup.install(SITE, SHA)
        system.assert_called_with('systemctl', 'disable', '--now', *setup.UNITS)
        self.assertEqual((setup.ETC / 'master.key').read_bytes(), key)

    def test_dependency_failure_leaves_no_installation_behind(self):
        _, system = self.prepare_install()
        system.side_effect = [b'', subprocess.CalledProcessError(1, ['pip'])]
        with self.assertRaisesRegex(setup.InstallError, 'Nothing installed'):
            setup.install(SITE, SHA)
        self.assertFalse(setup.APP.exists()); self.assertFalse(setup.ETC.exists())

    def test_never_overwrites_even_if_file_appears_after_preflight(self):
        target = self.root / 'master.key'; target.write_bytes(b'original fixture')
        with self.assertRaises(FileExistsError):
            setup.write_new(target, b'new')
        self.assertEqual(target.read_bytes(), b'original fixture')

    def test_health_requires_exact_revision_authenticated_boundary_and_ready_updater(self):
        import json
        setup.STATE.mkdir(); (setup.STATE / 'managed-revision').write_text(SHA)
        for actual, anon, agent_ready, expected in ((SHA,401,True,True), ('b'*40,401,True,False), (SHA,200,True,False), (SHA,401,False,False)):
            def response(port, path):
                return (200,json.dumps(dict(status='ok',version=setup.VERSION,revision=actual)).encode()) if path.endswith('health') else (anon,b'')
            with self.subTest(actual=actual, anon=anon), patch.object(setup,'request',side_effect=response), patch.object(Path,'is_socket',return_value=agent_ready), patch.object(setup.time,'sleep'):
                self.assertEqual(setup.ready(SHA, setup.VERSION), expected)
