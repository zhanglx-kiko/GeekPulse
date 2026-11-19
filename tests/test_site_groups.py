"""Offline tests for strict Sub2API group model catalogs and managed keys."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service import LabError
from site_groups import SiteGroups
from site_config import SiteConfig


GROUP_ID = 7
TEST_SITE = SiteConfig.from_env({'GEEKPULSE_PUBLIC_ORIGIN': 'https://gateway.example.com:8443', 'GEEKPULSE_SUB2API_PORT': '18080'})
USER_ID = 41
HEADERS = {
    "Authorization": "Bearer test-only-session-token",
    "X-GeekPulse-Client-IP": "203.0.113.19",
    "User-Agent": "ModelLabOfflineTest/1.0",
}
ADMIN = {"id": USER_ID, "role": "admin"}


def account(mapping=None, **overrides):
    value = {
        "status": "active", "schedulable": True, "group_ids": [GROUP_ID],
        "credentials": {"model_mapping": mapping or {}},
    }
    value.update(overrides)
    return value


class FakeSite:
    """Offline fixture verifies the router-registered /api/v1/keys route."""

    def __init__(self, accounts=None, models=None):
        self.accounts = list(accounts if accounts is not None else [account({"gpt-test": "upstream-test"})])
        self.models = list(models if models is not None else ["gpt-test"])
        self.keys = []
        self.calls = []
        self.lock = threading.Lock()
        self.block_post = None
        self.post_error = None
        self.create_then_error = False

    def request(self, method, path, headers, body=None, api_key=None, request_id=None):
        with self.lock:
            self.calls.append((method, path, dict(headers), body, api_key, request_id))
        if method == "GET" and path == "/api/v1/groups/available":
            return [{"id": GROUP_ID, "status": "active"}]
        if method == "GET" and path.startswith("/api/v1/admin/accounts?"):
            self.assert_test_account_query(path)
            return {"items": self.accounts, "pages": 1}
        if method == "GET" and path.startswith("/api/v1/keys?"):
            self.assert_test_key_query(path)
            return {"items": list(self.keys), "pages": 1}
        if method == "POST" and path == "/api/v1/keys":
            if self.block_post is not None:
                self.block_post.wait(3)
            if self.post_error is not None:
                error = self.post_error
                self.post_error = None
                raise error
            key = {
                "id": 9001, "key": body["custom_key"], "group_id": GROUP_ID,
                "user_id": USER_ID, "status": "active", "quota": body["quota"],
                "quota_used": 0, "expires_at": None,
            }
            self.keys.append(key)
            if self.create_then_error:
                self.create_then_error = False
                raise LabError(502, "site_api_unavailable", "test timeout after create")
            return key
        if method == "GET" and path == "/v1/models":
            if api_key is None or not api_key.startswith("sk-lab-"):
                raise AssertionError("model catalog must use the dedicated laboratory key")
            return {"data": [{"id": model} for model in self.models]}
        raise AssertionError(f"unexpected test API request: {method} {path}")

    @staticmethod
    def assert_test_account_query(path):
        if "group=7&status=active&lite=true&page=1&page_size=100" not in path:
            raise AssertionError(f"unexpected account query: {path}")

    @staticmethod
    def assert_test_key_query(path):
        if "/api/v1/keys?group_id=7&status=active&page=1&page_size=100" not in path:
            raise AssertionError(f"unexpected key query: {path}")


class SiteGroupsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="model-lab-site-groups-test-")
        self.directory = Path(self.temp.name)
        self.key_path = self.directory / "test-only-master.key"
        self.key_path.write_bytes(Fernet.generate_key())
        self.db_path = self.directory / "lab.sqlite3"
        self.site = FakeSite()
        self.groups = SiteGroups(self.db_path, self.key_path, TEST_SITE)
        self.groups._request = self.site.request

    def tearDown(self):
        self.temp.cleanup()

    def catalog(self, groups=None, ctx=ADMIN, headers=None):
        if groups is not None:
            self.site.accounts = groups
        return self.groups.catalog(GROUP_ID, ctx, headers or HEADERS)

    def row(self):
        import sqlite3
        db = sqlite3.connect(self.db_path)
        try:
            return db.execute(
                "SELECT key_ciphertext,request_id,state,remote_key_id FROM managed_group_keys WHERE user_id=? AND group_id=?",
                (USER_ID, GROUP_ID),
            ).fetchone()
        finally:
            db.close()

    def test_catalog_intersects_schedulable_account_mapping_with_gateway_catalog(self):
        self.site.accounts = [
            account({"gpt-test": "upstream-a", "mapped-but-filtered": "upstream-b"}),
            account({"other-account-model": "upstream-c"}, group_ids=[8]),
            account({"inactive-model": "upstream-d"}, status="disabled"),
            account({"unschedulable-model": "upstream-e"}, schedulable=False),
            account({"star-model": "upstream-f"}, group_ids=[GROUP_ID, True]),
            account({"*": "wildcard"}),
        ]
        self.site.models = ["default-platform-model", "gpt-test", "gpt-test"]

        self.assertEqual(self.catalog()["models"], ["gpt-test"])
        paths = [call[1] for call in self.site.calls]
        self.assertIn("/api/v1/groups/available", paths)
        self.assertTrue(any(path.startswith("/api/v1/admin/accounts?group=7&") for path in paths))
        self.assertNotIn("default-platform-model", self.catalog()["models"])

    def test_unbindable_admin_visible_group_is_rejected_before_account_or_key_access(self):
        self.site.request = self._recording_unbindable
        self.groups._request = self.site.request
        with self.assertRaises(LabError) as raised:
            self.groups.catalog(GROUP_ID, ADMIN, HEADERS)
        self.assertEqual(raised.exception.code, "group_not_bindable")
        self.assertEqual([call[1] for call in self.site.calls], ["/api/v1/groups/available"])

    def _recording_unbindable(self, method, path, headers, body=None, api_key=None, request_id=None):
        self.site.calls.append((method, path, headers, body, api_key, request_id))
        if path == "/api/v1/groups/available":
            return [{"id": GROUP_ID + 1, "status": "active"}]
        raise AssertionError("group authorization must run before other API calls")

    def test_non_admin_or_missing_trusted_request_identity_is_rejected(self):
        for ctx, headers, code in [
            ({"id": USER_ID, "role": "user"}, HEADERS, "ADMIN_REQUIRED"),
            (ADMIN, {**HEADERS, "Authorization": ""}, "LOGIN_REQUIRED"),
            (ADMIN, {**HEADERS, "X-GeekPulse-Client-IP": "not-an-ip"}, "PROXY_CONFIGURATION"),
        ]:
            with self.subTest(code=code), self.assertRaises(LabError) as raised:
                self.groups.catalog(GROUP_ID, ctx, headers)
            self.assertEqual(raised.exception.code, code)
        self.assertEqual(self.site.calls, [])

    def test_no_explicit_mapping_returns_empty_message_without_creating_key(self):
        self.site.accounts = [account({"*": "anything"}), account({})]
        result = self.catalog()
        self.assertEqual(result, {
            "models": [],
            "reasoning_effort_blocks": {},
            "message": "该分组账号未配置实验室可用的模型，请检查原站模型映射。",
        })
        self.assertFalse(any(call[0] == "POST" for call in self.site.calls))
        self.assertIsNone(self.row())

    def test_effort_restrictions_use_alias_targets_without_exposing_mapping(self):
        self.site.accounts = [
            account({'sonar': 'openai/GPT_6.2_SOL', 'terra': 'gpt-6.1-terra',
                     'gpt-6.2-astra': 'gpt-6.2-astra', 'not-listed': 'gpt-6.2-sol'}, platform='openai'),
            account({'sonar': 'other-model', 'third-party': 'gpt-6.2-sol'}, platform='anthropic'),
        ]
        self.site.models = ['sonar', 'terra', 'third-party', 'gpt-6.2-astra', 'platform-fallback']
        for model, effort in (('sonar', 'none'), ('gpt-6.2-astra', 'minimal'), ('terra', 'minimal')):
            with self.subTest(model=model, effort=effort), self.assertRaises(LabError) as raised:
                self.groups.credential(GROUP_ID, ADMIN, HEADERS, model, effort)
            self.assertEqual((raised.exception.status, raised.exception.code), (400, 'effort_not_supported'))
        self.assertFalse(any(call[0] == 'POST' for call in self.site.calls))
        response = self.catalog()
        self.assertEqual(response, {
            'models': ['sonar', 'terra', 'third-party', 'gpt-6.2-astra'],
            'reasoning_effort_blocks': {'sonar': ['minimal', 'none'], 'terra': ['minimal'],
                                        'gpt-6.2-astra': ['minimal', 'none']},
        })
        for model, effort in (('sonar', 'low'), ('sonar', 'max'), ('sonar', 'default'), ('terra', 'none')):
            self.assertEqual(self.groups.credential(GROUP_ID, ADMIN, HEADERS, model, effort)['group_id'], GROUP_ID)
        self.assertEqual([call[0] for call in self.site.calls].count('POST'), 1)

    def test_catalog_excludes_models_with_disabled_effort_suffix(self):
        self.site.models = ['gpt-6.2-astra-none', 'gpt-6.1-terra-none', 'gpt-6.2-sol-low']
        self.site.accounts = [account({model: model for model in self.site.models}, platform='openai')]
        with self.assertRaises(LabError) as raised:
            self.groups.credential(GROUP_ID, ADMIN, HEADERS, 'gpt-6.2-astra-none', 'low')
        self.assertEqual((raised.exception.status, raised.exception.code), (400, 'effort_not_supported'))
        self.assertIn('后缀', str(raised.exception))
        self.assertFalse(any(call[0] == 'POST' for call in self.site.calls))
        self.assertEqual(self.catalog(), {
            'models': ['gpt-6.1-terra-none', 'gpt-6.2-sol-low'],
            'reasoning_effort_blocks': {'gpt-6.1-terra-none': ['minimal'], 'gpt-6.2-sol-low': ['minimal', 'none']},
        })

    def test_mapping_target_must_be_a_nonempty_string_after_trimming(self):
        self.site.accounts = [account({
            "empty-target": "",
            "whitespace-target": "  \t ",
            "non-string-target": 23,
            "valid-target": " upstream-model ",
        })]
        self.site.models = ["empty-target", "whitespace-target", "non-string-target", "valid-target"]

        self.assertEqual(self.catalog()["models"], ["valid-target"])

    def test_expired_or_temporarily_unschedulable_accounts_do_not_add_models(self):
        self.site.accounts = [
            account({"expired-model": "x"}, auto_pause_on_expired=True, expires_at=1),
            account({"rate-limited-model": "x"}, rate_limit_reset_at="2999-01-01T00:00:00Z"),
            account({"overloaded-model": "x"}, overload_until="2999-01-01T00:00:00Z"),
            account({"temporary-model": "x"}, temp_unschedulable_until="2999-01-01T00:00:00Z"),
        ]
        self.site.models = ["expired-model", "rate-limited-model", "overloaded-model", "temporary-model"]
        self.assertEqual(self.catalog()["models"], [])

    def test_invalid_or_undeclared_model_is_rejected_before_key_creation(self):
        for model in ("platform-default", "model with spaces", 42):
            with self.subTest(model=model), self.assertRaises(LabError) as raised:
                self.groups.credential(GROUP_ID, ADMIN, HEADERS, model, "low")
            self.assertEqual(raised.exception.status, 400)
        self.assertFalse(any(call[0] == "POST" for call in self.site.calls))
        self.assertIsNone(self.row())

    def test_model_removed_from_current_gateway_catalog_is_rejected_on_save(self):
        self.site.models = []
        with self.assertRaises(LabError) as raised:
            self.groups.credential(GROUP_ID, ADMIN, HEADERS, "gpt-test", "low")
        self.assertEqual(raised.exception.code, "model_not_available")
        self.assertEqual([call[0] for call in self.site.calls].count("POST"), 1)

    def test_key_is_encrypted_and_reused_for_later_catalog_requests(self):
        first = self.catalog()
        row = self.row()
        self.assertIsNotNone(row)
        encrypted, request_id, state, remote_id = row
        secret = self.groups._fernet.decrypt(encrypted).decode()
        self.assertTrue(secret.startswith("sk-lab-"))
        self.assertNotIn(secret.encode(), encrypted)
        self.assertEqual((state, remote_id), ("ready", 9001))
        self.assertEqual(first["models"], ["gpt-test"])

        self.catalog()
        self.assertEqual([call[0] for call in self.site.calls].count("POST"), 1)
        post = next(call for call in self.site.calls if call[0] == "POST")
        self.assertEqual(post[3]["custom_key"], secret)
        self.assertEqual(post[3]["quota"], 0)
        self.assertEqual(post[5], request_id)

    def test_restart_reuses_encrypted_reservation_and_remote_key(self):
        self.catalog()
        first_row = self.row()
        restarted = SiteGroups(self.db_path, self.key_path, TEST_SITE)
        restarted._request = self.site.request
        self.assertEqual(restarted.catalog(GROUP_ID, ADMIN, HEADERS)["models"], ["gpt-test"])
        self.assertEqual(self.row(), first_row)
        self.assertEqual([call[0] for call in self.site.calls].count("POST"), 1)

    def test_concurrent_catalog_requests_create_only_one_managed_key(self):
        release = threading.Event()
        self.site.block_post = release
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.catalog)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not any(call[0] == "POST" for call in self.site.calls):
                time.sleep(0.005)
            second = executor.submit(self.catalog)
            release.set()
            self.assertEqual(first.result(timeout=5)["models"], ["gpt-test"])
            self.assertEqual(second.result(timeout=5)["models"], ["gpt-test"])
        self.assertEqual([call[0] for call in self.site.calls].count("POST"), 1)

    def test_timeout_after_remote_creation_recovers_existing_key_without_post(self):
        self.site.create_then_error = True
        with self.assertRaises(LabError):
            self.catalog()
        pending = self.row()
        self.assertEqual(pending[2], "pending")
        self.assertEqual(len(self.site.keys), 1)

        self.catalog()
        self.assertEqual([call[0] for call in self.site.calls].count("POST"), 1)
        self.assertEqual(self.row()[2:], ("ready", 9001))

    def test_timeout_before_creation_reuses_same_payload_and_idempotency_key(self):
        self.site.post_error = LabError(502, "site_api_unavailable", "test-only timeout")
        with self.assertRaises(LabError):
            self.catalog()
        first_post = next(call for call in self.site.calls if call[0] == "POST")

        self.catalog()
        posts = [call for call in self.site.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][3], posts[1][3])
        self.assertEqual(posts[0][5], posts[1][5])
        self.assertEqual(first_post[5], self.row()[1])

    def test_disabled_deleted_expired_or_exhausted_key_is_never_replaced(self):
        self.catalog()
        self.site.keys[0]["status"] = "disabled"
        # The normal active-key list omits disabled keys; a ready reservation
        # must fail closed rather than create a replacement.
        self.site.keys.clear()
        with self.assertRaises(LabError) as raised:
            self.catalog()
        self.assertEqual(raised.exception.code, "managed_key_unavailable")
        self.assertEqual([call[0] for call in self.site.calls].count("POST"), 1)

    def test_active_but_expired_or_exhausted_key_is_rejected(self):
        self.catalog()
        self.site.keys[0]["expires_at"] = "2000-01-01T00:00:00Z"
        with self.assertRaises(LabError) as expired:
            self.catalog()
        self.assertEqual(expired.exception.code, "managed_key_unavailable")

        self.site.keys[0]["expires_at"] = None
        self.site.keys[0]["quota"] = 1
        self.site.keys[0]["quota_used"] = 1
        with self.assertRaises(LabError) as exhausted:
            self.catalog()
        self.assertEqual(exhausted.exception.code, "managed_key_unavailable")
        self.assertEqual([call[0] for call in self.site.calls].count("POST"), 1)

    def test_site_api_requests_use_separate_admin_and_key_authorization(self):
        self.catalog()
        management = [call for call in self.site.calls if call[1] != "/v1/models"]
        model = next(call for call in self.site.calls if call[1] == "/v1/models")
        self.assertTrue(all(call[2]["Authorization"] == HEADERS["Authorization"] for call in management))
        self.assertEqual(model[4], self.groups._fernet.decrypt(self.row()[0]).decode())

    def test_real_client_ip_and_user_agent_are_preserved_in_management_headers(self):
        self.catalog()
        for call in self.site.calls:
            headers = call[2]
            self.assertEqual(headers["X-GeekPulse-Client-IP"], HEADERS["X-GeekPulse-Client-IP"])
            self.assertEqual(headers["User-Agent"], HEADERS["User-Agent"])
        with patch("site_groups.http.client.HTTPConnection") as connection_type:
            instance = connection_type.return_value
            instance.getresponse.return_value.status = 200
            instance.getresponse.return_value.read.return_value = json.dumps(
                {"code": 0, "data": []}
            ).encode()
            SiteGroups._request(self.groups, "GET", "/api/v1/groups/available", HEADERS)
            _, kwargs = instance.request.call_args
            self.assertEqual(kwargs["headers"]["X-Real-IP"], HEADERS["X-GeekPulse-Client-IP"])
            self.assertEqual(kwargs["headers"]["X-Forwarded-For"], HEADERS["X-GeekPulse-Client-IP"])
            self.assertEqual(kwargs["headers"]["User-Agent"], HEADERS["User-Agent"])
            self.assertEqual(kwargs["headers"]["Host"], TEST_SITE.authority)
            connection_type.assert_called_once_with('127.0.0.1', 18080, timeout=12)

    def test_oversized_upstream_response_is_sanitized(self):
        with patch("site_groups.http.client.HTTPConnection") as connection_type:
            instance = connection_type.return_value
            instance.getresponse.return_value.status = 200
            instance.getresponse.return_value.read.return_value = b"x" * (2 * 1024 * 1024 + 1)
            with self.assertRaises(LabError) as raised:
                SiteGroups._request(self.groups, "GET", "/api/v1/groups/available", HEADERS)
        self.assertEqual(raised.exception.status, 502)
        self.assertNotIn("x" * 64, str(raised.exception))

    def test_unapproved_destination_or_method_is_rejected(self):
        # `/api/v1/api-keys` appears in a stale handler comment; router.go is
        # authoritative and registers the authenticated `/keys` group.
        for method, path in (("POST", "/v1/chat/completions"), ("GET", "https://example.test"),
                             ("GET", "/api/v1/api-keys?group_id=7&status=active&page=1&page_size=100"),
                             ("POST", "/api/v1/api-keys")):
            with self.subTest(method=method, path=path), self.assertRaises(LabError) as raised:
                SiteGroups._request(self.groups, method, path, HEADERS)
            self.assertEqual(raised.exception.code, "ADAPTER_PATH")


if __name__ == "__main__":
    unittest.main()
