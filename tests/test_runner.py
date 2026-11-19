"""Offline protocol/security regression tests. Never contacts a real provider."""
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "runner.py"
SPEC = importlib.util.spec_from_file_location("model_lab_runner_test", MODULE_PATH)
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def sse(value, event=None):
    payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return (("event: " + event + "\n" if event else "") + "data: " + payload + "\n\n").encode("utf-8")


def chat(text=None, finish=None, usage=None):
    delta = {} if text is None else {"content": text}
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}], "usage": usage}


def completed_response(text="答案", usage=None):
    return {"status": "completed", "output": [{"type": "reasoning", "summary": [{"text": "ignore"}]},
            {"type": "message", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": text}]}], "usage": usage}


def public_answer(ip="93.184.216.34", family=socket.AF_INET):
    sockaddr = (ip, 443) if family == socket.AF_INET else (ip, 443, 0, 0)
    return (family, socket.SOCK_STREAM, 6, "", sockaddr)


class EndpointTests(unittest.TestCase):
    def test_endpoint_normalization(self):
        cases = {"https://provider.example": "/v1/responses",
                 "https://provider.example/v1/": "/v1/responses",
                 "https://provider.example/v1/responses": "/v1/responses",
                 "https://provider.example/proxy": "/proxy/v1/responses"}
        for url, path in cases.items():
            self.assertEqual(runner.normalize_endpoint(url, "responses").path, path)
        self.assertEqual(runner.normalize_endpoint("https://provider.example/v1/chat/completions", "chat_completions").path,
                         "/v1/chat/completions")

    def test_reject_ambiguous_and_non_https_urls(self):
        urls = ["http://example.com", "https://user:key@example.com", "https://example.com/?key=secret",
                "https://example.com/#fragment", "https://example.com\\@private.local", "https://localhost",
                "https://metadata.internal", "https://example.com\n.bad", "https://example.com/%2e%2e/a",
                "https://example.com/../a", "https://example.com:0", "https://example.com:99999",
                "https://[fe80::1%25eth0]/", "https://2130706433", "https://example.com."]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(runner.ProbeError):
                runner.normalize_endpoint(url, "responses")
        with self.assertRaises(runner.ProbeError):
            runner.normalize_endpoint("https://example.com/v1/chat/completions", "responses")

    def test_all_nonpublic_ranges_and_transition_addresses_rejected(self):
        addresses = ["127.0.0.1", "10.0.0.1", "172.16.1.1", "192.168.1.1", "169.254.169.254",
                     "100.100.100.200", "100.64.0.1", "0.0.0.0", "224.0.0.1", "255.255.255.255",
                     "192.0.2.1", "192.0.0.8", "192.88.99.1", "::1", "::", "fc00::1", "fe80::1", "ff02::1", "2001:db8::1",
                     "::ffff:8.8.8.8", "::ffff:127.0.0.1", "64:ff9b::808:808", "2002:0808:0808::1"]
        for address in addresses:
            with self.subTest(address=address):
                self.assertFalse(runner._is_public_ip(address))
        self.assertTrue(runner._is_public_ip("93.184.216.34"))
        self.assertTrue(runner._is_public_ip("2606:4700:4700::1111"))

    def test_dns_requires_every_answer_to_be_public(self):
        endpoint = runner.Endpoint("provider.example", 443, "/v1/responses")
        with mock.patch.object(runner.socket, "getaddrinfo", return_value=[public_answer(), public_answer("127.0.0.1")]) as dns:
            with self.assertRaises(runner.ProbeError):
                runner._resolve_public(endpoint, threading.Event(), time.monotonic() + 5)
            dns.assert_called_once()

    def test_connection_pins_validated_ip_and_tls_hostname_no_second_dns(self):
        endpoint = runner.Endpoint("provider.example", 443, "/v1/responses")
        raw, tls = mock.Mock(), mock.Mock()
        with mock.patch.object(runner.socket, "getaddrinfo", side_effect=[[public_answer()], [public_answer("127.0.0.1")]]) as dns:
            pinned = runner._resolve_public(endpoint, threading.Event(), time.monotonic() + 5)
            connection = runner._PinnedHTTPSConnection(endpoint, pinned, 5)
            context = mock.Mock()
            context.wrap_socket.return_value = tls
            connection._context = context
            with mock.patch.object(runner.socket, "socket", return_value=raw):
                connection.connect()
            dns.assert_called_once()
            raw.connect.assert_called_once_with(("93.184.216.34", 443))
            context.wrap_socket.assert_called_once_with(raw, server_hostname="provider.example", do_handshake_on_connect=False)
            tls.do_handshake.assert_called_once()
            self.assertIs(connection.sock, tls)

    def test_tls_context_defaults_verify_certificates(self):
        connection = runner._PinnedHTTPSConnection(runner.Endpoint("provider.example", 443, "/v1/responses"),
                                                  (socket.AF_INET, ("93.184.216.34", 443)), 5)
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode, runner.ssl.CERT_REQUIRED)

    def test_cancel_and_dns_timeout_are_bounded(self):
        cancelled = threading.Event()
        cancelled.set()
        with mock.patch.object(runner.socket, "getaddrinfo") as dns, self.assertRaises(runner.ProbeError):
            runner._resolve_public(runner.Endpoint("provider.example", 443, "/"), cancelled, time.monotonic() + 5)
        dns.assert_not_called()
        release = threading.Event()
        def slow_dns(*_args, **_kwargs):
            release.wait(1)
            return [public_answer()]
        try:
            with mock.patch.object(runner.socket, "getaddrinfo", side_effect=slow_dns), self.assertRaises(runner.ProbeError):
                runner._resolve_public(runner.Endpoint("provider.example", 443, "/"), threading.Event(), time.monotonic() + 0.01)
        finally:
            release.set()


class StreamingTests(unittest.TestCase):
    def consume(self, chunks, protocol="chat_completions", content_type="text/event-stream"):
        return runner._consume(iter(chunks), content_type, protocol, time.monotonic())

    def test_chat_usage_empty_choices_and_unicode_chunk_boundaries(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 8, "total_tokens": 108,
                 "prompt_tokens_details": {"cached_tokens": 30}, "completion_tokens_details": {"reasoning_tokens": 3}}
        data = (sse(chat("你好")) + sse(chat(finish="stop")) +
                sse({"choices": [], "usage": usage}) + sse("[DONE]")).replace(b"\n", b"\r\n")
        output, counters, ttft, speed = self.consume([bytes([byte]) for byte in data])
        self.assertEqual(output, "你好")
        self.assertEqual(counters, {"input_tokens": 100, "output_tokens": 8, "total_tokens": 108,
                                    "cached_tokens": 30, "reasoning_tokens": 3})
        self.assertGreaterEqual(ttft, 0)
        self.assertIsNone(speed)

    def test_usage_missing_is_null_not_zero(self):
        output, counters, _, speed = self.consume([sse(chat("ok", "stop")), sse("[DONE]")])
        self.assertEqual(output, "ok")
        self.assertTrue(all(value is None for value in counters.values()))
        self.assertIsNone(speed)

    def test_chat_requires_both_stop_and_done(self):
        cases = [[sse(chat("21"))], [sse(chat("21", "stop"))], [sse(chat("21")), sse("[DONE]")],
                 [sse(chat("21", "length")), sse("[DONE]")], [sse(chat("21", "content_filter")), sse("[DONE]")],
                 [sse(chat("21", "tool_calls")), sse("[DONE]")], [sse(chat("21", "stop")), b"data: [DONE]"]]
        for chunks in cases:
            with self.subTest(chunks=chunks), self.assertRaises(runner.ProbeError):
                self.consume(chunks)

    def test_duplicate_json_and_invalid_utf8_rejected(self):
        with self.assertRaises(runner.ProbeError):
            self.consume([b'data: {"choices": [], "choices": []}\n\n'])
        with self.assertRaises(runner.ProbeError):
            self.consume([b"data: \xff\n\n"])

    def test_no_reasoning_or_tool_content_leaks_into_answer(self):
        packet = chat("final", "stop")
        packet["choices"][0]["delta"]["reasoning_content"] = "21 private reasoning"
        self.assertEqual(self.consume([sse(packet), sse("[DONE]")])[0], "final")
        packet = chat("final", "stop")
        packet["choices"][0]["delta"]["refusal"] = "refused"
        with self.assertRaises(runner.ProbeError):
            self.consume([sse(packet), sse("[DONE]")])

    def test_responses_final_output_aggregation_not_duplicate_done(self):
        usage = {"input_tokens": 15, "output_tokens": 5, "total_tokens": 20,
                 "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 2}}
        packets = [sse({"type": "response.output_text.delta", "output_index": 1, "content_index": 0, "delta": "你好"}),
                   sse({"type": "response.output_text.done", "output_index": 1, "content_index": 0, "text": "你好"}),
                   sse({"type": "response.completed", "response": completed_response("你好", usage)})]
        output, counters, _, speed = self.consume(packets, "responses")
        self.assertEqual(output, "你好")
        self.assertEqual(counters["output_tokens"], 5)
        self.assertEqual(counters["cached_tokens"], 0)
        self.assertIsNone(speed)

    def test_responses_requires_complete_status_and_event(self):
        for kind in ("response.failed", "response.incomplete"):
            with self.assertRaises(runner.ProbeError):
                self.consume([sse({"type": kind, "response": {"status": "incomplete"}})], "responses")
        with self.assertRaises(runner.ProbeError):
            self.consume([sse({"type": "response.output_text.done", "output_index": 0, "content_index": 0, "text": "21"})], "responses")
        incomplete = completed_response()
        incomplete["status"] = "incomplete"
        with self.assertRaises(runner.ProbeError):
            self.consume([sse({"type": "response.completed", "response": incomplete})], "responses")

    def test_responses_http_json_uses_output_not_sdk_helper(self):
        body = completed_response("correct", {"input_tokens": 2, "output_tokens": 8,
                                              "output_tokens_details": {"reasoning_tokens": 3}})
        body["output_text"] = "wrong SDK convenience field"
        output, usage, ttft, speed = self.consume([json.dumps(body).encode()], "responses", "application/json")
        self.assertEqual(output, "correct")
        self.assertIsNone(ttft)
        self.assertIsNone(speed)
        self.assertEqual(usage["output_tokens"], 8)
        self.assertEqual(usage["reasoning_tokens"], 3)
        body["output"][1]["content"] = [{"type": "refusal", "refusal": "no"}]
        with self.assertRaises(runner.ProbeError):
            self.consume([json.dumps(body).encode()], "responses", "application/json")

    def test_multiple_final_text_parts_are_aggregated_in_order(self):
        body = completed_response("one")
        body["output"][1]["content"].append({"type": "output_text", "text": "two"})
        self.assertEqual(self.consume([sse({"type": "response.completed", "response": body})], "responses")[0], "onetwo")

    def test_sse_multiline_and_comments(self):
        data = b': heartbeat\nevent: sample\ndata: first\ndata: second\n\n'
        self.assertEqual(list(runner._sse_events([data])), [("sample", "first\nsecond")])

    def text_stream(self, protocol, reasoning=3, output=8):
        if protocol == "chat_completions":
            usage = {"prompt_tokens": 2, "completion_tokens": output,
                     "completion_tokens_details": {"reasoning_tokens": reasoning}}
            return [sse(chat("a")), sse(chat("b")), sse(chat(finish="stop", usage=usage)), sse("[DONE]")]
        usage = {"input_tokens": 2, "output_tokens": output,
                 "output_tokens_details": {"reasoning_tokens": reasoning}}
        return [sse({"type": "response.output_text.delta", "output_index": 1, "content_index": 0, "delta": text})
                for text in ("a", "b")] + [
                    sse({"type": "response.output_text.done", "output_index": 1, "content_index": 0, "text": "ab"}),
                    sse({"type": "response.completed", "response": completed_response("ab", usage)})]

    def consume_at(self, chunks, times, protocol="chat_completions"):
        with mock.patch.object(runner.time, "monotonic", side_effect=times):
            return runner._consume(iter(chunks), "text/event-stream", protocol, 0)

    def test_multiple_deltas_in_one_read_do_not_establish_speed(self):
        for protocol in ("chat_completions", "responses"):
            packets = self.text_stream(protocol)
            for chunks, times in (([b"".join(packets)], [1]),
                                  ([packets[0] + packets[1], *packets[2:]], [1, 8, 10])):
                with self.subTest(protocol=protocol, reads=len(chunks)):
                    output, _, ttft, speed = self.consume_at(chunks, times, protocol)
                    self.assertEqual(output, "ab")
                    self.assertEqual(ttft, 1000)
                    self.assertIsNone(speed)

    def test_distinct_text_reads_measure_visible_tokens_only(self):
        for protocol in ("chat_completions", "responses"):
            with self.subTest(protocol=protocol):
                output, usage, ttft, speed = self.consume_at(self.text_stream(protocol), [1, 3, 8, 10], protocol)
                self.assertEqual(output, "ab")
                self.assertEqual(ttft, 1000)
                self.assertEqual(speed, 2.5)
                self.assertEqual(usage["output_tokens"], 8)
                self.assertEqual(usage["reasoning_tokens"], 3)

    def test_completion_delay_does_not_change_text_delivery_speed(self):
        for protocol in ("chat_completions", "responses"):
            packets = self.text_stream(protocol)
            with self.subTest(protocol=protocol):
                quick = self.consume_at(packets, [1, 3, 3, 3], protocol)
                delayed = self.consume_at(packets, [1, 3, 30, 60], protocol)
                self.assertEqual(quick, delayed)
                self.assertEqual(quick[3], 2.5)

    def test_short_or_identical_read_times_fail_project_quality_gate(self):
        for protocol in ("chat_completions", "responses"):
            for elapsed in (0, 0.0001, 0.099):
                with self.subTest(protocol=protocol, elapsed=elapsed):
                    result = self.consume_at(self.text_stream(protocol), [0, elapsed, 8, 10], protocol)
                    self.assertEqual(result[2], 0)
                    self.assertIsNone(result[3])
            result = self.consume_at(self.text_stream(protocol), [0, 0.1, 8, 10], protocol)
            self.assertEqual(result[3], 50)

    def test_completed_only_or_done_text_cannot_establish_ttft(self):
        packets = self.text_stream("responses")
        for chunks, times in (([packets[-1]], [10]), (packets[2:], [8, 10])):
            with self.subTest(reads=len(chunks)):
                output, usage, ttft, speed = self.consume_at(chunks, times, "responses")
                self.assertEqual(output, "ab")
                self.assertEqual(usage["output_tokens"], 8)
                self.assertIsNone(ttft)
                self.assertIsNone(speed)

    def test_final_text_not_fully_observed_in_deltas_has_no_speed(self):
        packets = self.text_stream("responses")
        usage = {"output_tokens": 8, "output_tokens_details": {"reasoning_tokens": 3}}
        packets[-1] = sse({"type": "response.completed", "response": completed_response("abc", usage)})
        output, _, ttft, speed = self.consume_at(packets, [1, 3, 8, 10], "responses")
        self.assertEqual(output, "abc")
        self.assertEqual(ttft, 1000)
        self.assertIsNone(speed)

    def test_split_delta_frame_uses_arrival_of_complete_event(self):
        for protocol in ("chat_completions", "responses"):
            packets = self.text_stream(protocol)
            chunks = [packets[0][:-1], packets[0][-1:], *packets[1:]]
            with self.subTest(protocol=protocol):
                output, _, ttft, speed = self.consume_at(chunks, [1, 2, 4, 8, 10], protocol)
                self.assertEqual(output, "ab")
                self.assertEqual(ttft, 2000)
                self.assertEqual(speed, 2.5)

    def test_empty_delta_does_not_start_or_extend_text_interval(self):
        packets = self.text_stream("chat_completions")
        chunks = [sse(chat("")), *packets[:2], sse(chat("")), *packets[2:]]
        output, _, ttft, speed = self.consume_at(chunks, [0.5, 1, 3, 6, 8, 10])
        self.assertEqual(output, "ab")
        self.assertEqual(ttft, 1000)
        self.assertEqual(speed, 2.5)

    def test_reasoning_usage_must_be_known_valid_and_leave_visible_tokens(self):
        for protocol in ("chat_completions", "responses"):
            for reasoning, expected in ((None, None), (-1, None), (False, None), (3.5, None),
                                        (9, None), (8, None), (0, 4), (3, 2.5)):
                with self.subTest(protocol=protocol, reasoning=reasoning):
                    _, usage, _, speed = self.consume_at(self.text_stream(protocol, reasoning), [1, 3, 8, 10], protocol)
                    self.assertEqual(speed, expected)
                    self.assertEqual(usage["output_tokens"], 8)
                    # Preserve upstream integer counters even when inconsistent.
                    self.assertEqual(usage["reasoning_tokens"], reasoning if type(reasoning) is int and reasoning >= 0 else None)

    def test_absent_reasoning_details_do_not_assume_zero(self):
        for protocol in ("chat_completions", "responses"):
            packets = self.text_stream(protocol)
            if protocol == "chat_completions":
                packets[2] = sse(chat(finish="stop", usage={"completion_tokens": 8}))
            else:
                packets[-1] = sse({"type": "response.completed", "response": completed_response("ab", {"output_tokens": 8})})
            with self.subTest(protocol=protocol):
                _, usage, _, speed = self.consume_at(packets, [1, 3, 8, 10], protocol)
                self.assertIsNone(usage["reasoning_tokens"])
                self.assertIsNone(speed)

    def test_invalid_or_zero_output_usage_has_no_speed(self):
        for protocol in ("chat_completions", "responses"):
            for output in (None, -1, False, 0, 3.5):
                with self.subTest(protocol=protocol, output=output):
                    result = self.consume_at(self.text_stream(protocol, reasoning=0, output=output), [1, 3, 8, 10], protocol)
                    self.assertIsNone(result[3])


class ScoringTests(unittest.TestCase):
    def test_candy_exact_structured_strategy(self):
        self.assertEqual(runner.score_answer('{"answer":21,"round":9,"star":12}', "candy", "21"), "passed")
        invalid = ["21", "I saw 21 but answer is 20", '{"answer":20,"round":9,"star":12}',
                   '{"answer":21,"round":12,"star":9}', '{"answer":21,"round":10,"star":11}',
                   '{"answer":21,"round":9,"star":12,"extra":true}', '{"answer":21,"round":true,"star":20}',
                   '{"answer":21.0,"round":9,"star":12}', '{"answer":21,"answer":20,"round":9,"star":12}',
                   '```json\n{"answer":21,"round":9,"star":12}\n```']
        for text in invalid:
            with self.subTest(text=text):
                self.assertEqual(runner.score_answer(text, "candy", "21"), "failed")

    def test_oracle_matches_every_composition(self):
        failures = set()
        for ca in range(8):
            for cp in range(10):
                for cm in range(9):
                    for sa in range(8):
                        for sp in range(7):
                            for sm in range(5):
                                if not ((ca and sp) or (cp and sa)):
                                    failures.add((ca + cp + cm, sa + sp + sm))
        guarantees = []
        for rounds in range(25):
            for stars in range(18):
                self.assertEqual(runner.candy_guaranteed(rounds, stars), (rounds, stars) not in failures)
                if runner.candy_guaranteed(rounds, stars):
                    guarantees.append((rounds + stars, rounds, stars))
        self.assertEqual(min(guarantees), (21, 9, 12))

    def test_custom_exact_match_and_unscored(self):
        self.assertEqual(runner.score_answer("21", "custom", "21"), "passed")
        self.assertEqual(runner.score_answer("answer is 21", "custom", "21"), "failed")
        self.assertEqual(runner.score_answer("anything", "custom", ""), "completed")


class SanitizerTests(unittest.TestCase):
    def test_complete_svg_css_and_local_refs_preserved(self):
        source = '''<svg viewBox="0 0 400 200" xmlns="http://www.w3.org/2000/svg">
          <style>.wheel{animation:spin 2s linear infinite;transform-origin:center}
          @keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}</style>
          <defs><linearGradient id="paint"><stop offset="0%" stop-color="#fff"/></linearGradient></defs>
          <circle class="wheel" id="wheel" cx="100" cy="100" r="30" fill="url(#paint)"/>
          <use href="#wheel" x="80"/><g><animateTransform attributeName="transform" type="rotate"
          from="0 100 100" to="360 100 100" dur="2s" repeatCount="indefinite"/></g></svg>'''
        result = runner.sanitize_artifact(source)
        for expected in ('Content-Security-Policy', 'viewBox="0 0 400 200"', '@keyframes spin', '<linearGradient',
                         'href="#wheel"', 'fill="url(#paint)"', '<animateTransform attributeName="transform"'):
            self.assertIn(expected, result)
        self.assertLess(result.index("Content-Security-Policy"), result.index("<svg"))

    def test_malicious_html_active_content_and_network_removed(self):
        source = '''<!doctype html><html><head><base href="https://evil.example/">
          <meta http-equiv="refresh" content="0;url=https://evil.example/">
          <link rel="stylesheet" href="https://evil.example/x.css"></head><body>
          <script>fetch('https://evil.example/')</script><iframe src="https://evil.example/"></iframe>
          <object data="https://evil.example/"></object><form action="https://evil.example/"><input></form>
          <a href="https://evil.example/">link</a><img src="https://evil.example/x">
          <svg onload="alert(1)"><use href="https://evil.example/x#id"/>
          <foreignObject><div onclick="alert(1)">evil</div></foreignObject>
          <a href="https://evil.example/"><circle r="5"/></a>
          <animate attributeName="href" values="https://evil.example/"/><circle r="5" onclick="alert(1)"/>
          </svg><div style="background-image:url(https://evil.example/);color:red">safe</div></body></html>'''
        result = runner.sanitize_artifact(source)
        for forbidden in ("evil.example", "onclick", "onload", "<script", "<iframe", "<object", "<form", "<base", "<a ", "<img", "foreignObject", 'attributeName="href"'):
            self.assertNotIn(forbidden, result)
        self.assertIn('style="color:red"', result)
        self.assertIn("safe", result)
        self.assertEqual(result.count("Content-Security-Policy"), 1)

    def test_css_network_obfuscation_is_removed(self):
        cases = ["background:u/**/rl(https://evil.example)", r"background:u\72l(https://evil.example)",
                 "--bad:url(https://evil.example)", "background:image-set('https://evil.example')",
                 "behavior:expression(alert(1))"]
        for value in cases:
            self.assertEqual(runner.sanitize_css(value, inline=True), "")
        for sheet in ('@import "https://evil.example"; .x{color:red}',
                      '@font-face{font-family:a;src:url(https://evil.example)}',
                      '.x{background:url(https://evil.example);color:red}'):
            self.assertNotIn("evil.example", runner.sanitize_css(sheet))

    def test_svg_dynamic_href_and_event_timing_removed(self):
        source = '''<svg><animate attributeName="href" values="javascript:alert(1)"/>
          <set attributeName="href" to="https://evil.example"/>
          <animate attributeName="opacity" from="0" to="1" begin="click" dur="2s"/>
          <circle r="10"/></svg>'''
        result = runner.sanitize_artifact(source)
        self.assertNotIn("href", result)
        self.assertNotIn("click", result)
        self.assertIn('attributeName="opacity"', result)

    def test_incomplete_or_ambiguous_structure_rejected(self):
        for source in ("<svg><circle", "<svg><g></svg>", "<div><p>missing", "```html\n<svg></svg>",
                       "prose before <svg></svg>", '<svg><circle id="x" id="y"/></svg>', '<script>alert(1)</script>'):
            with self.subTest(source=source), self.assertRaises(runner.ProbeError):
                runner.sanitize_artifact(source)

    def test_markdown_fence_and_text_escaping(self):
        result = runner.sanitize_artifact('```svg\n<svg><text>&lt;script&gt;</text></svg>\n```')
        self.assertIn("&lt;script&gt;", result)
        self.assertNotIn("<script>", result)


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None):
        self.status = status
        self.body = io.BytesIO(body)
        self.headers = headers or {"Content-Type": "text/event-stream"}
        self.read_count = 0

    def getheaders(self):
        return list(self.headers.items())

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, length):
        self.read_count += 1
        return self.body.read(length)


class RequestTests(unittest.TestCase):
    def request(self, response):
        connection = mock.Mock()
        connection.sock = None
        connection.getresponse.return_value = response
        with mock.patch.object(runner, "_PinnedHTTPSConnection", return_value=connection):
            return runner._request(runner.Endpoint("provider.example", 443, "/v1/responses"),
                                   (socket.AF_INET, ("93.184.216.34", 443)), b"{}", "test-key",
                                   "responses", threading.Event(), time.monotonic() + 5, time.monotonic())

    def test_redirect_rejected_without_reading_body(self):
        response = FakeResponse(b"private key error", 302, {"Location": "http://169.254.169.254/"})
        with self.assertRaises(runner.ProbeError) as raised:
            self.request(response)
        self.assertIn("302", str(raised.exception))
        self.assertNotIn("private", str(raised.exception))
        self.assertEqual(response.read_count, 0)

    def test_http_error_body_never_reflected(self):
        response = FakeResponse(b"test-key is denied upstream", 401)
        with self.assertRaises(runner.ProbeError) as raised:
            self.request(response)
        self.assertNotIn("test-key", str(raised.exception))
        self.assertEqual(response.read_count, 0)

    def test_content_length_and_chunked_body_limit(self):
        response = FakeResponse(b"", headers={"Content-Length": str(runner.MAX_RESPONSE_BYTES + 1)})
        with self.assertRaises(runner.ProbeError):
            self.request(response)
        self.assertEqual(response.read_count, 0)
        response = FakeResponse(b"x" * (runner.MAX_RESPONSE_BYTES + 1), headers={"Content-Type": "application/json"})
        with self.assertRaises(runner.ProbeError):
            self.request(response)
        self.assertGreater(response.read_count, 0)

    def test_compressed_response_rejected(self):
        with self.assertRaises(runner.ProbeError):
            self.request(FakeResponse(b"", headers={"Content-Encoding": "gzip"}))

    def test_cancellation_interrupts_blocked_response_read(self):
        closed = threading.Event()
        cancelled = threading.Event()
        connection = mock.Mock()
        connection.sock = mock.Mock()
        connection.sock.shutdown.side_effect = lambda *_args: closed.set()
        response = FakeResponse()
        def blocked_read(_length):
            # Cancel only after entering the blocking read. A wall-clock timer
            # could fire during TLS/request setup on a slower host instead.
            cancelled.set()
            if not closed.wait(1):
                self.fail("cancel watchdog did not close the socket")
            raise OSError("socket closed")
        response.read1 = blocked_read
        connection.getresponse.return_value = response
        with mock.patch.object(runner, "_PinnedHTTPSConnection", return_value=connection), self.assertRaises(OSError):
            runner._request(runner.Endpoint("provider.example", 443, "/v1/responses"),
                            (socket.AF_INET, ("93.184.216.34", 443)), b"{}", "test-key", "responses",
                            cancelled, time.monotonic() + 5, time.monotonic())
        self.assertTrue(closed.is_set())
        connection.close.assert_called()


class RunProbeTests(unittest.TestCase):
    def spec(self, **changes):
        value = {"base_url": "https://provider.example", "api_key": "test-secret-key", "model": "test-model",
                 "protocol": "chat_completions", "effort": "high", "kind": "reasoning", "preset_id": "candy",
                 "prompt": "Test only", "expected_answer": "21", "max_output_tokens": 128, "timeout_seconds": 30}
        value.update(changes)
        return value

    def test_end_to_end_mock_request_and_scoring(self):
        usage = dict.fromkeys(runner._USAGE_KEYS)
        usage.update(input_tokens=10, output_tokens=20, total_tokens=30)
        with mock.patch.object(runner, "_resolve_public", return_value=(socket.AF_INET, ("93.184.216.34", 443))), \
             mock.patch.object(runner, "_request", return_value=('{"answer":21,"round":9,"star":12}', usage, 0, None)) as request:
            result = runner.run_probe(self.spec(), threading.Event())
        self.assertEqual(result["status"], "passed")
        payload = json.loads(request.call_args.args[2])
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["max_completion_tokens"], 128)
        self.assertEqual(result["total_tokens"], 30)
        self.assertIsNone(result["tokens_per_second"])

    def test_measured_speed_is_not_recomputed_from_request_duration(self):
        usage = dict.fromkeys(runner._USAGE_KEYS)
        usage.update(output_tokens=5000, reasoning_tokens=4990, total_tokens=5268)
        with mock.patch.object(runner, "_resolve_public"), \
             mock.patch.object(runner, "_request", return_value=("OK", usage, 0, 2.5)), \
             mock.patch.object(runner.time, "monotonic", return_value=1):
            result = runner.run_probe(self.spec(preset_id="custom", expected_answer=""), threading.Event())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tokens_per_second"], 2.5)
        self.assertEqual(result["duration_ms"], 0)
        self.assertEqual(result["output_tokens"], 5000)
        self.assertEqual(result["reasoning_tokens"], 4990)
        self.assertEqual(result["total_tokens"], 5268)

    def test_key_echo_redacted_before_persistence(self):
        with mock.patch.object(runner, "_resolve_public"), \
             mock.patch.object(runner, "_request", return_value=("echo test-secret-key", dict.fromkeys(runner._USAGE_KEYS), 1, None)):
            result = runner.run_probe(self.spec(preset_id="custom", expected_answer=""), threading.Event())
        self.assertEqual(result["output_text"], "echo [REDACTED]")
        self.assertNotIn("test-secret-key", json.dumps(result))

    def test_model_effort_limits_and_explicit_payload(self):
        answer = '{"answer":21,"round":9,"star":12}'
        for protocol in ('responses', 'chat_completions'):
            for model, effort, allowed in (
                ('gpt-6.2-sol', 'none', False), ('openai/GPT_6.2_ASTRA', 'minimal', False),
                ('gpt-6.2-astra', 'low', True), ('gpt-6.2-sol', 'max', True),
                ('gpt-6.2-sol', 'default', True), ('gpt-6.1-terra', 'none', True),
                ('another-gpt-compatible-model', 'none', True),
            ):
                with self.subTest(protocol=protocol, model=model, effort=effort), \
                     mock.patch.object(runner, '_resolve_public') as resolve, \
                     mock.patch.object(runner, '_request', return_value=(answer, dict.fromkeys(runner._USAGE_KEYS), 1, None)) as request:
                    result = runner.run_probe(self.spec(model=model, protocol=protocol, effort=effort), threading.Event())
                    if not allowed:
                        self.assertEqual(result['status'], 'error')
                        self.assertEqual(result['error_message'], runner.REASONING_REQUIRED_MESSAGE)
                        resolve.assert_not_called()
                        request.assert_not_called()
                        continue
                    request.assert_called_once()
                    self.assertEqual(result['status'], 'passed')
                    sent = json.loads(request.call_args.args[2])
                    field = 'reasoning' if protocol == 'responses' else 'reasoning_effort'
                    if effort == 'default':
                        self.assertNotIn(field, sent)
                    else:
                        self.assertEqual(sent[field], {'effort': effort} if protocol == 'responses' else effort)

    def test_blocked_model_suffix_is_rejected_even_with_valid_effort(self):
        for model in ('openai/GPT_6.2_ASTRA_NONE', 'gpt-6.1-terra-minimal'):
            for protocol in ('responses', 'chat_completions'):
                with self.subTest(model=model, protocol=protocol), \
                     mock.patch.object(runner, '_resolve_public') as resolve, \
                     mock.patch.object(runner, '_request', return_value=('', dict.fromkeys(runner._USAGE_KEYS), None, None)) as request:
                    result = runner.run_probe(self.spec(model=model, protocol=protocol, effort='low'), threading.Event())
                    self.assertEqual(result['status'], 'error')
                    self.assertIn('后缀', result['error_message'])
                    resolve.assert_not_called()
                    request.assert_not_called()

    def test_missing_defaults_score_question_once_at_low_cost_for_both_protocols(self):
        for protocol in ("responses", "chat_completions"):
            answer = '{"answer":21,"round":9,"star":12}'
            spec = self.spec(protocol=protocol, expected_answer=answer)
            del spec["preset_id"]
            del spec["max_output_tokens"]
            del spec["effort"]
            for returned, status in ((answer, "passed"), ("OK", "failed")):
                with self.subTest(protocol=protocol, answer=returned), mock.patch.object(runner, "_resolve_public"), \
                     mock.patch.object(runner, "_request", return_value=(returned, dict.fromkeys(runner._USAGE_KEYS), 1, None)) as request:
                    result = runner.run_probe(spec, threading.Event())
                self.assertEqual(result["status"], status)
                payload = json.loads(request.call_args.args[2])
                self.assertEqual(payload.get("max_output_tokens", payload.get("max_completion_tokens")), 64)
                self.assertEqual(payload.get("reasoning", {}).get("effort", payload.get("reasoning_effort")), "none")
                if protocol == "responses":
                    self.assertEqual(payload["instructions"], "Follow the test prompt exactly.")
                    self.assertEqual(payload["input"], spec["prompt"])
                else:
                    self.assertNotIn("instructions", payload)
                self.assertEqual(request.call_count, 1)

    def test_encoded_key_echo_removed_from_artifact(self):
        with mock.patch.object(runner, "_resolve_public"), \
             mock.patch.object(runner, "_request", return_value=("<svg><text>test-secret-&#107;ey</text></svg>", dict.fromkeys(runner._USAGE_KEYS), 1, None)):
            result = runner.run_probe(self.spec(kind="drawing"), threading.Event())
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("test-secret-key", result["artifact_html"])

    def test_unpaired_unicode_surrogate_is_not_persisted(self):
        with mock.patch.object(runner, "_resolve_public"), \
             mock.patch.object(runner, "_request", return_value=("\ud800", dict.fromkeys(runner._USAGE_KEYS), 1, None)):
            result = runner.run_probe(self.spec(), threading.Event())
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["output_text"], "")

    def test_errors_never_reflect_exception_or_api_key(self):
        with mock.patch.object(runner, "_resolve_public", side_effect=OSError("test-secret-key private body")):
            result = runner.run_probe(self.spec(), threading.Event())
        self.assertEqual(result["status"], "error")
        self.assertNotIn("test-secret-key", json.dumps(result))

    def test_cancelled_and_invalid_budget_do_not_contact_network(self):
        cancelled = threading.Event()
        cancelled.set()
        with mock.patch.object(runner, "_resolve_public") as resolve:
            self.assertEqual(runner.run_probe(self.spec(), cancelled)["status"], "cancelled")
            for value in (901, float("nan"), -1):
                self.assertEqual(runner.run_probe(self.spec(timeout_seconds=value), threading.Event())["status"], "error")
            resolve.assert_not_called()

    def test_drawing_completed_without_quality_score_and_incomplete_error(self):
        with mock.patch.object(runner, "_resolve_public"), \
             mock.patch.object(runner, "_request", return_value=("<svg><circle r='4'/></svg>", dict.fromkeys(runner._USAGE_KEYS), 1, None)):
            result = runner.run_probe(self.spec(kind="drawing", preset_id="svg"), threading.Event())
        self.assertEqual(result["status"], "completed")
        self.assertIn("Content-Security-Policy", result["artifact_html"])
        with mock.patch.object(runner, "_resolve_public"), \
             mock.patch.object(runner, "_request", return_value=("<svg>", dict.fromkeys(runner._USAGE_KEYS), 1, None)):
            result = runner.run_probe(self.spec(kind="drawing"), threading.Event())
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["artifact_html"])

    def test_custom_drawing_prompt_reaches_each_protocol_and_html_is_sanitized_without_scoring(self):
        prompt = "只输出完整HTML和内联CSS：绘制一只骑自行车的鹈鹕，包含SVG车轮。"
        source = ('<!doctype html><html><body><div style="color:teal" onclick="alert(1)">'
                  'Pelican bicycle<svg viewBox="0 0 400 200"><circle r="8"/></svg></div>'
                  '<script>fetch("https://evil.example/")</script></body></html>')
        for protocol in ("responses", "chat_completions"):
            with self.subTest(protocol=protocol), mock.patch.object(runner, "_resolve_public"), \
                 mock.patch.object(runner, "_request", return_value=(source, dict.fromkeys(runner._USAGE_KEYS), 1, None)) as request, \
                 mock.patch.object(runner, "score_answer", side_effect=AssertionError("drawing must not be scored")) as score:
                result = runner.run_probe(self.spec(protocol=protocol, kind="drawing", preset_id="custom_drawing",
                                                    prompt=prompt, expected_answer="irrelevant"), threading.Event())
            self.assertEqual(result["status"], "completed")
            sent = json.loads(request.call_args.args[2])
            self.assertEqual(sent["input"] if protocol == "responses" else sent["messages"][0]["content"], prompt)
            self.assertEqual(request.call_count, 1)
            score.assert_not_called()
            self.assertIn("Pelican bicycle", result["artifact_html"])
            self.assertIn('viewBox="0 0 400 200"', result["artifact_html"])
            self.assertIn("Content-Security-Policy", result["artifact_html"])
            for forbidden in ("<script", "onclick", "evil.example"):
                self.assertNotIn(forbidden, result["artifact_html"])


if __name__ == "__main__":
    unittest.main()
