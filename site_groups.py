"""Fixed-origin Sub2API group catalog and encrypted laboratory credentials.

No model generation is performed here. A dedicated key is prepared once per
authenticated administrator and bindable group, through the normal user API.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import uuid

from cryptography.fernet import Fernet, InvalidToken

from runner import blocked_reasoning_efforts, reasoning_configuration_error, reasoning_effort_error
from service import LabError, _public


_MAX_RESPONSE = 2 * 1024 * 1024
_MODEL = re.compile(r"^[^\s\x00-\x1f\x7f]{1,200}$")


class SiteGroups:
    _locks_guard = threading.Lock()
    _locks = {}

    @_public
    def __init__(self, db_path, key_path, site):
        self.site = site
        self._db_path = Path(db_path).resolve()
        if self._db_path == Path(key_path).resolve():
            raise LabError(500, "encryption_unavailable", "独立加密配置不可用。")
        try:
            self._fernet = Fernet(Path(key_path).read_bytes().strip())
        except (OSError, TypeError, ValueError):
            raise LabError(500, "encryption_unavailable", "独立加密配置不可用。") from None
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._db_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self._secure_files()
        with self._database() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS managed_group_keys (
                user_id INTEGER NOT NULL, group_id INTEGER NOT NULL,
                key_ciphertext BLOB NOT NULL, request_id TEXT NOT NULL,
                state TEXT NOT NULL, remote_key_id INTEGER,
                PRIMARY KEY(user_id,group_id)
            )""")

    @contextmanager
    def _database(self):
        db = sqlite3.connect(str(self._db_path), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _secure_files(self):
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self._db_path) + suffix)
            if path.exists():
                os.chmod(path, 0o600)

    def _group_lock(self, user_id, group_id):
        identity = (str(self._db_path), user_id, group_id)
        with self._locks_guard:
            return self._locks.setdefault(identity, threading.RLock())

    @staticmethod
    def _identity(group_id, ctx, headers):
        if (not isinstance(ctx, dict) or type(ctx.get("id")) is not int
                or ctx["id"] <= 0 or ctx.get("role") != "admin"):
            raise LabError(403, "ADMIN_REQUIRED", "此操作仅限管理员。")
        if type(group_id) is not int or group_id <= 0:
            raise LabError(400, "invalid_group", "请选择本站分组。")
        bearer = headers.get("Authorization", "")
        client_ip = headers.get("X-GeekPulse-Client-IP", "")
        user_agent = headers.get("User-Agent", "")
        if (not isinstance(bearer, str) or not bearer.startswith("Bearer ")
                or not 16 <= len(bearer) <= 8192 or "\r" in bearer or "\n" in bearer):
            raise LabError(401, "LOGIN_REQUIRED", "请重新登录后配置分组监测。")
        try:
            ipaddress.ip_address(client_ip)
        except (ValueError, TypeError):
            raise LabError(503, "PROXY_CONFIGURATION", "入口代理配置不完整。") from None
        if not isinstance(user_agent, str) or len(user_agent) > 4096 or "\r" in user_agent or "\n" in user_agent:
            raise LabError(400, "INVALID_REQUEST", "请求头无效。")
        return ctx["id"]

    def _request(self, method, path, headers, body=None, api_key=None, request_id=None):
        # Only internally constructed paths reach this method; there is no URL
        # parameter, redirect handling or caller-supplied destination.
        allowed = path == "/api/v1/groups/available" or path == "/api/v1/keys" or (
            method == "GET" and re.fullmatch(r"/api/v1/keys\?group_id=[1-9][0-9]*&status=active&page=[1-9][0-9]*&page_size=100", path)
        ) or (
            method == "GET" and re.fullmatch(r"/api/v1/admin/accounts\?group=[1-9][0-9]*&status=active&lite=true&page=[1-9][0-9]*&page_size=100", path)
        ) or path == "/v1/models"
        if not allowed or method not in {"GET", "POST"} or method == "POST" and path != "/api/v1/keys":
            raise LabError(500, "ADAPTER_PATH", "站内接口不受支持。")
        authorization = "Bearer " + api_key if api_key is not None else headers.get("Authorization", "")
        outgoing = {
            "Host": self.site.authority, "Authorization": authorization,
            "X-Real-IP": headers.get("X-GeekPulse-Client-IP", ""),
            "X-Forwarded-For": headers.get("X-GeekPulse-Client-IP", ""),
            "X-Forwarded-Proto": "https", "User-Agent": headers.get("User-Agent", ""),
            "Accept": "application/json",
        }
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            outgoing["Content-Type"] = "application/json"
        if request_id:
            outgoing["Idempotency-Key"] = request_id
        connection = http.client.HTTPConnection("127.0.0.1", self.site.sub2api_port, timeout=12)
        try:
            connection.request(method, path, body=payload, headers=outgoing)
            response = connection.getresponse()
            data = response.read(_MAX_RESPONSE + 1)
            if response.status not in (200, 201):
                if response.status == 401 and api_key is None:
                    raise LabError(401, "LOGIN_REQUIRED", "登录已过期，请重新登录。")
                if response.status in (401, 403) and api_key is not None:
                    raise LabError(409, "managed_key_unavailable", "本站监测凭据不可用，请检查原站 Key 状态。")
                raise LabError(502, "site_api_unavailable", "本站接口未完成操作，请稍后重试；未自动重复提交。")
            if len(data) > _MAX_RESPONSE:
                raise ValueError
            decoded = json.loads(data)
            if not isinstance(decoded, dict):
                raise ValueError
            if api_key is not None:
                return decoded
            if decoded.get("code") != 0 or "data" not in decoded:
                raise ValueError
            return decoded["data"]
        except LabError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise LabError(502, "site_api_unavailable", "本站接口响应未确认，请稍后重试；未自动重复提交。") from None
        finally:
            connection.close()

    def _authorize_group(self, group_id, headers):
        groups = self._request("GET", "/api/v1/groups/available", headers)
        if not isinstance(groups, list):
            raise LabError(502, "ADAPTER_INCOMPATIBLE", "本站分组接口格式已变化。")
        if not any(isinstance(group, dict) and type(group.get("id")) is int
                   and group["id"] == group_id and group.get("status", "active") == "active"
                   for group in groups):
            raise LabError(403, "group_not_bindable", "当前账号不能绑定此分组，请先在原站授予分组权限。")

    def _reservation(self, user_id, group_id):
        with self._database() as db:
            row = db.execute("SELECT * FROM managed_group_keys WHERE user_id=? AND group_id=?",
                             (user_id, group_id)).fetchone()
            if row is None:
                key = "sk-lab-" + secrets.token_urlsafe(32)
                db.execute("INSERT OR IGNORE INTO managed_group_keys VALUES(?,?,?,?,'reserved',NULL)",
                           (user_id, group_id, self._fernet.encrypt(key.encode()), str(uuid.uuid4())))
                row = db.execute("SELECT * FROM managed_group_keys WHERE user_id=? AND group_id=?",
                                 (user_id, group_id)).fetchone()
        self._secure_files()
        try:
            secret = self._fernet.decrypt(row["key_ciphertext"]).decode("utf-8")
        except (InvalidToken, UnicodeError):
            raise LabError(500, "key_unavailable", "监测凭据无法解密，请检查服务器配置。") from None
        return dict(row), secret

    def _save_state(self, user_id, group_id, state, remote_id=None):
        with self._database() as db:
            db.execute("UPDATE managed_group_keys SET state=?,remote_key_id=COALESCE(?,remote_key_id) "
                       "WHERE user_id=? AND group_id=?", (state, remote_id, user_id, group_id))
        self._secure_files()

    @staticmethod
    def _check_key(key, secret, user_id, group_id):
        if (not isinstance(key, dict) or type(key.get("id")) is not int or key["id"] <= 0
                or key.get("key") != secret or key.get("group_id") != group_id
                or "user_id" in key and key["user_id"] != user_id):
            raise LabError(502, "ADAPTER_INCOMPATIBLE", "本站凭据接口格式已变化。")
        usable = key.get("status") == "active"
        expires = key.get("expires_at")
        if expires:
            try:
                expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                usable = usable and expiry.tzinfo is not None and expiry > datetime.now(timezone.utc)
            except (AttributeError, ValueError):
                usable = False
        quota, used = key.get("quota", 0), key.get("quota_used", 0)
        if (type(quota) not in (int, float) or type(used) not in (int, float)
                or not math.isfinite(quota) or not math.isfinite(used) or quota < 0 or used < 0):
            usable = False
        elif quota > 0 and used >= quota:
            usable = False
        if not usable:
            raise LabError(409, "managed_key_unavailable", "本站监测凭据已停用、到期或额度不足，请检查原站 Key。")

    def _find_key(self, group_id, headers, secret, remote_id):
        # The account API returns real credentials. Inspect only this group's
        # pages in memory; no response or unrelated key is cached or exposed.
        for page in range(1, 101):
            data = self._request("GET", f"/api/v1/keys?group_id={group_id}&status=active&page={page}&page_size=100", headers)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise LabError(502, "ADAPTER_INCOMPATIBLE", "本站凭据列表格式已变化。")
            for key in data["items"]:
                if isinstance(key, dict) and (key.get("key") == secret or remote_id is not None and key.get("id") == remote_id):
                    return key
            pages = data.get("pages")
            if type(pages) is not int or pages < 0:
                raise LabError(502, "ADAPTER_INCOMPATIBLE", "本站凭据分页格式已变化。")
            if page >= pages:
                return None
        raise LabError(502, "site_key_limit", "本站凭据列表过长，无法安全确认监测凭据。")

    def _credential_locked(self, user_id, group_id, headers):
        reservation, secret = self._reservation(user_id, group_id)
        key = self._find_key(group_id, headers, secret, reservation["remote_key_id"])
        if key is None:
            if reservation["state"] == "ready" or reservation["remote_key_id"] is not None:
                raise LabError(409, "managed_key_unavailable", "本站监测凭据已删除或停用，请检查原站 Key；未自动重建。")
            # Commit the stable operation before the only POST in this call.
            # A timeout leaves it pending; the next explicit call first looks
            # up the same key, then may replay this exact idempotent operation.
            self._save_state(user_id, group_id, "pending")
            key = self._request("POST", "/api/v1/keys", headers, body={
                "name": f"模型实验室 · 分组 {group_id}", "group_id": group_id,
                "custom_key": secret, "quota": 0,
            }, request_id=reservation["request_id"])
        self._check_key(key, secret, user_id, group_id)
        if reservation["remote_key_id"] is not None and key["id"] != reservation["remote_key_id"]:
            raise LabError(409, "managed_key_unavailable", "本站监测凭据标识已变化，请检查原站 Key。")
        self._save_state(user_id, group_id, "ready", key["id"])
        return secret

    @staticmethod
    def _account_usable(account, group_id):
        if (not isinstance(account, dict) or account.get("status") != "active"
                or account.get("schedulable") is not True
                or not isinstance(account.get("group_ids"), list)
                or not any(type(value) is int and value == group_id for value in account["group_ids"])):
            return False
        now = datetime.now(timezone.utc)
        expires = account.get("expires_at")
        if account.get("auto_pause_on_expired") and expires is not None:
            if type(expires) not in (int, float) or not math.isfinite(expires) or expires <= now.timestamp():
                return False
        for name in ("rate_limit_reset_at", "overload_until", "temp_unschedulable_until"):
            value = account.get(name)
            if value is not None:
                try:
                    until = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if until.tzinfo is None or until > now:
                        return False
                except (AttributeError, ValueError):
                    return False
        return True

    def _declared_models(self, group_id, headers):
        models = {}
        for page in range(1, 101):
            data = self._request("GET", f"/api/v1/admin/accounts?group={group_id}&status=active&lite=true&page={page}&page_size=100", headers)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise LabError(502, "ADAPTER_INCOMPATIBLE", "本站账号目录格式已变化。")
            for account in data["items"]:
                if not self._account_usable(account, group_id):
                    continue
                credentials = account.get("credentials")
                mapping = credentials.get("model_mapping") if isinstance(credentials, dict) else None
                if not isinstance(mapping, dict):
                    continue
                for name, mapped in mapping.items():
                    if (isinstance(name, str) and _MODEL.fullmatch(name) and "*" not in name
                            and not reasoning_configuration_error(name)
                            and isinstance(mapped, str) and mapped.strip()):
                        blocked = models.setdefault(name, set(blocked_reasoning_efforts(name)))
                        if account.get("platform") == "openai":
                            blocked.update(blocked_reasoning_efforts(mapped))
                if len(models) > 4096:
                    raise LabError(502, "ADAPTER_INCOMPATIBLE", "本站模型目录超过限制。")
            pages = data.get("pages")
            if type(pages) is not int or pages < 0:
                raise LabError(502, "ADAPTER_INCOMPATIBLE", "本站账号分页格式已变化。")
            if page >= pages:
                return models
        raise LabError(502, "site_account_limit", "本站账号目录过长，无法安全确认分组模型。")

    def _models(self, headers, secret, declared):
        data = self._request("GET", "/v1/models", headers, api_key=secret)
        if not isinstance(data.get("data"), list):
            raise LabError(502, "ADAPTER_INCOMPATIBLE", "本站模型目录格式已变化。")
        models = []
        for item in data["data"]:
            value = item.get("id") if isinstance(item, dict) else None
            if not isinstance(value, str) or not _MODEL.fullmatch(value):
                raise LabError(502, "ADAPTER_INCOMPATIBLE", "本站模型目录含无效模型名称。")
            # The public gateway can fall back to a platform default catalog.
            # Only concrete names explicitly declared by this group's actual
            # schedulable accounts are eligible here. The intersection also
            # retains the gateway's own platform/allowlist/routing decisions.
            if value in declared and value not in models:
                models.append(value)
            if len(models) > 4096:
                raise LabError(502, "ADAPTER_INCOMPATIBLE", "本站模型目录超过限制。")
        return models

    @_public
    def check_binding(self, group_id, ctx, headers):
        self._identity(group_id, ctx, headers)
        self._authorize_group(group_id, headers)

    @_public
    def catalog(self, group_id, ctx, headers):
        user_id = self._identity(group_id, ctx, headers)
        self._authorize_group(group_id, headers)
        declared = self._declared_models(group_id, headers)
        if not declared:
            return {"models": [], "reasoning_effort_blocks": {}, "message": "该分组账号未配置实验室可用的模型，请检查原站模型映射。"}
        with self._group_lock(user_id, group_id):
            secret = self._credential_locked(user_id, group_id, headers)
            models = self._models(headers, secret, declared)
            return {"models": models, "reasoning_effort_blocks": {
                model: sorted(declared[model]) for model in models if declared[model]
            }}

    @_public
    def credential(self, group_id, ctx, headers, model, effort):
        user_id = self._identity(group_id, ctx, headers)
        if not isinstance(model, str) or not _MODEL.fullmatch(model):
            raise LabError(400, "invalid_model", "请选择有效模型。")
        self._authorize_group(group_id, headers)
        if message := reasoning_configuration_error(model):
            raise LabError(400, "effort_not_supported", message)
        declared = self._declared_models(group_id, headers)
        if model not in declared:
            raise LabError(400, "model_not_available", "所选模型未在此分组可调度账号中明确配置，请刷新后选择。")
        if effort in declared[model]:
            raise LabError(400, "effort_not_supported", reasoning_effort_error(declared[model]))
        with self._group_lock(user_id, group_id):
            secret = self._credential_locked(user_id, group_id, headers)
            if model not in self._models(headers, secret, declared):
                raise LabError(400, "model_not_available", "所选模型不在此分组当前目录中，请刷新后选择。")
            return {"api_key": secret, "group_id": group_id}
