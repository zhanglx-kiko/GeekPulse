"""Bounded, public-HTTPS-only model probes. Python 3.11; no third-party packages.

The caller owns authentication, scheduling and encrypted key storage. This module
never logs requests, credentials or upstream error bodies, and never retries a
billable request. ``artifact_html`` still requires an empty iframe sandbox.
"""
from __future__ import annotations

import codecs
import html
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_CHARS = 300_000
# Project measurement-quality gate, not a provider limit or a speed cap.
MIN_TEXT_DELIVERY_SECONDS = 0.1
_USAGE_KEYS = ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "total_tokens")
_ENDPOINTS = {"responses": "/responses", "chat_completions": "/chat/completions"}
REASONING_EFFORTS = frozenset(("", "default", "none", "minimal", "low", "medium", "high", "xhigh", "max"))
REASONING_REQUIRED_MESSAGE = "该模型的最低显式推理强度为低（low）。"
MINIMAL_UNAVAILABLE_MESSAGE = "该模型不支持最低（minimal），请选择其他推理强度。"
MODEL_EFFORT_SUFFIX_MESSAGE = "模型名带有不支持的推理强度后缀，请改用不带该后缀的模型。"
# Sub2API v0.2.23 recognizes these effort and compact suffixes.
REASONING_EFFORT_BLOCKS = {
    model + suffix: blocked
    for model, blocked in (
        ("gpt-6.1-sol", ("none", "minimal")),
        ("gpt-6.1-terra", ("minimal",)),
        ("gpt-6.2-sol", ("none", "minimal")),
        ("gpt-6.2-astra", ("none", "minimal")),
    )
    for suffix in ("", "-none", "-minimal", "-low", "-medium", "-high", "-xhigh", "-max", "-openai-compact")
}
_CSP = ("default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; "
        "img-src 'none'; font-src 'none'; connect-src 'none'; media-src 'none'; "
        "object-src 'none'; frame-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'")


def _canonical_reasoning_model(model):
    name = model.rsplit("/", 1)[-1].strip().lower()
    return re.sub(r"-+", "-", "-".join(name.replace("_", "-").split()))


def blocked_reasoning_efforts(model):
    return REASONING_EFFORT_BLOCKS.get(_canonical_reasoning_model(model), ())


def reasoning_configuration_error(model, effort=None):
    name = _canonical_reasoning_model(model)
    blocked = REASONING_EFFORT_BLOCKS.get(name, ())
    if name.rsplit("-", 1)[-1] in blocked:
        return MODEL_EFFORT_SUFFIX_MESSAGE
    if effort in blocked:
        return reasoning_effort_error(blocked)
    return None


def reasoning_effort_error(blocked):
    return REASONING_REQUIRED_MESSAGE if "none" in blocked else MINIMAL_UNAVAILABLE_MESSAGE


class ProbeError(Exception):
    """Only fixed, non-sensitive explanations may be passed to this exception."""


def _check_cancel(cancel_event: threading.Event, deadline: float) -> None:
    if cancel_event.is_set():
        raise ProbeError("任务已取消；上游可能已经产生费用。")
    if time.monotonic() >= deadline:
        raise ProbeError("请求超过时间限制；未自动重试。")


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    path: str


def normalize_endpoint(base_url: str, protocol: str) -> Endpoint:
    if protocol not in _ENDPOINTS:
        raise ProbeError("不支持的接口协议。")
    if not isinstance(base_url, str) or not base_url or len(base_url) > 2048:
        raise ProbeError("上游地址无效。")
    if any(ord(c) <= 32 or ord(c) == 127 for c in base_url) or "\\" in base_url:
        raise ProbeError("上游地址含不允许的字符。")
    try:
        parsed = urlsplit(base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or "%" in parsed.netloc):
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = 443 if parsed.port is None else parsed.port
        if not 1 <= port <= 65535 or host.endswith("."):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            labels = host.split(".")
            if (len(host) > 253 or len(labels) < 2 or
                    any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
                    or labels[-1] in {"localhost", "local", "internal", "lan", "home", "invalid", "test"}):
                raise ValueError
        path = parsed.path.rstrip("/")
        if (not path.isascii() or "%" in path or "//" in path
                or any(part in {".", ".."} for part in path.split("/"))):
            raise ValueError
        suffix = _ENDPOINTS[protocol]
        other = _ENDPOINTS["responses" if protocol == "chat_completions" else "chat_completions"]
        if path.endswith(other):
            raise ProbeError("上游接口路径与协议不一致。")
        if path.endswith(suffix):
            pass
        elif not path:
            path = "/v1" + suffix
        elif path.endswith("/v1"):
            path += suffix
        else:
            path += "/v1" + suffix
        return Endpoint(host, port, path)
    except (ValueError, UnicodeError):
        raise ProbeError("仅允许不带凭据、查询串和片段的公网 HTTPS 地址。") from None


_TRANSITION_NETS = tuple(ipaddress.ip_network(n) for n in (
    "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32", "fec0::/10",
))
_SPECIAL_V4_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
))


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_reserved or address.is_unspecified:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None or address.scope_id is not None:
            return False
        if any(address in network for network in _TRANSITION_NETS):
            return False
    elif any(address in network for network in _SPECIAL_V4_NETS):
        return False
    return True


def _resolve_public(endpoint: Endpoint, cancel_event: threading.Event, deadline: float) -> tuple:
    """Resolve exactly once, validate every answer, and return one pinned sockaddr.

    getaddrinfo has no portable cancellation API. A daemon resolver lets callers
    honour their deadline without waiting for the OS resolver; it never connects.
    """
    done = threading.Event()
    box: dict = {}

    def resolve() -> None:
        try:
            box["answers"] = socket.getaddrinfo(endpoint.host, endpoint.port, type=socket.SOCK_STREAM)
        except Exception:
            box["failed"] = True
        finally:
            done.set()

    _check_cancel(cancel_event, deadline)
    threading.Thread(target=resolve, name="model-lab-dns", daemon=True).start()
    while not done.wait(0.05):
        _check_cancel(cancel_event, deadline)
    _check_cancel(cancel_event, deadline)
    answers = box.get("answers")
    if not answers:
        raise ProbeError("无法解析上游公网地址。")
    for family, socktype, _proto, _canonname, sockaddr in answers:
        if (family not in (socket.AF_INET, socket.AF_INET6) or socktype != socket.SOCK_STREAM
                or not _is_public_ip(sockaddr[0]) or sockaddr[1] != endpoint.port
                or (family == socket.AF_INET6 and len(sockaddr) > 3 and sockaddr[3] != 0)):
            raise ProbeError("上游地址解析包含非公网或不允许的地址。")
    family, _socktype, _proto, _canonname, sockaddr = answers[0]
    return family, sockaddr


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, endpoint: Endpoint, pinned: tuple, timeout: float):
        super().__init__(endpoint.host, endpoint.port, timeout=timeout, context=ssl.create_default_context())
        self._pinned = pinned

    def connect(self) -> None:
        # Never call create_connection(host): it would resolve DNS a second time.
        family, sockaddr = self._pinned
        raw = socket.socket(family, socket.SOCK_STREAM)
        self.sock = raw
        raw.settimeout(self.timeout)
        try:
            raw.connect(sockaddr)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
            self.sock.do_handshake()
        except Exception:
            raw.close()
            self.sock = None
            raise


def _close_socket(connection: _PinnedHTTPSConnection) -> None:
    sock = connection.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        try:
            sock.close()
        except OSError:
            pass


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _json(value: str | bytes):
    def reject_constant(_value):
        raise ValueError("non-finite JSON")
    try:
        return json.loads(value, object_pairs_hook=_unique_object, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ProbeError("上游返回了无效或不完整的 JSON。") from None


def _number(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _usage(value, protocol: str) -> dict:
    result = dict.fromkeys(_USAGE_KEYS)
    if not isinstance(value, dict):
        return result
    prefix_in, prefix_out = ("prompt_tokens", "completion_tokens") if protocol == "chat_completions" else ("input_tokens", "output_tokens")
    result["input_tokens"] = _number(value.get(prefix_in))
    result["output_tokens"] = _number(value.get(prefix_out))
    result["total_tokens"] = _number(value.get("total_tokens"))
    in_details, out_details = value.get(prefix_in + "_details"), value.get(prefix_out + "_details")
    if isinstance(in_details, dict):
        result["cached_tokens"] = _number(in_details.get("cached_tokens"))
    if isinstance(out_details, dict):
        result["reasoning_tokens"] = _number(out_details.get("reasoning_tokens"))
    return result


def _sse_events(chunks):
    """Incremental UTF-8 and SSE framing, including CRLF split across reads."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    line, data, event = [], [], "message"
    skip_lf = False
    first = True
    try:
        for chunk in chunks:
            text = decoder.decode(chunk)
            if first and text:
                text = text.removeprefix("\ufeff")
                first = False
            for char in text:
                if skip_lf:
                    skip_lf = False
                    if char == "\n":
                        continue
                if char not in "\r\n":
                    line.append(char)
                    continue
                skip_lf = char == "\r"
                current = "".join(line)
                line.clear()
                if not current:
                    if data:
                        yield event, "\n".join(data)
                    data, event = [], "message"
                elif not current.startswith(":"):
                    key, sep, value = current.partition(":")
                    if sep and value.startswith(" "):
                        value = value[1:]
                    if key == "data":
                        data.append(value)
                    elif key == "event":
                        event = value
        decoder.decode(b"", final=True)
    except UnicodeError:
        raise ProbeError("上游流不是有效的 UTF-8。") from None
    # A pending event at EOF is deliberately discarded, per SSE framing rules.


def _response_text(response: dict) -> str:
    if not isinstance(response, dict) or response.get("status") != "completed":
        raise ProbeError("上游响应未完整完成。")
    output = response.get("output")
    if not isinstance(output, list):
        raise ProbeError("上游响应缺少最终输出。")
    texts = []
    for item in output:
        if not isinstance(item, dict):
            raise ProbeError("上游最终输出结构无效。")
        if item.get("type") != "message":
            continue
        if item.get("role") != "assistant" or item.get("status", "completed") != "completed":
            raise ProbeError("上游助手消息未完整完成。")
        content = item.get("content")
        if not isinstance(content, list):
            raise ProbeError("上游最终输出结构无效。")
        for part in content:
            if not isinstance(part, dict):
                raise ProbeError("上游最终输出结构无效。")
            if part.get("type") == "refusal":
                raise ProbeError("上游拒绝生成所请求的内容。")
            if part.get("type") == "output_text":
                if not isinstance(part.get("text"), str):
                    raise ProbeError("上游最终文本无效。")
                texts.append(part["text"])
    text = "".join(texts)
    if not text.strip():
        raise ProbeError("上游没有返回可用的最终文本。")
    return text


def _consume(chunks, content_type: str, protocol: str,
             started: float) -> tuple[str, dict, float | None, float | None]:
    if content_type != "text/event-stream":
        if not (content_type == "application/json" or content_type.endswith("+json")):
            raise ProbeError("上游没有返回 JSON 或 SSE。")
        body = _json(b"".join(chunks))
        if not isinstance(body, dict):
            raise ProbeError("上游响应结构无效。")
        if protocol == "responses":
            text = _response_text(body)
        else:
            choices = body.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise ProbeError("上游响应缺少唯一的最终答案。")
            choice = choices[0]
            if choice.get("finish_reason") != "stop":
                raise ProbeError("上游答案被截断、过滤或转入了工具调用。")
            message = choice.get("message")
            if not isinstance(message, dict) or message.get("refusal"):
                raise ProbeError("上游没有返回可评分的助手消息。")
            text = message.get("content")
            if not isinstance(text, str) or not text.strip():
                raise ProbeError("上游没有返回可用的最终文本。")
        # A buffered JSON response cannot establish when the first token arrived.
        return text, _usage(body.get("usage"), protocol), None, None

    usage = dict.fromkeys(_USAGE_KEYS)
    fragments: list[str] = []
    stopped = False
    response_parts: dict[tuple[int, int], str] = {}
    read_batch = 0
    arrived_at = started
    first_text_at = last_text_at = None
    last_text_batch = None
    text_batches = 0

    def timed_chunks():
        nonlocal read_batch, arrived_at
        for chunk in chunks:
            # Timestamp the read once: parsing several buffered SSE events must
            # not manufacture separate text arrivals or generation time.
            read_batch += 1
            arrived_at = time.monotonic()
            yield chunk

    def mark_delta(text):
        nonlocal first_text_at, last_text_at, last_text_batch, text_batches
        if text and read_batch != last_text_batch:
            if first_text_at is None:
                first_text_at = arrived_at
            last_text_at = arrived_at
            last_text_batch = read_batch
            text_batches += 1

    def with_timing(text, counters):
        ttft = (first_text_at - started) * 1000 if first_text_at is not None else None
        speed = None
        output, reasoning = counters["output_tokens"], counters["reasoning_tokens"]
        # This is observed visible-text delivery throughput, not provider
        # compute speed. Missing reasoning usage cannot safely mean zero.
        if (text_batches >= 2 and "".join(fragments) == text
                and output is not None and reasoning is not None and output > reasoning):
            elapsed = last_text_at - first_text_at
            if elapsed >= MIN_TEXT_DELIVERY_SECONDS:
                speed = round((output - reasoning) / elapsed, 2)
        return text, counters, ttft, speed

    for event, data in _sse_events(timed_chunks()):
        if data == "[DONE]":
            if protocol != "chat_completions" or not stopped:
                raise ProbeError("上游流结束前没有确认完整答案。")
            text = "".join(fragments)
            if not text.strip():
                raise ProbeError("上游没有返回可用的最终文本。")
            return with_timing(text, usage)
        value = _json(data)
        if not isinstance(value, dict) or value.get("error") or event == "error":
            raise ProbeError("上游报告了生成错误。")
        if protocol == "chat_completions":
            if value.get("usage") is not None:
                usage = _usage(value["usage"], protocol)
            choices = value.get("choices")
            if not isinstance(choices, list) or len(choices) > 1:
                raise ProbeError("上游流的答案结构无效。")
            for choice in choices:
                if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                    raise ProbeError("上游流返回了非预期的答案。")
                delta = choice.get("delta", {})
                if not isinstance(delta, dict) or delta.get("refusal") or delta.get("tool_calls"):
                    raise ProbeError("上游未返回可评分的文本答案。")
                text = delta.get("content")
                if text is not None:
                    if not isinstance(text, str) or (stopped and text):
                        raise ProbeError("上游流的文本顺序无效。")
                    fragments.append(text)
                    mark_delta(text)
                finish = choice.get("finish_reason")
                if finish is not None:
                    if finish != "stop":
                        raise ProbeError("上游答案被截断、过滤或转入了工具调用。")
                    stopped = True
        else:
            kind = value.get("type", event)
            if event not in ("message", kind):
                raise ProbeError("上游 SSE 事件类型不一致。")
            if kind in {"response.failed", "response.incomplete", "error"}:
                raise ProbeError("上游响应失败或未完整完成。")
            if kind in {"response.refusal.delta", "response.refusal.done"}:
                raise ProbeError("上游拒绝生成所请求的内容。")
            if kind in {"response.output_text.delta", "response.output_text.done"}:
                indexes = value.get("output_index"), value.get("content_index")
                if any(type(index) is not int or index < 0 or index > 1000 for index in indexes):
                    raise ProbeError("上游文本索引无效。")
                text = value.get("delta" if kind.endswith(".delta") else "text")
                if not isinstance(text, str):
                    raise ProbeError("上游流的最终文本无效。")
                if kind.endswith(".delta"):
                    response_parts[indexes] = response_parts.get(indexes, "") + text
                    fragments.append(text)
                    mark_delta(text)
                else:
                    if indexes in response_parts and response_parts[indexes] != text:
                        raise ProbeError("上游文本终稿与增量不一致。")
                    response_parts[indexes] = text
            elif kind == "response.completed":
                response = value.get("response")
                text = _response_text(response)
                return with_timing(text, _usage(response.get("usage"), protocol))
    raise ProbeError("上游流已中断，未收到完整结束标记。")


def _request(endpoint: Endpoint, pinned: tuple, payload: bytes, api_key: str,
             protocol: str, cancel_event: threading.Event, deadline: float, started: float):
    _check_cancel(cancel_event, deadline)
    connection = _PinnedHTTPSConnection(endpoint, pinned, max(0.01, deadline - time.monotonic()))
    finished = threading.Event()

    def watchdog():
        while not finished.wait(0.05):
            if cancel_event.is_set() or time.monotonic() >= deadline:
                _close_socket(connection)
                return

    watcher = threading.Thread(target=watchdog, name="model-lab-cancel", daemon=True)
    watcher.start()
    try:
        connection.request("POST", endpoint.path, body=payload, headers={
            "Authorization": "Bearer " + api_key, "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json", "Accept-Encoding": "identity",
            "User-Agent": "GeekPulse",
        })
        response = connection.getresponse()
        _check_cancel(cancel_event, deadline)
        if response.status != 200:
            # In particular, do not follow redirects or read/reflect their body.
            raise ProbeError(f"上游接口返回 HTTP {int(response.status)}；未自动重试。")
        if sum(len(k) + len(v) for k, v in response.getheaders()) > 32_768:
            raise ProbeError("上游响应头超过限制。")
        if response.getheader("Content-Encoding", "identity").strip().lower() not in {"", "identity"}:
            raise ProbeError("上游返回了不支持的压缩响应。")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
            raise ProbeError("上游响应超过 2 MiB 限制。")
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()

        def chunks():
            consumed = 0
            while True:
                _check_cancel(cancel_event, deadline)
                chunk = response.read1(min(8192, MAX_RESPONSE_BYTES - consumed + 1))
                _check_cancel(cancel_event, deadline)
                if not chunk:
                    return
                consumed += len(chunk)
                if consumed > MAX_RESPONSE_BYTES:
                    raise ProbeError("上游响应超过 2 MiB 限制。")
                yield chunk

        return _consume(chunks(), content_type, protocol, started)
    finally:
        finished.set()
        _close_socket(connection)
        connection.close()
        watcher.join(timeout=0.2)


def candy_guaranteed(round_count: int, star_count: int) -> bool:
    return (type(round_count) is int and type(star_count) is int
            and 0 <= round_count <= 24 and 0 <= star_count <= 17
            and round_count > 8 and star_count > 4
            and (round_count > 15 or star_count > 11)
            and (round_count > 17 or star_count > 10))


def score_answer(text: str, preset_id: str, expected_answer) -> str:
    if preset_id == "candy":
        try:
            value = _json(text)
        except ProbeError:
            return "failed"
        if not isinstance(value, dict) or set(value) != {"answer", "round", "star"}:
            return "failed"
        answer, rounds, stars = value["answer"], value["round"], value["star"]
        passed = (type(answer) is int and answer == 21 and candy_guaranteed(rounds, stars)
                  and rounds + stars == answer)
        return "passed" if passed else "failed"
    if expected_answer is None or str(expected_answer).strip() == "":
        return "completed"
    return "passed" if text.strip() == str(expected_answer).strip() else "failed"


# CSS is parsed into balanced blocks/declarations, then checked against a small
# property/function vocabulary. CSS escapes and arbitrary resource functions are
# deliberately unsupported; they are not needed for a self-contained SVG scene.
_CSS_PROPERTIES = set("""color background background-color background-image opacity display position
top right bottom left inset width height min-width max-width min-height max-height
margin margin-top margin-right margin-bottom margin-left padding padding-top padding-right padding-bottom padding-left
border border-width border-style border-color border-radius box-sizing box-shadow overflow overflow-x overflow-y
font font-family font-size font-style font-weight line-height letter-spacing text-align text-shadow text-transform
white-space word-break vertical-align visibility z-index align-items align-content align-self justify-content justify-items
gap row-gap column-gap flex flex-direction flex-wrap flex-grow flex-shrink flex-basis order grid grid-template-columns
grid-template-rows grid-column grid-row place-items place-content transform transform-origin transform-box perspective
animation animation-name animation-duration animation-delay animation-iteration-count animation-direction
animation-timing-function animation-fill-mode animation-play-state transition transition-property transition-duration
transition-delay transition-timing-function fill fill-opacity fill-rule stroke stroke-width stroke-opacity stroke-linecap
stroke-linejoin stroke-dasharray stroke-dashoffset paint-order filter clip-path isolation content user-select
""".split())
_CSS_FUNCTIONS = set("""rgb rgba hsl hsla hwb lab lch oklab oklch color color-mix var calc min max clamp
translate translatex translatey translatez translate3d rotate rotatex rotatey rotatez rotate3d scale scalex scaley
scalez scale3d skew skewx skewy matrix matrix3d cubic-bezier steps linear repeat minmax fit-content
linear-gradient radial-gradient conic-gradient repeating-linear-gradient repeating-radial-gradient
blur brightness contrast grayscale invert opacity saturate sepia drop-shadow inset circle ellipse polygon path
""".split())


def _strip_css_comments(text: str) -> str:
    result, index, quote = [], 0, None
    while index < len(text):
        char = text[index]
        if quote:
            result.append(char)
            if char == quote:
                quote = None
            index += 1
        elif char in "\"'":
            quote = char
            result.append(char)
            index += 1
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                return ""
            result.append(" ")
            index = end + 2
        else:
            result.append(char)
            index += 1
    return "".join(result) if quote is None else ""


def _safe_css_value(value: str) -> bool:
    if not value or len(value) > 4096 or any(c in value for c in "\\<>@{}"):
        return False
    if any(ord(c) < 32 and c not in "\t\n\r" for c in value):
        return False
    depth, quote, index = 0, None, 0
    while index < len(value):
        char = value[index]
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char.isalpha() or char in "_-":
            end = index + 1
            while end < len(value) and (value[end].isalnum() or value[end] in "_-"):
                end += 1
            word = value[index:end].lower()
            after = end
            while after < len(value) and value[after].isspace():
                after += 1
            if after < len(value) and value[after] == "(" and word not in _CSS_FUNCTIONS:
                return False
            if word in {"expression", "javascript", "vbscript", "url", "image-set"}:
                return False
            index = end - 1
        elif char == "(":
            depth += 1
            if depth > 16:
                return False
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
        elif char not in "0123456789.,%#:/+-*=! \t\r\n":
            return False
        index += 1
    return depth == 0 and quote is None


def _split_css(text: str, separator: str) -> list[str]:
    result, start, depth, quote = [], 0, 0, None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == separator and depth == 0:
            result.append(text[start:index])
            start = index + 1
    if depth != 0 or quote is not None:
        return []
    result.append(text[start:])
    return result


def _css_declarations(text: str) -> str:
    safe = []
    for declaration in _split_css(text, ";"):
        prop, separator, value = declaration.partition(":")
        prop, value = prop.strip().lower(), value.strip()
        if separator and (prop in _CSS_PROPERTIES or re.fullmatch(r"--[a-z][a-z0-9-]{0,63}", prop)) and _safe_css_value(value):
            safe.append(prop + ":" + value)
    return ";".join(safe)


def _css_blocks(text: str):
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index == len(text):
            return
        start, quote = index, None
        while index < len(text):
            char = text[index]
            if quote:
                if char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char == "{":
                break
            elif char in "};":
                raise ValueError
            index += 1
        if index == len(text):
            raise ValueError
        prelude, body_start = text[start:index].strip(), index + 1
        index += 1
        depth = 1
        while index < len(text) and depth:
            char = text[index]
            if quote:
                if char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char == "{":
                depth += 1
                if depth > 16:
                    raise ValueError
            elif char == "}":
                depth -= 1
            index += 1
        if depth or quote:
            raise ValueError
        yield prelude, text[body_start:index - 1]


def sanitize_css(text: str, inline: bool = False, _depth: int = 0) -> str:
    if len(text) > 65_536 or "\\" in text or "<" in text or ">" in text or _depth > 6:
        return ""
    text = _strip_css_comments(text)
    if inline:
        return _css_declarations(text)
    safe = []
    try:
        for selector, body in _css_blocks(text):
            if selector.startswith("@"):
                if re.fullmatch(r"@(?:-webkit-)?keyframes\s+[a-zA-Z_][\w-]{0,63}", selector):
                    frames = []
                    for frame, declarations in _css_blocks(body):
                        if all(re.fullmatch(r"(?:from|to|(?:100|\d{1,2})(?:\.\d+)?%)", part.strip()) for part in frame.split(",")):
                            frames.append(frame + "{" + _css_declarations(declarations) + "}")
                    if frames:
                        safe.append(selector + "{" + "".join(frames) + "}")
                elif re.fullmatch(r"@media\s+[a-zA-Z0-9\s():.,/-]{1,200}", selector):
                    nested = sanitize_css(body, _depth=_depth + 1)
                    if nested:
                        safe.append(selector + "{" + nested + "}")
            elif re.fullmatch(r"[\w\s.#,:+~*=\[\]\"'()%-]{1,512}", selector):
                declarations = _css_declarations(body)
                if declarations:
                    safe.append(selector + "{" + declarations + "}")
    except ValueError:
        return ""
    return "".join(safe)


_HTML_TAGS = set("div span main section article header footer p h1 h2 h3 h4 h5 h6 ul ol li strong em b i small br hr figure figcaption style".split())
_SVG_TAGS = set("svg g path rect circle ellipse line polyline polygon text tspan defs lineargradient radialgradient stop clippath mask pattern use symbol title desc animatetransform animate".split())
_CANONICAL_TAG = {"lineargradient": "linearGradient", "radialgradient": "radialGradient", "clippath": "clipPath", "animatetransform": "animateTransform"}
_WRAPPERS = {"html", "head", "body"}
_VOID = set("area base br col embed hr img input link meta param source track wbr".split())
_SVG_ATTRS = set("x y x1 x2 y1 y2 cx cy r rx ry width height d points transform opacity fill fill-opacity fill-rule stroke stroke-width stroke-opacity stroke-linecap stroke-linejoin stroke-dasharray stroke-dashoffset viewbox preserveaspectratio version offset stop-color stop-opacity gradientunits gradienttransform spreadmethod clippathunits clip-path mask maskunits maskcontentunits patternunits patterncontentunits patterntransform text-anchor dominant-baseline font-family font-size font-weight letter-spacing dx dy rotate pathlength vector-effect marker-start marker-mid marker-end".split())
_ATTR_CASE = {"viewbox": "viewBox", "preserveaspectratio": "preserveAspectRatio", "gradientunits": "gradientUnits", "gradienttransform": "gradientTransform", "spreadmethod": "spreadMethod", "clippathunits": "clipPathUnits", "maskunits": "maskUnits", "maskcontentunits": "maskContentUnits", "patternunits": "patternUnits", "patterncontentunits": "patternContentUnits", "patterntransform": "patternTransform", "pathlength": "pathLength", "attributename": "attributeName", "repeatcount": "repeatCount", "calcmode": "calcMode", "keytimes": "keyTimes", "keysplines": "keySplines"}
_LOCAL_REF = re.compile(r"#[A-Za-z_][\w.-]{0,80}\Z")
_LOCAL_PAINT = re.compile(r"url\(\s*(['\"]?)(#[A-Za-z_][\w.-]{0,80})\1\s*\)\Z")
_ANIM_ATTRS = set("attributename type from to by values dur begin repeatcount fill additive accumulate calcmode keytimes keysplines".split())
_ANIMATED_NAMES = set("x y x1 x2 y1 y2 cx cy r rx ry opacity fill-opacity stroke-opacity stroke-width stroke-dashoffset".split())


def _svg_value(value: str) -> bool:
    return bool(len(value) <= 8192 and (not value or _LOCAL_PAINT.fullmatch(value) or
                (not any(c in value for c in "\\<>&@;{}") and _safe_css_value(value))))


def _attributes(tag: str, attrs: list[tuple[str, str | None]]) -> str:
    result = []
    values = dict(attrs)
    if len(values) != len(attrs):
        raise ProbeError("作品含重复属性，无法安全预览。")
    for name, value in attrs:
        if value is None or len(value) > 8192 or name.startswith("on"):
            continue
        allowed = False
        if name in {"id", "class"}:
            allowed = bool(re.fullmatch(r"[A-Za-z0-9_\s.-]{1,160}", value))
        elif name == "style":
            value = sanitize_css(value, inline=True)
            allowed = bool(value)
        elif name in {"role", "aria-label", "aria-hidden"}:
            allowed = len(value) <= 200
        elif name in {"href", "xlink:href"} and tag in {"use", "lineargradient", "radialgradient", "pattern"}:
            allowed = bool(_LOCAL_REF.fullmatch(value))
            name = "href"
        elif tag == "svg" and name == "xmlns":
            value, allowed = "http://www.w3.org/2000/svg", True
        elif tag in {"animate", "animatetransform"} and name in _ANIM_ATTRS:
            if name == "attributename":
                allowed = value == "transform" if tag == "animatetransform" else value in _ANIMATED_NAMES
            elif name == "type":
                allowed = value in {"translate", "rotate", "scale", "skewX", "skewY"}
            elif name in {"from", "to", "by", "values", "keytimes", "keysplines"}:
                allowed = bool(re.fullmatch(r"[0-9eE+.,;%\s-]{1,2048}", value))
            elif name in {"dur", "begin"}:
                allowed = bool(re.fullmatch(r"(?:\d{1,5}(?:\.\d{1,3})?)(?:ms|s)?", value))
            elif name == "repeatcount":
                allowed = value == "indefinite" or bool(re.fullmatch(r"\d{1,5}(?:\.\d+)?", value))
            elif name == "fill":
                allowed = value in {"freeze", "remove"}
            elif name == "additive":
                allowed = value in {"sum", "replace"}
            elif name == "accumulate":
                allowed = value in {"sum", "none"}
            elif name == "calcmode":
                allowed = value in {"linear", "discrete", "paced", "spline"}
        elif tag in _SVG_TAGS and name in _SVG_ATTRS:
            allowed = _svg_value(value)
        if allowed:
            result.append(f' {_ATTR_CASE.get(name, name)}="{html.escape(value, quote=True)}"')
    return "".join(result)


class _ArtifactParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool, bool]] = []
        self.parts: list[str] = []
        self.styles: list[str] = []
        self.nodes = 0
        self.visible = 0

    def handle_decl(self, decl):
        if decl.lower() != "doctype html":
            raise ProbeError("作品含不支持的文档声明。")

    def unknown_decl(self, data):
        raise ProbeError("作品含不支持的文档结构。")

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 8000 or len(self.stack) > 64:
            raise ProbeError("作品结构超过预览限制。")
        parent_drop = self.stack[-1][1] if self.stack else False
        svg_parent = any(item[0] == "svg" for item in self.stack)
        allowed = tag in _HTML_TAGS or (tag in _SVG_TAGS and (svg_parent or tag == "svg"))
        if tag in {"animate", "animatetransform"}:
            target = dict(attrs).get("attributename")
            allowed = allowed and (target == "transform" if tag == "animatetransform" else target in _ANIMATED_NAMES)
        drop = parent_drop or (not allowed and tag not in _WRAPPERS)
        emit = allowed and not drop and tag != "style"
        if tag not in _VOID:
            self.stack.append((tag, drop, emit))
        if emit:
            self.parts.append("<" + _CANONICAL_TAG.get(tag, tag) + _attributes(tag, attrs) + ">")
            if tag not in {"style", "defs", "title", "desc"}:
                self.visible += 1
        if tag == "style" and not drop:
            self.styles = []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if not self.stack or self.stack[-1][0] != tag:
            raise ProbeError("作品标签不完整或未正确闭合。")
        _tag, drop, emit = self.stack.pop()
        if tag == "style" and not drop:
            css = sanitize_css("".join(self.styles))
            if css:
                self.parts.append("<style>" + css + "</style>")
            self.styles = []
        elif emit:
            self.parts.append("</" + _CANONICAL_TAG.get(tag, tag) + ">")

    def handle_data(self, data):
        if self.stack and self.stack[-1][1]:
            return
        if self.stack and self.stack[-1][0] == "style":
            self.styles.append(data)
        else:
            self.parts.append(html.escape(data, quote=False))


def sanitize_artifact(output_text: str) -> str:
    source = output_text.strip()
    if source.startswith("```"):
        lines = source.splitlines()
        if len(lines) < 3 or lines[0].strip().lower() not in {"```", "```html", "```svg"} or lines[-1].strip() != "```":
            raise ProbeError("作品代码块不完整或含额外内容。")
        source = "\n".join(lines[1:-1]).strip()
    if len(source) > MAX_ARTIFACT_CHARS or not source.startswith("<"):
        raise ProbeError("请返回完整的 HTML 或 SVG 作品。")
    parser = _ArtifactParser()
    try:
        parser.feed(source)
        parser.close()
    except ProbeError:
        raise
    except (ValueError, AssertionError, RecursionError):
        raise ProbeError("作品结构无效，无法安全预览。") from None
    if parser.stack or parser.rawdata.strip() or not parser.visible:
        raise ProbeError("作品不完整或没有可预览的安全内容。")
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="' + html.escape(_CSP, quote=True) + '">'
            '<meta name="referrer" content="no-referrer">'
            '<style>html,body{margin:0;min-height:100%;overflow:auto}svg{max-width:100%;height:auto}</style>'
            '</head><body>' + "".join(parser.parts) + '</body></html>')


def run_probe(spec: dict, cancel_event: threading.Event) -> dict:
    started = time.monotonic()
    result = {"output_text": "", "artifact_html": None, "status": "error", "ttft_ms": None,
              "duration_ms": None, "tokens_per_second": None, "cost_usd": None, "error_message": None,
              **dict.fromkeys(_USAGE_KEYS)}
    deadline = started + 900
    api_key = ""
    try:
        if not isinstance(spec, dict):
            raise ProbeError("任务参数无效。")
        timeout = spec.get("timeout_seconds", 180)
        max_tokens = spec.get("max_output_tokens", 64)
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 1 <= timeout <= 900:
            raise ProbeError("请求时限必须在 1 到 900 秒之间。")
        deadline = started + timeout
        _check_cancel(cancel_event, deadline)
        if type(max_tokens) is not int or not 64 <= max_tokens <= 32768:
            raise ProbeError("输出上限必须在 64 到 32768 Token 之间。")
        protocol, kind = spec.get("protocol"), spec.get("kind")
        if kind not in {"reasoning", "drawing"}:
            raise ProbeError("不支持的任务类型。")
        endpoint = normalize_endpoint(spec.get("base_url"), protocol)
        api_key, model, prompt = spec.get("api_key"), spec.get("model"), spec.get("prompt")
        if not isinstance(api_key, str) or not 1 <= len(api_key) <= 4096 or any(not 33 <= ord(c) <= 126 for c in api_key):
            raise ProbeError("API Key 为空或格式无效。")
        if not isinstance(model, str) or not 1 <= len(model) <= 200 or any(ord(c) < 32 for c in model):
            raise ProbeError("模型名称无效。")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 65_536:
            raise ProbeError("提示词为空或超过长度限制。")
        effort = spec.get("effort", "none") or ""
        if effort not in REASONING_EFFORTS:
            raise ProbeError("不支持的推理强度。")
        if message := reasoning_configuration_error(model, effort):
            raise ProbeError(message)
        payload = {"model": model, "stream": True}
        if protocol == "responses":
            payload.update(input=prompt, instructions="Follow the test prompt exactly.",
                           max_output_tokens=max_tokens, store=False)
            if effort not in {"", "default"}:
                payload["reasoning"] = {"effort": effort}
        else:
            payload.update(messages=[{"role": "user", "content": prompt}], max_completion_tokens=max_tokens,
                           stream_options={"include_usage": True})
            if effort not in {"", "default"}:
                payload["reasoning_effort"] = effort
        pinned = _resolve_public(endpoint, cancel_event, deadline)
        raw, usage, ttft, speed = _request(endpoint, pinned, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                         api_key, protocol, cancel_event, deadline, started)
        _check_cancel(cancel_event, deadline)
        try:
            raw.encode("utf-8", errors="strict")
        except UnicodeError:
            raise ProbeError("上游最终文本含无效的 Unicode 字符。") from None
        text = raw.replace(api_key, "[REDACTED]")
        result.update(output_text=text, ttft_ms=round(ttft, 2) if ttft is not None else None,
                      tokens_per_second=speed, text_rate_version=1, **usage)
        if kind == "drawing":
            artifact = sanitize_artifact(text)
            result["artifact_html"] = artifact.replace(api_key, "[REDACTED]").replace(html.escape(api_key, quote=True), "[REDACTED]")
            result["status"] = "completed"
        else:
            result["status"] = score_answer(text, spec.get("preset_id", "candy"), spec.get("expected_answer"))
        _check_cancel(cancel_event, deadline)
    except ProbeError as exc:
        result["status"] = "cancelled" if cancel_event.is_set() else "error"
        result["error_message"] = str(exc)
    except ssl.SSLCertVerificationError:
        result["error_message"] = "上游 TLS 证书校验失败。"
    except (OSError, http.client.HTTPException, ssl.SSLError):
        result["error_message"] = "上游连接中断或 TLS 握手失败；未自动重试。"
    except Exception:
        result["error_message"] = "任务处理失败；未自动重试。"
    finally:
        if cancel_event.is_set():
            result["status"] = "cancelled"
            result["error_message"] = "任务已取消；上游可能已经产生费用。"
            result["artifact_html"] = None
        elif time.monotonic() >= deadline:
            result["status"] = "error"
            result["error_message"] = "请求超过时间限制；未自动重试。"
            result["artifact_html"] = None
        result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
    return result
