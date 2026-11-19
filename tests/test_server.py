"""HTTP boundary tests: real loopback sockets; no production credentials or upstream calls."""
import http.client
import json
import pathlib
import sys
import threading
import concurrent.futures
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from server import LabHTTPServer, Sub2Adapter
from service import LabError
from site_config import SiteConfig

TEST_SITE = SiteConfig.from_env({'GEEKPULSE_PUBLIC_ORIGIN': 'https://gateway.example.com:8443', 'GEEKPULSE_SUB2API_PORT': '18080',
                                'GEEKPULSE_SITE_NAME': 'Test & Station', 'GEEKPULSE_LOGO_PATH': '/brand/icon.svg', 'GEEKPULSE_RECEIPT_NAMESPACE': 'fixture-lab'})


class FakeLab:
    def bootstrap_presets(self): return []
    def list_probes(self): return {'probes': []}
    def list_runs(self, ctx, scope, kind):
        return {'runs': [], 'verified_user': ctx['id']}
    def start_self(self, user_id, body):
        return {'id': 'test-run', 'status': 'queued', 'verified_user': user_id}
    def get_request(self, request_id, ctx):
        return {'id': 'test-run', 'status': 'running', 'verified_user': ctx['id']}
    def delete_run(self, run_id, ctx):
        return {'ok': True, 'verified_user': ctx['id'], 'deleted_id': run_id}
    def save_site_probe(self, body, probe_id, groups, credential_for, check_binding):
        config, check = self.prepare_site_probe(body, probe_id, groups)
        return self.save_probe({**config, **credential_for(config, check)}, probe_id, groups)
    def prepare_site_probe(self, body, probe_id, groups):
        if 'base_url' in body or 'api_key' in body:
            raise LabError(400, 'site_probe_only', '本站探测无需填写地址或密钥')
        if body.get('group_id') != 1:
            raise LabError(400, 'invalid_group', '分组无效')
        return {**body, 'base_url': TEST_SITE.origin, 'effort': body.get('effort', 'none')}, True
    def save_probe(self, payload, probe_id=None, groups=None):
        if payload.get('api_key') != 'TEST_ONLY_MANAGED_GROUP_KEY':
            raise AssertionError('managed key missing')
        return {'id': probe_id or 'new-probe', 'group_id': payload['group_id'], 'model': payload['model'], 'has_key': True}
    def dashboard(self, ctx, hours):
        if hours == 1: raise RuntimeError('TEST_PRIVATE_VALUE_DO_NOT_EMIT')
        return {'hours': hours}


class FakeAdapter:
    def authenticate(self, headers):
        token = headers.get('Authorization', '')
        if token not in ('Bearer test-user-session', 'Bearer test-admin-session'):
            raise LabError(401, 'LOGIN_REQUIRED', '请先登录')
        role = 'admin' if 'admin' in token else 'user'
        return ({'id': 7, 'role': role, 'group_ids': [1], 'group_rates': {1: 0}},
                {'id': 7, 'role': role}, [{'id': 1, 'name': '测试', 'rate_multiplier': 0}])


class FakeSiteGroups:
    def check_binding(self, group_id, ctx, headers):
        if group_id not in ctx['group_ids']:
            raise LabError(403, 'group_not_bindable', '分组不可用')

    def catalog(self, group_id, ctx, headers):
        if group_id != 1:
            raise LabError(403, 'group_forbidden', '分组不可用')
        return {'models': ['test-model'], 'reasoning_effort_blocks': {'test-model': ['none', 'minimal']}}

    def credential(self, group_id, ctx, headers, model, effort):
        self.catalog(group_id, ctx, headers)
        if model != 'test-model':
            raise LabError(400, 'model_unavailable', '模型不在所选分组中')
        if effort in ('none', 'minimal'):
            raise LabError(400, 'effort_not_supported', '请选择可用的推理强度')
        return {'group_id': group_id, 'api_key': 'TEST_ONLY_MANAGED_GROUP_KEY'}


class HTTPBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = LabHTTPServer(('127.0.0.1', 0), FakeLab(), FakeAdapter(), site_groups=FakeSiteGroups(), site=TEST_SITE)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)

    def request(self, path, method='GET', data=None, auth='user', headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        hs = {'Connection': 'close'}
        if auth: hs['Authorization'] = 'Bearer test-' + auth + '-session'
        if data is not None: hs['Content-Type'] = 'application/json'
        hs.update(headers or {})
        try:
            conn.request(method, path, body=data, headers=hs)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally: conn.close()

    def test_unauthenticated_private_api(self):
        status, _, body = self.request('/model-lab/api/bootstrap', auth=None)
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)['error']['code'], 'LOGIN_REQUIRED')

    def test_bootstrap_origin_is_deployment_owned(self):
        status, _, body = self.request('/model-lab/api/bootstrap', headers={
            'Origin': TEST_SITE.origin, 'Host': 'unrelated.example.org',
        })
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['public_origin'], 'https://gateway.example.com:8443')
        self.assertEqual(json.loads(body)['receipt_namespace'], 'fixture-lab')
        self.assertEqual(self.request('/model-lab/api/bootstrap', headers={
            'Origin': 'https://gateway.example.com',
        })[0], 403)

    def test_bundled_mark_is_served_without_login(self):
        status, headers, body = self.request('/model-lab/pulse.svg', auth=None)
        self.assertEqual(status, 200)
        self.assertIn('image/svg+xml', headers['Content-Type'])
        self.assertIn('<svg', body)

    def test_same_application_renders_site_brand_without_sending_external_requests(self):
        status, _, page = self.request('/model-lab/', auth=None)
        self.assertEqual(status, 200)
        self.assertIn('<title>模型实验室 · Test &amp; Station</title>', page)
        self.assertEqual(page.count('"/brand/icon.svg"'), 2)
        self.assertNotIn('${site_name}', page)

    def test_bootstrap_uses_verified_identity(self):
        status, _, body = self.request('/model-lab/api/bootstrap?user_id=999&role=admin')
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload['user'], {'id': 7, 'role': 'user'})
        self.assertEqual(payload['groups'], [{'id': 1, 'name': '测试', 'rate_multiplier': 0}])
        self.assertNotIn('group_rates', payload)
        self.assertEqual(payload['reasoning_effort_blocks']['gpt-6.2-sol'], ['none', 'minimal'])
        self.assertEqual(payload['reasoning_effort_blocks']['gpt-6.1-terra'], ['minimal'])

    def test_user_cannot_access_any_admin_action(self):
        for path, method in [('admin/updates','GET'), ('admin/updates/check','POST'), ('admin/updates/install','POST'), ('admin/probes','GET'), ('admin/probes','POST'), ('admin/probes/x','PUT'), ('admin/probes/x','DELETE'), ('admin/probes/x/run','POST')]:
            with self.subTest(path=path, method=method):
                status, _, _ = self.request('/model-lab/api/' + path, method, '{}')
                self.assertEqual(status, 403)

    def test_admin_read(self):
        self.assertEqual(self.request('/model-lab/api/admin/probes', auth='admin')[0], 200)

    def test_group_model_catalog_requires_admin_and_valid_group(self):
        path = '/model-lab/api/admin/groups/1/models'
        self.assertEqual(self.request(path)[0], 403)
        status, _, body = self.request(path, auth='admin')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'models': ['test-model'], 'reasoning_effort_blocks': {'test-model': ['none', 'minimal']}})
        self.assertEqual(self.request('/model-lab/api/admin/groups/0/models', auth='admin')[0], 400)
        self.assertEqual(self.request('/model-lab/api/admin/groups/2/models', auth='admin')[0], 403)

    def test_probe_save_uses_server_managed_group_key(self):
        for method, path in [('POST', '/model-lab/api/admin/probes'), ('PUT', '/model-lab/api/admin/probes/existing')]:
            bad_status, _, bad_body = self.request(path, method, '{"group_id":1,"model":"test-model","effort":"none"}', auth='admin')
            self.assertEqual((bad_status, json.loads(bad_body)['error']['code']), (400, 'effort_not_supported'))
            status, _, body = self.request(path, method, '{"group_id":1,"model":"test-model","effort":"low"}', auth='admin')
            self.assertEqual(status, 201 if method == 'POST' else 200)
            self.assertTrue(json.loads(body)['has_key'])
            self.assertNotIn('MANAGED_GROUP_KEY', body)
        self.assertEqual(self.request('/model-lab/api/admin/probes', 'POST', '{"group_id":1,"model":"other"}', auth='admin')[0], 400)
        for field in ('api_key', 'base_url'):
            data = json.dumps({'group_id': 1, 'model': 'test-model', field: 'TEST_ONLY'})
            self.assertEqual(self.request('/model-lab/api/admin/probes', 'POST', data, auth='admin')[0], 400)

    def test_locked_probe_over_http_adjusts_frequency_and_deletes_with_no_catalog(self):
        from tempfile import TemporaryDirectory
        from cryptography.fernet import Fernet
        from service import ModelLab
        with TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            key = root / 'master.key'
            key.write_bytes(Fernet.generate_key())
            lab = ModelLab(root / 'test.sqlite3', key, site_origin=TEST_SITE.origin)
            try:
                with patch.object(self.server, 'lab', lab):
                    url = '/model-lab/api/admin/probes'
                    status, _, raw = self.request(url, 'POST', json.dumps(dict(group_id=1, model='test-model',
                                                        effort='low', publish=True)), auth='admin')
                    self.assertEqual(status, 201)
                    saved = json.loads(raw)
                    self.assertEqual((saved['name'], saved['identity_locked']), ('测试', True))
                    with patch.object(self.server.site_groups, 'credential', side_effect=AssertionError) as credential:
                        url += '/' + saved['id']
                        status, _, raw = self.request(url, 'PUT', '{"model":"replacement"}', auth='admin')
                        self.assertEqual((status, json.loads(raw)['error']['code']), (409, 'probe_identity_locked'))
                        with patch.object(self.server.site_groups, 'check_binding', side_effect=LabError(403, 'group_not_bindable', '无权绑定')):
                            self.assertEqual(self.request(url, 'PUT', '{"interval_minutes":20}', auth='admin')[0], 403)
                        self.assertEqual(self.request(url, 'PUT', '{"interval_minutes":20}', auth='admin')[0], 200)
                        self.assertEqual(self.request(url, 'DELETE', auth='admin')[0], 200)
                        credential.assert_not_called()
                        self.assertEqual(lab._db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)
            finally:
                lab.close()

    def test_request_recovery_requires_verified_login(self):
        path = '/model-lab/api/requests/test-receipt?user_id=999'
        self.assertEqual(self.request(path, auth=None)[0], 401)
        status, _, body = self.request(path)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['verified_user'], 7)

    def test_self_submission_user_id_is_not_trusted(self):
        status, _, body = self.request('/model-lab/api/runs', 'POST', '{"user_id":999}')
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body)['verified_user'], 7)

    def test_run_delete_uses_authenticated_identity_and_same_origin(self):
        path = '/model-lab/api/runs/test-run?user_id=999'
        self.assertEqual(self.request(path, 'DELETE', auth=None)[0], 401)
        self.assertEqual(self.request(path, 'DELETE', headers={'Origin': 'https://example.invalid'})[0], 403)
        status, _, body = self.request(path, 'DELETE')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'ok': True, 'verified_user': 7, 'deleted_id': 'test-run'})

    def test_cross_origin_request_rejected(self):
        for headers in [{'Origin': 'https://example.invalid'}, {'Sec-Fetch-Site': 'cross-site'}]:
            self.assertEqual(self.request('/model-lab/api/bootstrap', headers=headers)[0], 403)

    def test_query_token_never_authenticates(self):
        self.assertEqual(self.request('/model-lab/api/bootstrap?token=test-admin-session', auth=None)[0],401)

    def test_entry_query_removed_before_content(self):
        status, headers, body = self.request('/model-lab/?token=TEST_SECRET&user_id=99', auth=None)
        self.assertEqual(status,302)
        self.assertEqual(headers['Location'],'/model-lab/')
        self.assertEqual(headers['Referrer-Policy'],'no-referrer')
        self.assertEqual(body,'')
        self.assertNotIn('TEST_SECRET', str(headers))

    def test_health_does_not_require_login(self):
        self.assertEqual(self.request('/model-lab/api/health',auth=None)[0],200)

    def test_static_has_frame_and_script_isolation(self):
        status, headers, _ = self.request('/model-lab/',auth=None)
        self.assertEqual(status,200)
        self.assertEqual(headers['X-Frame-Options'],'SAMEORIGIN')
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertIn("script-src 'self'",headers['Content-Security-Policy'])
        self.assertNotIn("script-src 'self' 'unsafe-inline'",headers['Content-Security-Policy'])

    def test_static_path_traversal(self):
        self.assertEqual(self.request('/model-lab/../server.py',auth=None)[0],404)

    def test_invalid_body_types_and_lengths(self):
        for data, headers, expected in [('[]',{},400), ('null',{},400), ('{x}',{},400), ('{}',{'Content-Type':'text/plain'},415), ('{}',{'Content-Length':'131073'},413), ('{}',{'Transfer-Encoding':'chunked'},400)]:
            with self.subTest(data=data,headers=headers):
                self.assertEqual(self.request('/model-lab/api/runs','POST',data,headers=headers)[0],expected)

    def test_unexpected_exception_redacted(self):
        status, _, body = self.request('/model-lab/api/dashboard?hours=1')
        self.assertEqual(status,500)
        self.assertNotIn('TEST_PRIVATE',body)

    def test_bad_range_rejected(self):
        self.assertEqual(self.request('/model-lab/api/dashboard?hours=999')[0],400)


class AuthAdapterTests(unittest.TestCase):
    def headers(self):
        return {'Authorization':'Bearer synthetic-test-session', 'X-GeekPulse-Client-IP':'203.0.113.7', 'User-Agent':'Test browser UA'}

    def test_forwarded_context_and_active_groups(self):
        adapter = Sub2Adapter(TEST_SITE)
        with patch.object(adapter,'fetch',side_effect=[{'id':5,'role':'user','username':'x'},[{'id':3,'name':'active','status':'active'},{'id':4,'name':'disabled','status':'disabled'}]]) as fetch:
            ctx, user, groups = adapter.authenticate(self.headers())
            self.assertEqual(ctx, {'id':5,'role':'user','group_ids':[3], 'group_rates': {}})
            self.assertEqual(groups, [{'id':3,'name':'active','rate_multiplier':None}])
            self.assertEqual(fetch.call_args_list[0].args,('/api/v1/auth/me','Bearer synthetic-test-session','203.0.113.7','Test browser UA'))
            self.assertEqual(fetch.call_args_list[1].args[0],'/api/v1/groups/available')

    def test_admin_gets_admin_groups(self):
        adapter = Sub2Adapter(TEST_SITE)
        with patch.object(adapter,'fetch',side_effect=[{'id':5,'role':'admin'},[]]) as fetch:
            adapter.authenticate(self.headers())
            self.assertEqual(fetch.call_args_list[1].args[0],'/api/v1/admin/groups/all')

    def test_multiplier_only_exposes_active_group_safe_fields_and_valid_numbers(self):
        adapter = Sub2Adapter(TEST_SITE)
        source = [{'id': 1, 'name': 'Zero', 'rate_multiplier': 0, 'accounts': ['TEST_PRIVATE_ADMIN_FIELD']},
                  {'id': 2, 'name': 'Fractional', 'rate_multiplier': 2.5},
                  {'id': 3, 'name': 'Inactive', 'status': 'disabled', 'rate_multiplier': 8},
                  {'id': True, 'name': 'Invalid id', 'rate_multiplier': 9}]
        invalid = (None, True, False, '2', -1, float('nan'), float('inf'), 10**400)
        source += [{'id': 10 + index, 'name': 'Unknown', 'rate_multiplier': value}
                   for index, value in enumerate(invalid)]
        with patch.object(adapter, 'fetch', side_effect=[{'id': 5, 'role': 'admin'}, source]):
            ctx, _, groups = adapter.authenticate(self.headers())
        self.assertEqual(ctx['group_rates'], {1: 0, 2: 2.5})
        self.assertNotIn(3, ctx['group_ids'])
        self.assertEqual(ctx['group_ids'], [1, 2] + list(range(10, 10 + len(invalid))))
        self.assertTrue(all(set(group) == {'id', 'name', 'rate_multiplier'} for group in groups))
        self.assertTrue(all(group['rate_multiplier'] is None for group in groups[2:]))
        self.assertNotIn('TEST_PRIVATE_ADMIN_FIELD', json.dumps(groups))

    def test_group_multipliers_refresh_with_each_authorized_request(self):
        adapter = Sub2Adapter(TEST_SITE)
        with patch.object(adapter, 'fetch', side_effect=[
            {'id': 5, 'role': 'user'}, [{'id': 3, 'name': 'Allowed', 'rate_multiplier': 1}],
            {'id': 5, 'role': 'user'}, [{'id': 4, 'name': 'New access', 'rate_multiplier': 0}],
        ]) as fetch:
            first, _, _ = adapter.authenticate(self.headers())
            second, _, groups = adapter.authenticate(self.headers())
        self.assertEqual(first['group_rates'], {3: 1})
        self.assertEqual(second['group_rates'], {4: 0})
        self.assertEqual(second['group_ids'], [4])
        self.assertEqual(groups, [{'id': 4, 'name': 'New access', 'rate_multiplier': 0}])
        self.assertEqual(fetch.call_count, 4)

    def test_invalid_user_shape_fails_closed(self):
        for user in [{'id':True,'role':'admin'},{'id':5,'role':'owner'}, {'user':{'id':5,'role':'admin'}}]:
            adapter = Sub2Adapter(TEST_SITE)
            with patch.object(adapter,'fetch',return_value=user), self.assertRaises(LabError) as error:
                adapter.authenticate(self.headers())
            self.assertEqual(error.exception.code,'ADAPTER_INCOMPATIBLE')

    def test_invalid_proxy_context_never_calls_auth(self):
        for ip in ['', '127.0.0.1, 1.1.1.1','bad']:
            hs = self.headers();hs['X-GeekPulse-Client-IP'] = ip
            adapter = Sub2Adapter(TEST_SITE)
            with patch.object(adapter,'fetch') as fetch, self.assertRaises(LabError):
                adapter.authenticate(hs)
            fetch.assert_not_called()

    def test_adapter_rejects_arbitrary_destination(self):
        with self.assertRaises(LabError):
            Sub2Adapter(TEST_SITE).fetch('https://example.invalid','Bearer secret','203.0.113.7','UA')

    def test_auth_transport_keeps_local_port_and_configured_host(self):
        with patch('server.http.client.HTTPConnection') as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"code":0,"data":{}}'
            Sub2Adapter(TEST_SITE).fetch('/api/v1/auth/me', 'Bearer test-only-session', '203.0.113.7', 'Test UA')
            connection.assert_called_once_with('127.0.0.1', 18080, timeout=12)
            headers = connection.return_value.request.call_args.kwargs['headers']
            self.assertEqual(headers['Host'], TEST_SITE.authority)
            self.assertEqual(headers['X-Forwarded-Host'], TEST_SITE.authority)
            self.assertEqual(headers['X-Real-IP'], '203.0.113.7')


class HTTPAdmissionTests(unittest.TestCase):
    def test_busy_requests_are_bounded_and_recover(self):
        entered = threading.Barrier(3)
        release = threading.Event()
        class WaitingAdapter(FakeAdapter):
            def authenticate(self, headers):
                entered.wait(3)
                release.wait(3)
                return super().authenticate(headers)
        server = LabHTTPServer(('127.0.0.1',0),FakeLab(),WaitingAdapter(),max_handlers=2,site=TEST_SITE)
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path):
            conn = http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            try:
                conn.request('GET',path,headers={'Authorization':'Bearer test-user-session','Connection':'close'})
                response=conn.getresponse();response.read();return response.status
            finally:conn.close()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                pending=[pool.submit(request,'/model-lab/api/bootstrap') for _ in range(2)]
                entered.wait(3)
                self.assertEqual(request('/model-lab/api/health'),503)
                release.set()
                self.assertEqual([p.result() for p in pending],[200,200])
            self.assertEqual(request('/model-lab/api/health'),200)
        finally:
            release.set();server.shutdown();server.server_close();thread.join(3)


if __name__ == '__main__': unittest.main()
