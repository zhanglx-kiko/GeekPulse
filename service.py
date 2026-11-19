"""Model laboratory storage, authorization and bounded task scheduling.

Only the HTTP adapter may call configuration methods and start_probe after
verifying the administrator role. Read/cancel methods independently authorize
the trusted context passed by that adapter. No Sub2API database access is used.
"""
from __future__ import annotations

import copy
import functools
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import CancelledError, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken

from runner import (MINIMAL_UNAVAILABLE_MESSAGE, REASONING_EFFORTS, REASONING_REQUIRED_MESSAGE,
                    reasoning_configuration_error, run_probe)


class LabError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _public(method):
    @functools.wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except LabError:
            raise
        except Exception:
            # Never expose SQL, file paths, upstream bodies or credentials.
            raise LabError(500, "internal_error", "实验室暂时无法完成此操作。") from None
    return wrapped


PRESETS = (
    {
        "id": "candy", "name": "糖果形状与口味", "kind": "reasoning",
        "prompt": (
            "袋中有圆形糖果：苹果味7颗、桃味9颗、哈密瓜味8颗；"
            "星形糖果：苹果味7颗、桃味6颗、哈密瓜味4颗。"
            "你能用手摸出形状，但不能辨别口味，因此可以事先选择抽取的圆形与星形数量。"
            "在每种形状内抽取的口味由最坏情况决定。至少总共抽多少颗，"
            "才能保证其中存在两颗形状不同、且一颗苹果味另一颗桃味的糖果？"
            "只输出一个JSON对象，不加空格或换行，字段为answer（最少总数）、round（圆形数量）、"
            "star（星形数量），数值必须是整数。不要输出说明、推理过程、Markdown代码围栏或任何其他文本。"
        ),
        "expected_answer": '{"answer":21,"round":9,"star":12}',
    },
    {
        "id": "svg", "name": "鹈鹕 SVG 测试", "kind": "drawing",
        "prompt": "Generate an SVG of a pelican riding a bicycle",
        "expected_answer": None,
    },
    {
        "id": "custom_drawing", "name": "自定义绘图", "kind": "drawing",
        "prompt": "", "expected_answer": None,
    },
    {
        "id": "custom", "name": "自定义推理题", "kind": "reasoning",
        "prompt": "", "expected_answer": None,
    },
)
_PRESETS = {item["id"]: item for item in PRESETS}
_TERMINAL = frozenset({"passed", "failed", "error", "cancelled", "completed"})
_SUCCESSFUL = frozenset({"passed", "failed", "completed"})
_RETENTION_SECONDS = 30 * 3600
_PRUNE_INTERVAL_SECONDS = 300
_MONITOR_FIELDS = ("runs", "success_rate", "avg_ttft_ms")
_SUMMARY_FIELDS = ("runs", "passed", "scored", "pass_rate", "avg_duration_ms", "total_tokens")
_MODEL_FIELDS = ("runs", "passed", "scored", "pass_rate")
_METRICS = (
    "duration_ms", "ttft_ms", "input_tokens", "output_tokens", "cached_tokens",
    "reasoning_tokens", "total_tokens", "tokens_per_second", "cost_usd",
)
_ARTIFACT_FLAG = ("CASE WHEN json_type(result_json,'$.artifact_html')='text' "
                  "THEN length(json_extract(result_json,'$.artifact_html'))>0 ELSE 0 END AS has_artifact")
_LIVE_PROBE = ("(probe_id IS NULL OR EXISTS "
               "(SELECT 1 FROM probes WHERE probes.id=runs.probe_id AND probes.deleted=0))")
_SUMMARY_SELECT = (
    "SELECT id,user_id,probe_id,request_id,kind,public,group_id,group_name,created_at,finished_at,status,"
    "json_object('model',json_extract(spec_json,'$.model'),"
    "'protocol',json_extract(spec_json,'$.protocol'),'effort',json_extract(spec_json,'$.effort')) AS spec_json,"
    "json_object(" + ",".join(f"'{field}',json_extract(result_json,'$.{field}')"
                             for field in (*_METRICS, "error_message", "text_rate_version")) + ") AS result_json,"
    + _ARTIFACT_FLAG + " FROM runs"
)
_TOKEN_METRICS = frozenset({
    "input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "total_tokens",
})
_CHANNEL_METRICS = ("duration_ms", "ttft_ms", "input_tokens", "output_tokens", "cached_tokens",
                    "reasoning_tokens", "total_tokens")
_PRICE_FIELDS = ("input_price", "cached_price", "output_price")
_DEFAULTS = {
    "name": "新探测", "group_id": None, "model": "", "base_url": "",
    "protocol": "responses", "kind": "reasoning", "effort": "none",
    "preset_id": "candy", "interval_minutes": 60, "publish": False,
    "max_output_tokens": 64, "timeout_seconds": 300,
    "display_multiplier": None,
    "input_price": None, "cached_price": None, "output_price": None,
}
_IDENTITY_FIELDS = ('group_id', 'model', 'kind', 'preset_id', 'prompt', 'expected_answer')
_CONFIG_FIELDS = frozenset(_DEFAULTS) | {"prompt", "expected_answer", "enabled"}
_SAFE_ERRORS = frozenset({
    REASONING_REQUIRED_MESSAGE, MINIMAL_UNAVAILABLE_MESSAGE,
    "任务已取消；上游可能已经产生费用。", "请求超过时间限制；未自动重试。",
    "不支持的接口协议。", "上游地址无效。", "上游地址含不允许的字符。",
    "上游接口路径与协议不一致。", "仅允许不带凭据、查询串和片段的公网 HTTPS 地址。",
    "无法解析上游公网地址。", "上游地址解析包含非公网或不允许的地址。",
    "上游返回了无效或不完整的 JSON。", "上游流不是有效的 UTF-8。",
    "上游响应未完整完成。", "上游响应缺少最终输出。", "上游最终输出结构无效。",
    "上游助手消息未完整完成。", "上游拒绝生成所请求的内容。", "上游最终文本无效。",
    "上游没有返回可用的最终文本。", "上游没有返回 JSON 或 SSE。", "上游响应结构无效。",
    "上游响应缺少唯一的最终答案。", "上游答案被截断、过滤或转入了工具调用。",
    "上游没有返回可评分的助手消息。", "上游流结束前没有确认完整答案。",
    "上游报告了生成错误。", "上游流的答案结构无效。", "上游流返回了非预期的答案。",
    "上游未返回可评分的文本答案。", "上游流的文本顺序无效。", "上游 SSE 事件类型不一致。",
    "上游响应失败或未完整完成。", "上游文本索引无效。", "上游流的最终文本无效。",
    "上游文本终稿与增量不一致。", "上游流已中断，未收到完整结束标记。",
    "上游响应头超过限制。", "上游返回了不支持的压缩响应。", "上游响应超过 2 MiB 限制。",
    "作品含重复属性，无法安全预览。", "作品含不支持的文档声明。", "作品含不支持的文档结构。",
    "作品结构超过预览限制。", "作品标签不完整或未正确闭合。", "作品代码块不完整或含额外内容。",
    "请返回完整的 HTML 或 SVG 作品。", "作品结构无效，无法安全预览。",
    "作品不完整或没有可预览的安全内容。", "任务参数无效。",
    "请求时限必须在 1 到 900 秒之间。", "输出上限必须在 64 到 32768 Token 之间。",
    "不支持的任务类型。", "API Key 为空或格式无效。", "模型名称无效。",
    "提示词为空或超过长度限制。", "不支持的推理强度。", "上游最终文本含无效的 Unicode 字符。",
    "上游 TLS 证书校验失败。", "上游连接中断或 TLS 握手失败；未自动重试。",
    "任务处理失败；未自动重试。",
})


class _SharedPool:
    """One four-worker pool and sixteen admissions across all lab instances."""
    lock = threading.Lock()
    slots = threading.BoundedSemaphore(16)
    executor = None
    references = 0

    @classmethod
    def acquire(cls):
        with cls.lock:
            if cls.executor is None:
                cls.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="model-lab")
            cls.references += 1
            return cls.executor

    @classmethod
    def release(cls):
        with cls.lock:
            cls.references -= 1
            if cls.references == 0:
                executor, cls.executor = cls.executor, None
                executor.shutdown(wait=False)


def _iso(value):
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _number(value, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value > 10**18 or not math.isfinite(value):
        return None
    if integer:
        return int(value) if value == int(value) and value <= 10**15 else None
    return float(value)


def normalize_multiplier(value):
    """Accept finite nonnegative JSON numbers for display only."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise LabError(400, "invalid_input", f"{name} 超出允许范围。")
    return value


def _boolean(value, name):
    if not isinstance(value, bool):
        raise LabError(400, "invalid_input", f"{name} 必须为布尔值。")
    return value


def _text(value, name, maximum, allow_empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
        raise LabError(400, "invalid_input", f"{name} 格式不正确。")
    if "\x00" in value:
        raise LabError(400, "invalid_input", f"{name} 格式不正确。")
    return value.strip()


def _request_id(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise LabError(400, "invalid_request_id", "request_id 必须是有效UUID。") from None


def _url(value):
    value = _text(value, "base_url", 2048)
    if any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise LabError(400, "invalid_url", "接口地址必须为不含凭据或查询参数的公网HTTPS地址。")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
        if (parsed.scheme.lower() != "https" or not hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or "?" in value or "#" in value or (port is not None and not 1 <= port <= 65535)):
            raise ValueError
        hostname = hostname.encode("idna").decode("ascii").lower()
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            labels = hostname.rstrip(".").split(".")
            if len(labels) < 2 or any(
                not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                or not all(c.isascii() and (c.isalnum() or c == "-") for c in label)
                for label in labels
            ):
                raise ValueError
        else:
            if not address.is_global:
                raise ValueError
        authority = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None:
            authority += f":{port}"
        return urlunsplit(("https", authority, parsed.path.rstrip("/"), "", ""))
    except (ValueError, UnicodeError):
        raise LabError(400, "invalid_url", "接口地址必须为不含凭据或查询参数的公网HTTPS地址。") from None


def _secret(value, required=True):
    if value in (None, "") and not required:
        return ""
    value = _text(value, "api_key", 4096)
    if any(not 33 <= ord(c) <= 126 for c in value):
        raise LabError(400, "invalid_key", "API Key格式不正确。")
    return value


def _redact(value, secret):
    return value.replace(secret, "[已隐藏]") if secret and isinstance(value, str) else value


def _context(ctx):
    if not isinstance(ctx, dict):
        raise LabError(401, "unauthenticated", "请先登录。")
    user_id = ctx.get("id")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise LabError(401, "unauthenticated", "请先登录。")
    groups = ctx.get("group_ids", [])
    if not isinstance(groups, (list, tuple, set)):
        raise LabError(401, "invalid_context", "无法确认当前用户权限。")
    return user_id, ctx.get("role") == "admin", {
        item for item in groups if isinstance(item, int) and not isinstance(item, bool) and item > 0
    }


def _stats(runs):
    terminal = [run for run in runs if run["status"] in _TERMINAL]
    successful = sum(run["status"] in _SUCCESSFUL for run in terminal)
    scored = sum(run["status"] in {"passed", "failed"} for run in terminal)
    passed = sum(run["status"] == "passed" for run in terminal)
    attempted = sum(run["status"] != "cancelled" for run in terminal)

    def average(field):
        samples = [run[field] for run in terminal if run[field] is not None]
        return sum(samples) / len(samples) if samples else None

    token_samples = [run["total_tokens"] for run in terminal if run["total_tokens"] is not None]
    return {
        "runs": len(terminal), "successful": successful, "passed": passed, "scored": scored,
        "success_rate": 100 * successful / attempted if attempted else None,
        "pass_rate": 100 * passed / scored if scored else None,
        "avg_ttft_ms": average("ttft_ms"), "avg_duration_ms": average("duration_ms"),
        "total_tokens": sum(token_samples) if token_samples else None,
    }


class ModelLab:
    @_public
    def __init__(self, db_path, key_path, runner_fn=None, *, site_origin, update_gate=None):
        self._update_gate = update_gate
        self._site_origin = site_origin
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._closing = False
        self._closed = False
        self._tasks = {}
        self._active_users = set()
        self._active_probes = set()
        self._db_path = Path(db_path)
        try:
            if self._db_path.resolve() == Path(key_path).resolve():
                raise ValueError
            self._fernet = Fernet(Path(key_path).read_bytes().strip())
        except (OSError, ValueError, TypeError):
            raise LabError(500, "encryption_unavailable", "独立加密密钥不存在或格式无效。") from None
        self._runner = run_probe if runner_fn is None else runner_fn
        self._db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Precreate with restrictive permissions before SQLite can write data.
        fd = os.open(self._db_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self._db_path, 0o600)
        self._db = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.execute("PRAGMA secure_delete=ON")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS probes (
                id TEXT PRIMARY KEY, config_json TEXT NOT NULL, key_ciphertext BLOB,
                deleted INTEGER NOT NULL DEFAULT 0, next_run_at REAL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, probe_id TEXT,
                request_id TEXT NOT NULL, kind TEXT NOT NULL, public INTEGER NOT NULL,
                group_id INTEGER, group_name TEXT NOT NULL, created_at REAL NOT NULL,
                finished_at REAL, status TEXT NOT NULL, spec_json TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS runs_owner_time ON runs(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS runs_probe_time ON runs(probe_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS requests (
                user_id INTEGER NOT NULL, request_id TEXT NOT NULL, run_id TEXT NOT NULL,
                status TEXT NOT NULL, PRIMARY KEY(user_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS channel_metrics (
                run_id TEXT PRIMARY KEY, probe_id TEXT NOT NULL, created_at REAL NOT NULL,
                public INTEGER NOT NULL, group_id INTEGER, status TEXT NOT NULL, duration_ms REAL, ttft_ms REAL,
                input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
                reasoning_tokens INTEGER, total_tokens INTEGER
            );
            CREATE INDEX IF NOT EXISTS channel_metrics_probe_time
                ON channel_metrics(probe_id, created_at DESC);
        """)
        # Retention removes old samples, so a legacy row cannot prove it was never used.
        if 'identity_locked' not in {row['name'] for row in self._db.execute('PRAGMA table_info(probes)')}:
            self._db.execute('ALTER TABLE probes ADD COLUMN identity_locked INTEGER NOT NULL DEFAULT 1')
        now = time.time()
        interrupted = json.dumps({"error_message": "服务重启，任务已中断，未自动重试。"}, ensure_ascii=False)
        with self._db:
            # Upgrade old soft deletion semantics before handling live leftovers.
            # Removed probes must not leave answers/artworks in any history.
            self._remove_orphaned_history_locked()
            self._db.execute("UPDATE runs SET status='error',finished_at=?,result_json=? "
                             "WHERE status IN ('queued','running')", (now, interrupted))
            self._db.execute("UPDATE requests SET status='error' WHERE status IN ('queued','running')")
            # Only retained historical samples can be recovered on first upgrade.
            # This compact ledger thereafter keeps all 30h channel samples,
            # independent of the 60 full answer records kept per probe.
            self._db.execute(
                "INSERT OR IGNORE INTO channel_metrics(run_id,probe_id,created_at,public,group_id,status,"
                + ",".join(_CHANNEL_METRICS) + ") SELECT id,probe_id,created_at,public,group_id,status,"
                + ",".join(f"json_extract(result_json,'$.{field}')" for field in _CHANNEL_METRICS)
                + " FROM runs WHERE probe_id IS NOT NULL AND kind='reasoning' "
                "AND status IN ('passed','failed','error','cancelled','completed')")
            self._prune_locked(now)
        self._secure_files()
        self._executor = _SharedPool.acquire()
        self._scheduler = threading.Thread(target=self._schedule_loop, name="model-lab-scheduler", daemon=True)
        self._scheduler.start()

    def _secure_files(self):
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self._db_path) + suffix)
            if path.exists():
                os.chmod(path, 0o600)

    def _ready(self):
        if self._closing or self._closed:
            raise LabError(503, "service_stopping", "实验室正在停止，请稍后重试。")

    @_public
    def bootstrap_presets(self):
        return copy.deepcopy(list(PRESETS))

    def _config(self, payload, previous=None, groups=None):
        if not isinstance(payload, dict):
            raise LabError(400, "invalid_input", "请求必须是JSON对象。")
        config = dict(_DEFAULTS)
        if previous:
            config.update({key: value for key, value in previous.items() if key in _CONFIG_FIELDS})
        config.update({key: value for key, value in payload.items() if key in _CONFIG_FIELDS})
        config["enabled"] = _boolean(config.get("enabled", False), "enabled")
        config["publish"] = _boolean(config["publish"], "publish")
        config["base_url"] = _url(config["base_url"])
        config["model"] = _text(config["model"], "model", 200)
        if not isinstance(config["protocol"], str) or config["protocol"] not in {"responses", "chat_completions"}:
            raise LabError(400, "invalid_protocol", "不支持该接口协议。")
        if not isinstance(config["kind"], str) or config["kind"] not in {"reasoning", "drawing"}:
            raise LabError(400, "invalid_kind", "不支持该任务类型。")
        preset = _PRESETS.get(config["preset_id"]) if isinstance(config["preset_id"], str) else None
        if preset is None or preset["kind"] != config["kind"]:
            raise LabError(400, "invalid_preset", "预设与任务类型不匹配。")
        if not isinstance(config["effort"], str) or config["effort"] not in REASONING_EFFORTS:
            raise LabError(400, "invalid_effort", "推理强度无效。")
        if not previous or previous["preset_id"] != config["preset_id"]:
            for name in ("prompt", "expected_answer"):
                if name not in payload:
                    config[name] = preset[name]
        config["prompt"] = _text(config.get("prompt") or preset["prompt"], "prompt", 32000)
        expected = config.get("expected_answer", preset["expected_answer"])
        if expected is not None:
            expected = _text(expected, "expected_answer", 4000, allow_empty=True) or None
        config["expected_answer"] = expected if config["kind"] == "reasoning" else None
        if config["preset_id"] == "candy" and any(config[name] != preset[name] for name in ("prompt", "expected_answer")):
            raise LabError(400, "preset_locked", f"{preset['name']}使用固定题目和答案；修改题目请先选择自定义推理题。")
        for name, minimum, maximum in (
            ("interval_minutes", 5, 10080), ("max_output_tokens", 64, 32768), ("timeout_seconds", 30, 900)
        ):
            config[name] = _integer(config[name], name, minimum, maximum)
        for name in _PRICE_FIELDS:
            value = config[name]
            if value is not None and (_number(value) is None or value > 1_000_000):
                raise LabError(400, "invalid_price", "每百万Token单价格式无效。")
            config[name] = float(value) if value is not None else None
        multiplier = config["display_multiplier"]
        if multiplier is not None:
            multiplier = normalize_multiplier(multiplier)
            if multiplier is None or multiplier > 1_000_000:
                raise LabError(400, "invalid_multiplier", "展示倍率必须是0到1000000之间的数值，留空使用分组倍率。")
        config["display_multiplier"] = multiplier
        group_id = config["group_id"]
        if group_id is None:
            config["group_name"] = "自定义组"
        else:
            _integer(group_id, "group_id", 1, 2**63 - 1)
            group = next((item for item in (groups or []) if isinstance(item, dict)
                          and item.get("id") == group_id and not isinstance(item.get("id"), bool)), None)
            if group is None:
                raise LabError(400, "invalid_group", "所选分组不存在或不可用。")
            config["group_name"] = _text(group.get("name"), "group_name", 200)
        if not previous and group_id is not None and not payload.get('name'):
            config['name'] = config['group_name']
        config['name'] = _text(config['name'], 'name', 200)
        return config

    def _probe_row(self, probe_id):
        row = self._db.execute("SELECT * FROM probes WHERE id=? AND deleted=0", (probe_id,)).fetchone()
        if row is None:
            raise LabError(404, "probe_not_found", "探测配置不存在。")
        return row

    @staticmethod
    def _probe(row):
        result = json.loads(row["config_json"])
        result.setdefault("display_multiplier", None)
        result.update(id=row["id"], identity_locked=bool(row['identity_locked']), has_key=bool(row["key_ciphertext"]), next_run_at=_iso(row["next_run_at"]))
        return result

    @_public
    def list_probes(self):
        with self._lock:
            self._ready()
            return {"probes": [self._probe(row) for row in self._db.execute(
                "SELECT * FROM probes WHERE deleted=0 ORDER BY created_at,id") ]}

    @staticmethod
    def _check_identity(row, previous, payload):
        if row is None or not row['identity_locked']:
            return
        for field in _IDENTITY_FIELDS:
            if field not in payload:
                continue
            value = payload[field]
            if isinstance(value, str):
                value = value.strip()
            if field == 'expected_answer' and value == '':
                value = None
            if value != previous.get(field):
                raise LabError(409, 'probe_identity_locked', '此监控的分组、模型及测试内容已锁定。更换模型或题目请新建监控。')

    @_public
    def prepare_site_probe(self, payload, probe_id=None, groups=None):
        """Validate user configuration before provisioning a group credential."""
        if not isinstance(payload, dict):
            raise LabError(400, "invalid_input", "请求必须是JSON对象。")
        if any(field in payload for field in ("api_key", "base_url")):
            raise LabError(400, "site_probe_only", "管理探测使用本站分组，无需填写接口地址或密钥。")
        with self._lock:
            self._ready()
            previous = self._probe_row(probe_id) if probe_id else None
            old_config = json.loads(previous["config_json"]) if previous else None
            self._check_identity(previous, old_config, payload)
            config = self._config({**payload, "base_url": self._site_origin}, old_config, groups)
            if config["group_id"] is None:
                raise LabError(400, "invalid_group", "请选择本站分组。")
            # A saved combination must remain editable and pausable even if the
            # new rule marks it invalid. Changing that combination or enabling
            # it requires validation against the current site mapping.
            check_effort = old_config is None or (config['enabled'] and not old_config['enabled']) or any(
                config[key] != old_config.get(key) for key in ("group_id", "model", "effort")
            )
            return config, check_effort

    @_public
    def save_site_probe(self, payload, probe_id, groups, credential_for, check_binding):
        with self._lock:
            config, check_effort = self.prepare_site_probe(payload, probe_id, groups)
            previous = self._probe_row(probe_id) if probe_id else None
            reuse = bool(previous and not check_effort and previous['key_ciphertext'])
            if reuse:
                # Preserve the matching URL/key pair even for older deployments.
                config['base_url'] = json.loads(previous['config_json'])['base_url']
        # Both paths must check current binding rights, outside the scheduler lock.
        if reuse:
            check_binding(config['group_id'])
            credential = {'group_id': config['group_id']}
        else:
            credential = credential_for(config, check_effort)
        try:
            if credential.get('group_id') != config['group_id']:
                raise LabError(503, 'SITE_GROUP_MISMATCH', '分组凭据不匹配，请重新选择分组。')
            with self._lock:
                if previous and self._probe_row(probe_id)['updated_at'] != previous['updated_at']:
                    raise LabError(409, 'probe_changed', '监控配置已被修改，请刷新后重试。')
                if not reuse:
                    config['api_key'] = credential['api_key']
                return self.save_probe(config, probe_id, groups)
        finally:
            credential.pop('api_key', None)

    @_public
    def save_probe(self, payload, probe_id=None, groups=None):
        with self._lock:
            self._ready()
            previous = self._probe_row(probe_id) if probe_id else None
            old_config = json.loads(previous["config_json"]) if previous else None
            self._check_identity(previous, old_config, payload)
            config = self._config(payload, old_config, groups)
            key = _secret(payload.get("api_key"), required=False)
            cipher = self._fernet.encrypt(key.encode("utf-8")) if key else (
                previous["key_ciphertext"] if previous else None)
            if config["enabled"] and not cipher:
                raise LabError(400, "missing_key", "请先填写API Key再启用计划。")
            if cipher:
                try:
                    retained_key = self._fernet.decrypt(cipher).decode("utf-8")
                except (InvalidToken, UnicodeError):
                    raise LabError(500, "key_unavailable", "已保存的API Key无法解密，请重新填写。") from None
                for name, value in config.items():
                    if isinstance(value, str) and retained_key in value:
                        raise LabError(400, "key_in_configuration", "API Key只能填入独立密钥字段。")
            now = time.time()
            # Every configuration edit starts a fresh interval; no immediate billed request.
            next_at = now + config["interval_minutes"] * 60 if config["enabled"] else None
            probe_id = probe_id or str(uuid.uuid4())
            locked = bool((previous and previous['identity_locked']) or config['publish'] or config['enabled'])
            with self._db:
                self._db.execute(
                    "INSERT INTO probes(id,config_json,key_ciphertext,next_run_at,created_at,updated_at,identity_locked) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json,"
                    "key_ciphertext=excluded.key_ciphertext,next_run_at=excluded.next_run_at,updated_at=excluded.updated_at,"
                    "identity_locked=excluded.identity_locked",
                    (probe_id, json.dumps(config, ensure_ascii=False), cipher, next_at,
                     previous["created_at"] if previous else now, now, int(locked)),
                )
            self._secure_files()
            return self._probe(self._probe_row(probe_id))

    @_public
    def delete_probe(self, probe_id):
        with self._lock:
            self._ready()
            row = self._probe_row(probe_id)
            config = json.loads(row["config_json"])
            config["enabled"] = False
            tasks = [task for task in self._tasks.values() if task["probe_id"] == probe_id]
            for task in tasks:
                task["event"].set()
            with self._db:
                self._db.execute("UPDATE probes SET deleted=1,key_ciphertext=NULL,next_run_at=NULL,"
                                 "config_json=?,updated_at=? WHERE id=?",
                                 (json.dumps(config, ensure_ascii=False), time.time(), probe_id))
                self._db.execute("UPDATE requests SET status='cancelled' WHERE run_id IN "
                                 "(SELECT id FROM runs WHERE probe_id=? AND status IN ('queued','running'))",
                                 (probe_id,))
                self._db.execute("DELETE FROM runs WHERE probe_id=?", (probe_id,))
                self._db.execute("DELETE FROM channel_metrics WHERE probe_id=?", (probe_id,))
            # A queued future may invoke its done callback synchronously. The
            # rows must already be gone before that callback can finish.
            for task in tasks:
                if task["future"]:
                    task["future"].cancel()
            self._secure_files()
            return {"ok": True}

    def _duplicate(self, user_id, request_id):
        row = self._db.execute("SELECT run_id,status FROM requests WHERE user_id=? AND request_id=?",
                               (user_id, request_id)).fetchone()
        return {"id": row["run_id"], "status": row["status"]} if row else None

    @_public
    def get_request(self, request_id, ctx):
        identity = _context(ctx)
        request_id = _request_id(request_id)
        with self._lock:
            self._ready()
            run = self._duplicate(identity[0], request_id)
            if not run:
                raise LabError(404, "request_not_found", "尚未查到此请求；再次提交时会沿用同一请求编号。")
            return run

    @_public
    def start_self(self, user_id, payload):
        _integer(user_id, "user_id", 1, 2**63 - 1)
        if not isinstance(payload, dict):
            raise LabError(400, "invalid_input", "请求必须是JSON对象。")
        request_id = _request_id(payload.get("request_id"))
        with self._lock:
            self._ready()
            duplicate = self._duplicate(user_id, request_id)
            if duplicate:
                return duplicate
            # Self runs cannot impersonate a protected group or set server prices.
            clean = {key: value for key, value in payload.items() if key in _CONFIG_FIELDS}
            clean.update(group_id=None, enabled=False, input_price=None, cached_price=None, output_price=None)
            spec = self._config(clean)
            if message := reasoning_configuration_error(spec["model"], spec["effort"]):
                raise LabError(400, "effort_not_supported", message)
            spec.pop("display_multiplier", None)
            spec["api_key"] = _secret(payload.get("api_key"))
            spec["group_name"] = "自助测试"
            return self._enqueue(user_id, None, request_id, spec)

    def _probe_spec(self, row):
        if not row["key_ciphertext"]:
            raise LabError(400, "missing_key", "此探测尚未配置API Key。")
        try:
            key = self._fernet.decrypt(row["key_ciphertext"]).decode("utf-8")
        except (InvalidToken, UnicodeError):
            raise LabError(500, "key_unavailable", "已保存的API Key无法解密，请重新填写。") from None
        spec = json.loads(row["config_json"])
        spec.pop("display_multiplier", None)
        spec["api_key"] = key
        return spec

    @_public
    def start_probe(self, user_id, probe_id, request_id):
        _integer(user_id, "user_id", 1, 2**63 - 1)
        request_id = _request_id(request_id)
        with self._lock:
            self._ready()
            duplicate = self._duplicate(user_id, request_id)
            if duplicate:
                return duplicate
            return self._enqueue(user_id, probe_id, request_id, self._probe_spec(self._probe_row(probe_id)))

    def _enqueue(self, user_id, probe_id, request_id, spec, schedule_next_at=None):
        if self._update_gate is not None and self._update_gate.exists():
            raise LabError(503, 'update_in_progress', '监控正在升级，请稍后再提交。')
        if probe_id is None and user_id and user_id in self._active_users:
            raise LabError(429, "user_busy", "当前已有任务，请等待结束或取消。")
        if probe_id and probe_id in self._active_probes:
            raise LabError(409, "probe_busy", "此探测正在执行，不能重叠请求。")
        if not _SharedPool.slots.acquire(blocking=False):
            raise LabError(429, "queue_full", "任务队列已满，请稍后重试。")
        run_id = str(uuid.uuid4())
        now = time.time()
        # Never serialize self-run URLs/keys. Only fields required for historical display.
        snapshot = {key: _redact(spec.get(key), spec["api_key"]) for key in (
            "name", "model", "kind", "protocol", "effort", "preset_id", "prompt", "expected_answer",
            "input_price", "cached_price", "output_price",
        )}
        task = {"event": threading.Event(), "spec": spec, "user_id": user_id,
                "probe_id": probe_id, "future": None}
        try:
            with self._db:
                self._db.execute("INSERT INTO runs(id,user_id,probe_id,request_id,kind,public,group_id,"
                                 "group_name,created_at,status,spec_json) VALUES(?,?,?,?,?,?,?,?,?,'queued',?)",
                                 (run_id, user_id, probe_id, request_id, spec["kind"], int(spec["publish"]),
                                  spec["group_id"], spec["group_name"], now,
                                  json.dumps(snapshot, ensure_ascii=False)))
                self._db.execute("INSERT INTO requests VALUES(?,?,?,'queued')", (user_id, request_id, run_id))
                if probe_id is not None:
                    self._db.execute('UPDATE probes SET identity_locked=1 WHERE id=?', (probe_id,))
                if schedule_next_at is not None:
                    # Commit the next deadline with the run, before any billed request
                    # can begin. A crash cannot leave this same schedule slot due.
                    self._db.execute("UPDATE probes SET next_run_at=? WHERE id=? AND deleted=0",
                                     (schedule_next_at, probe_id))
            self._tasks[run_id] = task
            if probe_id is None and user_id:
                self._active_users.add(user_id)
            if probe_id:
                self._active_probes.add(probe_id)
            future = self._executor.submit(self._execute, run_id, task)
            task["future"] = future
            future.add_done_callback(lambda done, rid=run_id: self._task_done(rid, done))
            return {"id": run_id, "status": "queued"}
        except Exception:
            self._tasks.pop(run_id, None)
            if probe_id is None:
                self._active_users.discard(user_id)
            self._active_probes.discard(probe_id)
            _SharedPool.slots.release()
            with self._db:
                self._db.execute("UPDATE runs SET status='error',finished_at=? WHERE id=?", (time.time(), run_id))
                self._db.execute("UPDATE requests SET status='error' WHERE run_id=?", (run_id,))
            raise

    def _execute(self, run_id, task):
        with self._lock:
            if task["event"].is_set():
                return {"status": "cancelled", "error_message": "任务已取消。"}
            with self._db:
                self._db.execute("UPDATE runs SET status='running' WHERE id=? AND status='queued'", (run_id,))
                self._db.execute("UPDATE requests SET status='running' WHERE run_id=?", (run_id,))
        try:
            result = self._runner(task["spec"], task["event"])
            return self._normalize_result(result, task["spec"], task["event"].is_set())
        except Exception:
            return {"status": "cancelled" if task["event"].is_set() else "error",
                    "error_message": "任务已取消。" if task["event"].is_set() else "上游请求未完成，请检查配置后重试。"}

    @staticmethod
    def _normalize_result(result, spec, cancelled):
        if not isinstance(result, dict):
            return {"status": "error", "error_message": "上游返回格式无效。"}
        status = "cancelled" if cancelled else result.get("status")
        if status not in _TERMINAL:
            status = "error"
        cleaned = {name: _number(result.get(name), name in _TOKEN_METRICS) for name in _METRICS}
        cleaned["text_rate_version"] = 1 if result.get("text_rate_version") == 1 else None
        # Prices are configuration, not untrusted upstream cost claims.
        cleaned["cost_usd"] = None
        if all(spec.get(name) is not None for name in _PRICE_FIELDS):
            input_tokens, cached_tokens, output_tokens = (cleaned[name] for name in
                                                         ("input_tokens", "cached_tokens", "output_tokens"))
            if all(value is not None for value in (input_tokens, cached_tokens, output_tokens)) and cached_tokens <= input_tokens:
                cleaned["cost_usd"] = ((input_tokens - cached_tokens) * spec["input_price"]
                                       + cached_tokens * spec["cached_price"]
                                       + output_tokens * spec["output_price"]) / 1_000_000
        cleaned.update(status=status, error_message=None)
        for name in ("output_text", "artifact_html"):
            text = result.get(name)
            cleaned[name] = _redact(text[:1_000_000], spec.get("api_key")) if isinstance(text, str) else ""
        if status == "error":
            message = result.get("error_message")
            if isinstance(message, str) and (message in _SAFE_ERRORS or re.fullmatch(
                    r"上游接口返回 HTTP [1-5][0-9]{2}；未自动重试。", message)):
                cleaned["error_message"] = _redact(message, spec.get("api_key"))
            else:
                cleaned["error_message"] = "上游请求未完成，请检查配置或稍后重试。"
        elif status == "cancelled":
            cleaned["error_message"] = "任务已取消；上游可能已产生费用。"
        return cleaned

    def _task_done(self, run_id, future):
        try:
            result = future.result()
        except CancelledError:
            result = {"status": "cancelled", "error_message": "任务已取消。"}
        except Exception:
            result = {"status": "error", "error_message": "任务未完成，请稍后重试。"}
        with self._lock:
            task = self._tasks.get(run_id)
            if task is None:
                return
            try:
                now = time.time()
                row = self._db.execute("SELECT status,finished_at FROM runs WHERE id=?", (run_id,)).fetchone()
                if row is None:
                    # Probe deletion erased its source row. A late completion
                    # must only settle the receipt, never recreate its output.
                    with self._db:
                        self._db.execute("UPDATE requests SET status='cancelled' WHERE run_id=? "
                                         "AND status IN ('queued','running')", (run_id,))
                    return
                if task["event"].is_set() or row["status"] == "cancelled":
                    result["status"] = "cancelled"
                    result["error_message"] = "任务已取消；上游可能已产生费用。"
                with self._db:
                    self._db.execute("UPDATE runs SET status=?,finished_at=?,result_json=? WHERE id=?",
                                     (result["status"], row["finished_at"] or now,
                                      json.dumps(result, ensure_ascii=False, allow_nan=False), run_id))
                    self._db.execute("UPDATE requests SET status=? WHERE run_id=?", (result["status"], run_id))
                    if task["probe_id"] is not None and task["spec"]["kind"] == "reasoning":
                        self._db.execute(
                            "INSERT INTO channel_metrics(run_id,probe_id,created_at,public,group_id,status,"
                            + ",".join(_CHANNEL_METRICS) + ") SELECT id,probe_id,created_at,public,group_id,?,"
                            + ",".join("?" for _ in _CHANNEL_METRICS) + " FROM runs WHERE id=? "
                            "ON CONFLICT(run_id) DO NOTHING",
                            (result["status"], *(result.get(field) for field in _CHANNEL_METRICS), run_id))
                    self._prune_locked(now)
                self._secure_files()
            finally:
                task["spec"].pop("api_key", None)
                self._tasks.pop(run_id, None)
                if task["probe_id"] is None:
                    self._active_users.discard(task["user_id"])
                self._active_probes.discard(task["probe_id"])
                _SharedPool.slots.release()
                self._finish_close_locked()

    def _remove_orphaned_history_locked(self):
        self._db.execute("UPDATE requests SET status='cancelled' WHERE status IN ('queued','running') "
                         "AND run_id IN (SELECT id FROM runs WHERE NOT " + _LIVE_PROBE + ")")
        self._db.execute("DELETE FROM runs WHERE NOT " + _LIVE_PROBE)
        self._db.execute("DELETE FROM channel_metrics WHERE NOT EXISTS "
                         "(SELECT 1 FROM probes WHERE probes.id=channel_metrics.probe_id AND probes.deleted=0)")

    def _prune_locked(self, now):
        self._remove_orphaned_history_locked()
        self._db.execute("DELETE FROM channel_metrics WHERE created_at < ?", (now - _RETENTION_SECONDS,))
        self._db.execute("DELETE FROM runs WHERE status IN ('passed','failed','error','cancelled','completed') "
                         "AND created_at < ?", (now - _RETENTION_SECONDS,))
        self._db.execute("""DELETE FROM runs WHERE id IN (
            SELECT id FROM (
                SELECT id,ROW_NUMBER() OVER (PARTITION BY probe_id ORDER BY created_at DESC,id DESC) AS n
                FROM runs WHERE probe_id IS NOT NULL AND status NOT IN ('queued','running')
            ) WHERE n > 60)""")
        self._db.execute("""DELETE FROM runs WHERE id IN (
            SELECT id FROM (
                SELECT id,ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at DESC,id DESC) AS n
                FROM runs WHERE probe_id IS NULL AND status NOT IN ('queued','running')
            ) WHERE n > 60)""")
        # Normal SQLite deletion releases pages for reuse. Do not VACUUM or
        # force checkpoints on this live connection; WAL keeps its normal lifecycle.
        self._last_prune_monotonic = time.monotonic()

    def _run_row(self, run_id):
        row = self._db.execute("SELECT *," + _ARTIFACT_FLAG + " FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise LabError(404, "run_not_found", "任务不存在或无权查看。")
        return row

    def _access_row(self, run_id):
        row = self._db.execute("SELECT id,user_id,probe_id,public,group_id,status FROM runs WHERE id=? "
                               "AND (created_at>=? OR status IN ('queued','running')) AND " + _LIVE_PROBE,
                               (run_id, time.time() - _RETENTION_SECONDS)).fetchone()
        if row is None:
            raise LabError(404, "run_not_found", "任务不存在或无权查看。")
        return row

    @staticmethod
    def _can_read(row, identity):
        user_id, admin, groups = identity
        return (admin or row["user_id"] == user_id or bool(row["public"]) and (
            row["probe_id"] is None or row["group_id"] is None or row["group_id"] in groups))

    @staticmethod
    def _summary(row):
        spec = json.loads(row["spec_json"])
        result = json.loads(row["result_json"])
        summary = {
            "id": row["id"], "probe_id": row["probe_id"], "group_name": row["group_name"],
            "model": spec.get("model", ""), "kind": row["kind"], "protocol": spec.get("protocol"),
            "effort": spec.get("effort"), "status": row["status"], "created_at": _iso(row["created_at"]),
            "finished_at": _iso(row["finished_at"]), "public": bool(row["public"]),
            "error_message": result.get("error_message"), "has_artifact": bool(row["has_artifact"]),
            "can_delete": False,
        }
        summary.update({name: result.get(name) for name in _METRICS})
        if result.get("text_rate_version") != 1:
            summary["tokens_per_second"] = None
        return summary

    def _authorized_summary(self, row, identity):
        summary = self._summary(row)
        summary["can_delete"] = (row["probe_id"] is None and row["user_id"] == identity[0]
                                 and row["status"] in _TERMINAL and row["id"] not in self._tasks)
        return summary

    @_public
    def get_run(self, run_id, ctx):
        identity = _context(ctx)
        with self._lock:
            self._ready()
            if not self._can_read(self._access_row(run_id), identity):
                raise LabError(404, "run_not_found", "任务不存在或无权查看。")
            row = self._run_row(run_id)
            detail = self._authorized_summary(row, identity)
            result, spec = json.loads(row["result_json"]), json.loads(row["spec_json"])
            detail.update(output_text=result.get("output_text", ""), artifact_html=result.get("artifact_html", ""),
                          prompt=spec.get("prompt", ""), expected_answer=spec.get("expected_answer"))
            return detail

    @_public
    def delete_run(self, run_id, ctx):
        user_id, _, _ = _context(ctx)
        with self._lock:
            self._ready()
            row = self._db.execute("SELECT id,user_id,probe_id,status FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                # A repeated delete is harmless, but only the request owner can
                # recognize its already-erased receipt.
                owned = self._db.execute("SELECT 1 FROM requests WHERE run_id=? AND user_id=?",
                                         (run_id, user_id)).fetchone()
                if owned:
                    return {"ok": True}
                raise LabError(404, "run_not_found", "任务不存在或无权操作。")
            if row["probe_id"] is not None or row["user_id"] != user_id:
                raise LabError(404, "run_not_found", "任务不存在或无权操作。")
            if row["status"] not in _TERMINAL or run_id in self._tasks:
                raise LabError(409, "run_active", "任务尚未结束，请结束后再删除。")
            with self._db:
                self._db.execute("DELETE FROM runs WHERE id=?", (run_id,))
            self._secure_files()
            return {"ok": True}

    @_public
    def cancel_run(self, run_id, ctx):
        user_id, admin, _ = _context(ctx)
        with self._lock:
            self._ready()
            row = self._access_row(run_id)
            if not admin and row["user_id"] != user_id:
                raise LabError(404, "run_not_found", "任务不存在或无权操作。")
            if row["status"] in _TERMINAL:
                return {"id": run_id, "status": row["status"]}
            task = self._tasks.get(run_id)
            if task:
                task["event"].set()
            with self._db:
                self._db.execute("UPDATE runs SET status='cancelled',finished_at=?,result_json=? WHERE id=?",
                                 (time.time(), json.dumps({"error_message": "任务已取消；上游可能已产生费用。"},
                                                          ensure_ascii=False), run_id))
                self._db.execute("UPDATE requests SET status='cancelled' WHERE run_id=?", (run_id,))
            if task and task["future"]:
                task["future"].cancel()
            return {"id": run_id, "status": "cancelled"}

    @_public
    def list_runs(self, ctx, scope="mine", kind=None):
        identity = _context(ctx)
        if not isinstance(scope, str) or scope not in {"mine", "public"} or (
            kind is not None and (not isinstance(kind, str) or kind not in {"reasoning", "drawing"})
        ):
            raise LabError(400, "invalid_filter", "任务筛选条件无效。")
        with self._lock:
            self._ready()
            clause, params = ("user_id=? AND probe_id IS NULL", [identity[0]]) if scope == "mine" else ("public=1", [])
            if kind:
                clause += " AND kind=?"
                params.append(kind)
            clause += " AND (created_at>=? OR status IN ('queued','running'))"
            params.append(time.time() - _RETENTION_SECONDS)
            clause += " AND " + _LIVE_PROBE
            if scope == "public" and not identity[1]:
                allowed = sorted(identity[2])
                marks = ",".join("?" for _ in allowed)
                clause += " AND (probe_id IS NULL OR group_id IS NULL"
                if allowed:
                    clause += f" OR group_id IN ({marks})"
                    params.extend(allowed)
                clause += ")"
            rows = self._db.execute(f"{_SUMMARY_SELECT} WHERE {clause} ORDER BY created_at DESC,id DESC LIMIT 60", params)
            return {"runs": [self._authorized_summary(row, identity) for row in rows]}

    @_public
    def dashboard(self, ctx, hours=24):
        """Overview groups come only from visible, configured probes.

        Paused probes still count as configured. Available groups from the
        authentication/bootstrap catalog are deliberately not used here: they
        remain selectable when creating a probe, but do not create empty cards.
        Deleted probes and their retained history cannot resurrect a group.
        """
        identity = _context(ctx)
        if isinstance(hours, bool) or hours not in (1, 24):
            raise LabError(400, "invalid_range", "统计范围仅支持1或24小时。")
        supplied_rates = ctx.get("group_rates", {})
        group_rates = {group_id: normalize_multiplier(value) for group_id, value in supplied_rates.items()
                       if type(group_id) is int and group_id in identity[2]} if isinstance(supplied_rates, dict) else {}
        now = time.time()
        with self._lock:
            self._ready()
            groups, aggregate, count, enabled = {}, [], 0, 0
            for row in self._db.execute("SELECT id,config_json,next_run_at FROM probes WHERE deleted=0 ORDER BY created_at,id"):
                config = json.loads(row["config_json"])
                if not identity[1] and (not config["publish"] or config["group_id"] is not None
                                         and config["group_id"] not in identity[2]):
                    continue
                count += 1
                enabled += int(config["enabled"])
                records = list(self._db.execute(_SUMMARY_SELECT + " WHERE probe_id=? "
                                               "AND (created_at>=? OR status IN ('queued','running')) ORDER BY created_at,id",
                                               (row["id"], now - _RETENTION_SECONDS)))
                records = [item for item in records if self._can_read(item, identity)]
                # A changed probe kind must not make old drawing runs answer
                # samples, or put answer usage into the independent gallery.
                records = [item for item in records if item["kind"] == config["kind"]]
                records = records[-60:]
                history = [self._summary(item) for item in records if item["status"] in _SUCCESSFUL
                           and (config["kind"] != "drawing" or item["has_artifact"])]
                group = groups.setdefault(config["group_id"], {
                    "id": config["group_id"], "name": config["group_name"], "models": [],
                    "drawing_models": [], "_runs": [], "_day_runs": [], "_multipliers": [],
                })
                model = {name: config[name] for name in (
                    "name", "model", "kind", "protocol", "effort", "enabled", "interval_minutes",
                )}
                model.update(probe_id=row["id"], next_run_at=_iso(row["next_run_at"]))
                override = normalize_multiplier(config.get("display_multiplier"))
                model["display_multiplier"] = override if override is not None else group_rates.get(config["group_id"])
                group["_multipliers"].append(model["display_multiplier"])
                if config["kind"] == "drawing":
                    model["history"] = list(reversed(history))
                    group["drawing_models"].append(model)
                else:
                    day = [dict(item) for item in self._db.execute(
                        "SELECT status,created_at,public,group_id," + ",".join(_CHANNEL_METRICS)
                        + " FROM channel_metrics WHERE probe_id=? AND created_at>=? ORDER BY created_at,run_id",
                        (row["id"], now - 24 * 3600))
                        if identity[1] or item["public"] and (item["group_id"] is None or item["group_id"] in identity[2])]
                    recent = [item for item in day if item["created_at"] >= now - hours * 3600]
                    aggregate.extend(day)
                    group["_runs"].extend(recent)
                    group["_day_runs"].extend(day)
                    model.update(stats={name: value for name, value in _stats(history).items() if name in _MODEL_FIELDS},
                                 history=history)
                    group["models"].append(model)
            for group in groups.values():
                multipliers = set(group.pop("_multipliers"))
                group["display_multiplier"] = next(iter(multipliers)) if len(multipliers) == 1 else None
                recent = group.pop("_runs")
                group["stats"] = {name: value for name, value in _stats(recent).items() if name in _MONITOR_FIELDS}
                group["summary"] = {name: value for name, value in _stats(group.pop("_day_runs")).items()
                                    if name in _SUMMARY_FIELDS}
            stats = _stats(aggregate)
            return {"updated_at": _iso(now), "summary": {
                "probes": count, "enabled": enabled,
                **{name: stats[name] for name in _SUMMARY_FIELDS},
            }, "groups": list(groups.values())}

    def _schedule_loop(self):
        while not self._stop.wait(5):
            try:
                self._scheduler_tick()
            except Exception:
                # Retry the scheduler scan, never a previously submitted paid request.
                continue

    def _scheduler_tick(self):
        with self._lock:
            if self._closing:
                return
            now = time.time()
            if time.monotonic() - self._last_prune_monotonic >= _PRUNE_INTERVAL_SECONDS:
                with self._db:
                    self._prune_locked(now)
            rows = list(self._db.execute("SELECT * FROM probes WHERE deleted=0 AND next_run_at IS NOT NULL AND next_run_at<=?",
                                         (now,)))
            for row in rows:
                config = json.loads(row["config_json"])
                if not config["enabled"] or row["id"] in self._active_probes:
                    continue
                try:
                    spec = self._probe_spec(row)
                    self._enqueue(0, row["id"], str(uuid.uuid4()), spec,
                                  schedule_next_at=now + config["interval_minutes"] * 60)
                except LabError as error:
                    if error.code in {"missing_key", "key_unavailable"}:
                        config["enabled"] = False
                        with self._db:
                            self._db.execute("UPDATE probes SET config_json=?,next_run_at=NULL WHERE id=?",
                                             (json.dumps(config, ensure_ascii=False), row["id"]))
                    continue

    def _finish_close_locked(self):
        if self._closing and not self._tasks and not self._closed:
            self._db.close()
            self._closed = True
            _SharedPool.release()

    @_public
    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closing = True
            self._stop.set()
            futures = []
            for task in list(self._tasks.values()):
                task["event"].set()
                if task["future"]:
                    futures.append(task["future"])
                    task["future"].cancel()
        self._scheduler.join(timeout=6)
        if futures:
            wait(futures, timeout=10)
        with self._lock:
            # A runner still exiting owns its connection until its done callback.
            self._finish_close_locked()
