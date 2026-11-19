"""Offline behavioral tests; all API keys here are generated test-only strings."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service import LabError, ModelLab
from runner import score_answer


def ctx(user_id=1, role="user", groups=(), rates=None):
    return {"id": user_id, "role": role, "group_ids": list(groups), "group_rates": rates or {}}


def payload(**overrides):
    value = {
        "base_url": "https://relay.example.test/v1", "api_key": "test-only-" + uuid.uuid4().hex,
        "model": "test-model", "protocol": "responses", "effort": "high", "kind": "reasoning",
        "preset_id": "candy", "request_id": str(uuid.uuid4()),
    }
    value.update(overrides)
    return value


def result(**overrides):
    value = {"status": "passed", "output_text": '{"answer":21,"round":9,"star":12}',
             "artifact_html": None, "error_message": None, "duration_ms": 1000}
    value.update(overrides)
    return value


class ServiceTests(unittest.TestCase):
    def test_site_probe_preparation_fixed_origin_and_group_bound(self):
        lab = self.lab()
        value = payload(group_id=1)
        value.pop('api_key')
        value.pop('base_url')
        config, check_effort = lab.prepare_site_probe(value, groups=[{'id': 1, 'name': 'TEST ONLY group'}])
        self.assertTrue(check_effort)
        self.assertEqual(config['base_url'], 'https://gateway.example.com:8443')
        self.assertEqual(config['group_id'], 1)
        self.assertFalse(config['enabled'])
        for field in ('base_url', 'api_key'):
            with self.assertRaises(LabError) as raised:
                lab.prepare_site_probe({**value, field: 'TEST_ONLY'}, groups=[{'id': 1, 'name': 'group'}])
            self.assertEqual(raised.exception.code, 'site_probe_only')
        with self.assertRaises(LabError):
            lab.prepare_site_probe({**value, 'group_id': None}, groups=[])

    def test_preserve_unmodified_paused_effort_and_require_validation_when_reenabled(self):
        lab = self.lab()
        groups = [{'id': 1, 'name': 'TEST ONLY group'}]
        previous = lab.save_probe(payload(group_id=1, model='gpt-6.2-sol', effort='none'), groups=groups)
        config, check_effort = lab.prepare_site_probe({'name': 'Renamed'}, previous['id'], groups)
        self.assertEqual(config['effort'], 'none')
        self.assertFalse(check_effort)
        for change in ({'enabled': True}, {'model': 'gpt-6.2-astra'}, {'effort': 'minimal'}):
            with self.subTest(change=change):
                self.assertTrue(lab.prepare_site_probe(change, previous['id'], groups)[1])

    def test_self_rejects_disabled_effort_before_creating_request(self):
        lab = self.lab(runner=lambda *_: self.fail('unsupported effort queued'))
        for model, effort in (('gpt-6.2-sol', 'none'), ('openai/GPT-6.2-ASTRA', 'minimal'),
                              ('gpt-6.1-sol', 'none'), ('gpt-6.1-terra', 'minimal'),
                              ('openai/GPT_6.2_SOL_NONE', 'low'), ('gpt-6.1-terra-minimal', 'default')):
            with self.subTest(model=model, effort=effort):
                value = payload(model=model, effort=effort)
                self.error(400, 'effort_not_supported', lab.start_self, 1, value)
                self.error(404, 'request_not_found', lab.get_request, value['request_id'], ctx())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="model-lab-service-test-")
        self.directory = Path(self.temp.name)
        self.key_path = self.directory / "master.key"
        self.key_path.write_bytes(Fernet.generate_key())
        self.labs = []
        self.events = []

    def tearDown(self):
        for event in self.events:
            event.set()
        for lab in self.labs:
            lab.close()
        self.temp.cleanup()

    def lab(self, runner=None, name="lab"):
        value = ModelLab(self.directory / f"{name}.sqlite3", self.key_path,
                         runner_fn=runner or (lambda spec, cancel: result()), site_origin='https://gateway.example.com:8443')
        self.labs.append(value)
        return value

    def blocking_runner(self):
        release, started = threading.Event(), threading.Event()
        self.events.append(release)
        calls = []
        lock = threading.Lock()

        def run(spec, cancel):
            with lock:
                calls.append(spec["model"])
            started.set()
            while not release.wait(0.01):
                if cancel.is_set():
                    return result(status="cancelled")
            return result()
        return run, release, started, calls

    def finished(self, lab, run_id, identity=None):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with lab._lock:
                detail = lab.get_run(run_id, identity or ctx())
                active = run_id in lab._tasks
            if detail["status"] not in ("queued", "running") and not active:
                return detail
            time.sleep(0.01)
        self.fail("task did not settle")

    def error(self, status, code, fn, *args, **kwargs):
        with self.assertRaises(LabError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.status, status)
        self.assertEqual(caught.exception.code, code)

    def copy_record(self, lab, source, **changes):
        record = {key: value for key, value in dict(source).items() if key != "has_artifact"}
        record.update(id=str(uuid.uuid4()), request_id=str(uuid.uuid4()))
        record.update(changes)
        columns = list(record)
        with lab._lock, lab._db:
            lab._db.execute(f"INSERT INTO runs({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                            [record[name] for name in columns])
        return record["id"]

    def test_requires_existing_valid_key_and_wal(self):
        missing = self.directory / "missing.key"
        self.error(500, "encryption_unavailable", ModelLab,
                   self.directory / "missing.sqlite3", missing, runner_fn=lambda *_: result(), site_origin='https://gateway.example.com')
        missing.write_bytes(b"invalid-test-value")
        self.error(500, "encryption_unavailable", ModelLab,
                   self.directory / "invalid.sqlite3", missing, runner_fn=lambda *_: result(), site_origin='https://gateway.example.com')
        self.assertFalse((self.directory / "invalid.sqlite3").exists())
        lab = self.lab()
        self.assertEqual(lab._db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_presets_are_independent_and_have_verifiable_candy_contract(self):
        lab = self.lab()
        presets = lab.bootstrap_presets()
        by_id = {item["id"]: item for item in presets}
        self.assertEqual([item["id"] for item in presets], ["candy", "svg", "custom_drawing", "custom"])
        self.assertEqual(json.loads(by_id["candy"]["expected_answer"]), {"answer": 21, "round": 9, "star": 12})
        self.assertIn("摸出形状", by_id["candy"]["prompt"])
        self.assertIn("JSON", by_id["candy"]["prompt"])
        by_id["candy"]["prompt"] = "changed by caller"
        fresh = next(item for item in lab.bootstrap_presets() if item["id"] == "candy")
        self.assertNotEqual(fresh["prompt"], by_id["candy"]["prompt"])
        self.assertEqual(by_id["svg"]["prompt"], "Generate an SVG of a pelican riding a bicycle")
        self.assertEqual(by_id["custom_drawing"]["kind"], "drawing")
        self.assertEqual(by_id["custom_drawing"]["prompt"], "")

    def test_candy_prompt_matches_strict_runner_json_contract(self):
        lab = self.lab()
        candy = next(item for item in lab.bootstrap_presets() if item["id"] == "candy")
        self.assertIn("只输出一个JSON对象", candy["prompt"])
        self.assertIn("不加空格或换行", candy["prompt"])
        self.assertIn("不要输出说明", candy["prompt"])
        self.assertNotIn("说明最少性的理由", candy["prompt"])
        self.assertNotIn("最后一行", candy["prompt"])
        self.assertEqual(score_answer(candy["expected_answer"], "candy", candy["expected_answer"]), "passed")
        self.assertEqual(score_answer("先说明理由\n" + candy["expected_answer"], "candy", candy["expected_answer"]), "failed")

    def test_pelican_default_reaches_self_and_saved_probe_runners_unchanged(self):
        captured = []

        def runner(spec, cancel):
            captured.append((spec["preset_id"], spec["kind"], spec["prompt"], spec["expected_answer"]))
            return result(status="completed", output_text="<svg></svg>", artifact_html="<svg></svg>")

        lab = self.lab(runner)
        drawing = payload(kind="drawing", preset_id="svg")
        saved = lab.save_probe(drawing)
        expected_prompt = "Generate an SVG of a pelican riding a bicycle"
        self.assertEqual(saved["prompt"], expected_prompt)
        self_run = lab.start_self(1, drawing)
        self.assertEqual(self.finished(lab, self_run["id"])["status"], "completed")
        probe_run = lab.start_probe(1, saved["id"], str(uuid.uuid4()))
        self.assertEqual(self.finished(lab, probe_run["id"])["status"], "completed")
        self.assertEqual(captured, [("svg", "drawing", expected_prompt, None)] * 2)

    def test_custom_drawing_requires_prompt_before_save_or_run(self):
        calls = []
        lab = self.lab(lambda *_: calls.append(True) or result())
        for change in ({}, {"prompt": ""}, {"prompt": "  \t  "}):
            with self.subTest(change=change):
                drawing = payload(kind="drawing", preset_id="custom_drawing", **change)
                self.error(400, "invalid_input", lab.save_probe, drawing)
                self.error(400, "invalid_input", lab.start_self, 1, drawing)
        self.assertEqual(lab.list_probes()["probes"], [])
        self.assertEqual(calls, [])

    def test_custom_drawing_html_prompt_is_saved_and_reaches_runner_without_grading(self):
        captured = []
        prompt = "只输出完整HTML和内联CSS：绘制一只骑自行车的鹈鹕，包含SVG车轮。"

        def runner(spec, cancel):
            captured.append((spec["preset_id"], spec["prompt"], spec["expected_answer"]))
            return result(status="completed", output_text="<div>drawing</div>", artifact_html="<div>drawing</div>")

        lab = self.lab(runner)
        drawing = payload(kind="drawing", preset_id="custom_drawing", prompt=prompt,
                          expected_answer="drawing tasks are never graded")
        saved = lab.save_probe(drawing)
        self.assertEqual(saved["prompt"], prompt)
        self.assertIsNone(saved["expected_answer"])
        probe_run = lab.start_probe(1, saved["id"], str(uuid.uuid4()))
        self.assertEqual(self.finished(lab, probe_run["id"])["status"], "completed")
        self_run = lab.start_self(1, drawing)
        self.assertEqual(self.finished(lab, self_run["id"])["status"], "completed")
        self.assertEqual(captured, [("custom_drawing", prompt, None)] * 2)

    def test_existing_svg_prompt_survives_an_unrelated_edit_after_default_changes(self):
        captured = []
        old_prompt = "只输出精简SVG：一个紫色行星和一圈缓慢转动的轨道。"
        lab = self.lab(lambda spec, _: captured.append(spec["prompt"]) or
                       result(status="completed", output_text="<svg></svg>", artifact_html="<svg></svg>"))
        saved = lab.save_probe(payload(kind="drawing", preset_id="svg", prompt=old_prompt))
        edited = lab.save_probe({"name": "Existing drawing renamed"}, saved["id"])
        self.assertEqual(edited["prompt"], old_prompt)
        run = lab.start_probe(1, saved["id"], str(uuid.uuid4()))
        self.assertEqual(self.finished(lab, run["id"])["status"], "completed")
        self.assertEqual(captured, [old_prompt])
        fresh = lab.save_probe(payload(kind="drawing", preset_id="svg"))
        self.assertEqual(fresh["prompt"], "Generate an SVG of a pelican riding a bicycle")

    def test_drawing_and_reasoning_presets_cannot_be_used_for_the_other_kind(self):
        lab = self.lab()
        for kind, preset in (("reasoning", "svg"), ("reasoning", "custom_drawing"),
                             ("drawing", "candy"), ("drawing", "custom")):
            with self.subTest(kind=kind, preset=preset):
                value = payload(kind=kind, preset_id=preset, prompt="Test only prompt")
                self.error(400, "invalid_preset", lab.save_probe, value)
                self.error(400, "invalid_preset", lab.start_self, 1, value)

    def test_low_cost_question_defaults_reach_runner_and_admin_can_raise_budget(self):
        captured = []

        def runner(spec, cancel):
            captured.append({name: spec[name] for name in (
                "preset_id", "prompt", "expected_answer", "effort", "max_output_tokens",
            )})
            answer = '{"answer":21,"round":9,"star":12}'
            return result(status=score_answer(answer, spec["preset_id"], spec["expected_answer"]), output_text=answer)

        lab = self.lab(runner)
        minimal = {name: value for name, value in payload().items()
                   if name in {"base_url", "api_key", "model", "request_id"}}
        probe = lab.save_probe(minimal)
        self.assertEqual(probe["preset_id"], "candy")
        self.assertEqual(probe["effort"], "none")
        self.assertEqual(probe["max_output_tokens"], 64)
        self.assertFalse(probe["enabled"])
        run = lab.start_self(1, minimal)
        self.assertEqual(self.finished(lab, run["id"])["status"], "passed")
        candy = lab.bootstrap_presets()[0]
        self.assertEqual(captured, [{"preset_id": "candy", "prompt": candy["prompt"],
                                    "expected_answer": '{"answer":21,"round":9,"star":12}',
                                    "effort": "none", "max_output_tokens": 64}])
        raised = lab.save_probe({"max_output_tokens": 512, "effort": "low"}, probe["id"])
        self.assertEqual(raised["max_output_tokens"], 512)
        self.assertEqual(raised["effort"], "low")

    def test_question_default_scores_the_answer_and_locks_fixed_fields(self):
        lab = self.lab()
        candy = lab.bootstrap_presets()[0]
        self.assertEqual(candy["id"], "candy")
        self.assertEqual(score_answer(candy["expected_answer"], "candy", candy["expected_answer"]), "passed")
        for answer in ("OK", "21", '{"answer":21,"round":12,"star":9}'):
            self.assertEqual(score_answer(answer, "candy", candy["expected_answer"]), "failed")
        for change in ({"prompt": "What is 2+2?"}, {"expected_answer": "4"}, {"expected_answer": None}):
            self.error(400, "preset_locked", lab.start_self, 1, payload(**change))
            self.error(400, "preset_locked", lab.save_probe, payload(**change))

    def test_admin_key_and_active_schedule_survive_metadata_edit(self):
        lab = self.lab()
        value = payload(name="模型A", enabled=True, interval_minutes=5)
        before = time.time()
        saved = lab.save_probe(value)
        self.assertTrue(saved["has_key"])
        self.assertNotIn("api_key", saved)
        self.assertNotIn(value["api_key"], json.dumps(saved))
        row = lab._probe_row(saved["id"])
        self.assertGreaterEqual(row["next_run_at"], before + 300)
        ciphertext = row["key_ciphertext"]
        self.assertNotEqual(ciphertext, value["api_key"].encode())
        self.assertEqual(Fernet(self.key_path.read_bytes()).decrypt(ciphertext).decode(), value["api_key"])
        edited = lab.save_probe({"name": "改名", "api_key": ""}, saved["id"])
        self.assertTrue(edited["enabled"])
        self.assertIsNotNone(edited["next_run_at"])
        self.assertTrue(edited["has_key"])
        self.assertEqual(lab._probe_row(saved["id"])["key_ciphertext"], ciphertext)
        self.assertNotIn(value["api_key"], json.dumps(lab.list_probes()))
        for file in self.directory.glob("*.sqlite3*"):
            self.assertNotIn(value["api_key"].encode(), file.read_bytes())

    def test_cannot_enable_without_key_and_rejects_key_in_other_fields(self):
        lab = self.lab()
        value = payload(api_key="", enabled=True)
        self.error(400, "missing_key", lab.save_probe, value)
        value = payload(preset_id="custom")
        value["prompt"] = "do not store " + value["api_key"]
        self.error(400, "key_in_configuration", lab.save_probe, value)

    def test_group_is_verified_and_name_is_server_derived(self):
        lab = self.lab()
        value = payload(group_id=2, group_name="forged")
        self.error(400, "invalid_group", lab.save_probe, value, groups=[{"id": 1, "name": "Other"}])
        saved = lab.save_probe(value, groups=[{"id": 2, "name": "真实分组"}])
        self.assertEqual(saved["group_name"], "真实分组")

    def test_invalid_urls_and_protocols_are_rejected_before_runner(self):
        lab = self.lab()
        for address in ("http://relay.example.test", "https://u:p@relay.example.test",
                        "https://relay.example.test/?key=secret", "https://relay.example.test/#token",
                        "https://127.0.0.1", "https://[::1]", "https://169.254.169.254", "https://localhost",
                        "https://relay.example.test\\@127.0.0.1", "https://relay.example.test:99999"):
            with self.subTest(address=address):
                self.error(400, "invalid_url", lab.start_self, 1, payload(base_url=address))
        self.error(400, "invalid_protocol", lab.start_self, 1, payload(protocol=[]))
        self.error(400, "invalid_effort", lab.start_self, 1, payload(effort="auto"))
        self.error(400, "invalid_input", lab.start_self, 1, payload(max_output_tokens=True))

    def test_self_key_stays_only_in_memory_and_echo_is_redacted(self):
        value = payload(publish=True, preset_id="custom")
        secret = value["api_key"]
        value["prompt"] = "test echo " + secret
        value["model"] = "test-model-" + secret

        def runner(spec, cancel):
            self.assertEqual(spec["api_key"], secret)
            return result(output_text="echo " + secret, artifact_html="<p>" + secret + "</p>")
        lab = self.lab(runner)
        run = lab.start_self(1, value)
        detail = self.finished(lab, run["id"])
        self.assertNotIn(secret, json.dumps(detail))
        self.assertNotIn(secret, json.dumps(lab.list_runs(ctx(), "mine")))
        self.assertNotIn("user_id", detail)
        for file in self.directory.glob("*.sqlite3*"):
            self.assertNotIn(secret.encode(), file.read_bytes())

    def test_raw_upstream_exception_and_error_body_are_not_exposed(self):
        secret = "test-only-error-" + uuid.uuid4().hex
        for mode in ("raise", "result"):
            def runner(spec, cancel):
                if mode == "raise":
                    raise RuntimeError("Authorization: Bearer " + secret)
                return result(status="error", error_message="Authorization: Bearer " + secret)
            lab = self.lab(runner, name=mode)
            run = lab.start_self(1, payload(api_key=secret))
            detail = self.finished(lab, run["id"])
            self.assertEqual(detail["status"], "error")
            self.assertNotIn(secret, json.dumps(detail))
            self.assertIsNotNone(detail["error_message"])

    def test_known_runner_diagnostics_remain_useful_and_unknown_bodies_are_hidden(self):
        for number, message in enumerate(("上游 TLS 证书校验失败。", "请求超过时间限制；未自动重试。",
                                           "上游接口返回 HTTP 401；未自动重试。",
                                           "上游流已中断，未收到完整结束标记。")):
            lab = self.lab(lambda *_, message=message: result(status="error", error_message=message), name=str(number))
            run = lab.start_self(1, payload())
            self.assertEqual(self.finished(lab, run["id"])["error_message"], message)

    def test_custom_preset_requires_prompt_and_candy_cannot_be_repurposed(self):
        lab = self.lab()
        self.error(400, "invalid_input", lab.start_self, 1, payload(preset_id="custom"))
        self.error(400, "preset_locked", lab.start_self, 1, payload(prompt="What is 2+2?"))
        self.error(400, "preset_locked", lab.save_probe, payload(expected_answer="4"))
        probe = lab.save_probe(payload())
        edited = lab.save_probe({"preset_id": "custom", "prompt": "What is 2+2?"}, probe["id"])
        self.assertIsNone(edited["expected_answer"])
        self.assertEqual(edited["prompt"], "What is 2+2?")
        self.assertEqual(edited["preset_id"], "custom")

    def test_concurrent_same_request_is_idempotent_and_user_limit_is_enforced(self):
        runner, release, started, calls = self.blocking_runner()
        lab = self.lab(runner)
        value = payload()
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: lab.start_self(1, value), range(8)))
        self.assertEqual(len({item["id"] for item in responses}), 1)
        self.assertTrue(started.wait(1))
        self.error(429, "user_busy", lab.start_self, 1, payload())
        release.set()
        self.finished(lab, responses[0]["id"])
        same = lab.start_self(1, {"request_id": value["request_id"]})
        self.assertEqual(same["id"], responses[0]["id"])
        self.assertEqual(len(calls), 1)

    def test_same_request_id_is_scoped_to_user(self):
        lab = self.lab()
        value = payload()
        first = lab.start_self(1, value)
        second = lab.start_self(2, value)
        self.assertNotEqual(first["id"], second["id"])
        self.finished(lab, first["id"])
        self.finished(lab, second["id"], ctx(2))

    def test_admin_run_concurrent_receipt_and_probe_overlap_are_bounded(self):
        runner, release, started, calls = self.blocking_runner()
        lab = self.lab(runner)
        probe = lab.save_probe(payload(name="TEST ONLY admin probe"))
        request_id = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: lab.start_probe(1, probe["id"], request_id), range(8)))
        self.assertEqual(len({item["id"] for item in responses}), 1)
        self.assertTrue(started.wait(1))
        self.error(409, "probe_busy", lab.start_probe, 2, probe["id"], str(uuid.uuid4()))
        self.assertEqual(lab.get_request(request_id, ctx())["id"], responses[0]["id"])
        release.set()
        self.finished(lab, responses[0]["id"])
        self.assertEqual(lab.start_probe(1, probe["id"], request_id)["id"], responses[0]["id"])
        self.assertEqual(len(calls), 1)

    def test_same_admin_can_run_distinct_probes_and_self_without_unlocking_self_early(self):
        releases = {name: threading.Event() for name in ("self", "probe-a", "probe-b")}
        starts = {name: threading.Event() for name in releases}
        self.events.extend(releases.values())

        def runner(spec, cancel):
            name = spec["model"]
            starts[name].set()
            while not releases[name].wait(0.01):
                if cancel.is_set():
                    return result(status="cancelled")
            return result()

        lab = self.lab(runner)
        probes = [lab.save_probe(payload(model=name)) for name in ("probe-a", "probe-b")]
        own = lab.start_self(1, payload(model="self"))
        launched = [lab.start_probe(1, probe["id"], str(uuid.uuid4())) for probe in probes]
        self.assertTrue(all(event.wait(1) for event in starts.values()))
        self.error(429, "user_busy", lab.start_self, 1, payload())
        self.error(409, "probe_busy", lab.start_probe, 1, probes[0]["id"], str(uuid.uuid4()))
        releases["probe-a"].set()
        self.finished(lab, launched[0]["id"])
        # Completing a manual probe must not release an unrelated self-run lock.
        self.error(429, "user_busy", lab.start_self, 1, payload())
        releases["self"].set()
        self.finished(lab, own["id"])
        releases["probe-b"].set()
        self.finished(lab, launched[1]["id"])

    def test_due_probes_start_concurrently_and_manual_run_does_not_block_other_schedule(self):
        runner, release, started, calls = self.blocking_runner()
        lab = self.lab(runner)
        probes = [lab.save_probe(payload(model=f"scheduled-{i}", enabled=True)) for i in range(3)]
        manual = lab.start_probe(1, probes[0]["id"], str(uuid.uuid4()))
        with lab._lock, lab._db:
            lab._db.execute("UPDATE probes SET next_run_at=?", (time.time() - 1,))
        lab._scheduler_tick()
        deadline = time.monotonic() + 2
        while len(calls) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertCountEqual(calls, ["scheduled-0", "scheduled-1", "scheduled-2"])
        self.assertEqual(len(lab._tasks), 3)
        release.set()
        self.finished(lab, manual["id"])

    def test_channel_metrics_keep_all_day_samples_while_full_history_keeps_latest_sixty(self):
        sequence = []

        def runner(*_):
            index = len(sequence)
            sequence.append(index)
            return result(status="failed" if index < 5 else "passed", input_tokens=10,
                          output_tokens=5, cached_tokens=2, total_tokens=15, ttft_ms=100,
                          duration_ms=100 if index < 5 else 200)

        lab = self.lab(runner)
        probe = lab.save_probe(payload(publish=True))
        first = None
        for _ in range(65):
            run = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
            first = first or run
            self.finished(lab, run["id"])
        overview = lab.dashboard(ctx(1, "admin"))
        model = overview["groups"][0]["models"][0]
        self.assertEqual(len(model["history"]), 60)
        self.assertEqual(model["stats"]["pass_rate"], 100)
        self.assertEqual(overview["summary"]["runs"], 65)
        self.assertEqual(overview["summary"]["scored"], 65)
        self.assertAlmostEqual(overview["summary"]["pass_rate"], 6000 / 65)
        self.assertEqual(overview["summary"]["total_tokens"], 65 * 15)
        self.assertAlmostEqual(overview["summary"]["avg_duration_ms"], (5 * 100 + 60 * 200) / 65)
        self.assertNotIn("window_stats", model)
        self.error(404, "run_not_found", lab.get_run, first["id"], ctx())
        self.assertEqual(lab._db.execute("SELECT count(*) FROM channel_metrics").fetchone()[0], 65)

    def test_metrics_backfill_is_idempotent_compact_and_only_covers_reasoning_probes(self):
        lab = self.lab()
        probe = lab.save_probe(payload(publish=True))
        drawing = lab.save_probe(payload(kind="drawing", preset_id="svg", publish=True))
        run = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
        self.finished(lab, run["id"])
        for run in (lab.start_self(1, payload()), lab.start_probe(1, drawing["id"], str(uuid.uuid4()))):
            self.finished(lab, run["id"])
        # Simulate the pre-ledger database; no business rows are restored or replaced.
        lab.close()
        old_db = sqlite3.connect(str(self.directory / "lab.sqlite3"))
        try:
            old_db.execute("DROP TABLE channel_metrics")
            old_db.commit()
        finally:
            old_db.close()
        reopened = self.lab()
        self.assertEqual(reopened._db.execute("SELECT count(*) FROM channel_metrics").fetchone()[0], 1)
        fields = {row[1] for row in reopened._db.execute("PRAGMA table_info(channel_metrics)")}
        self.assertEqual(fields, {"run_id", "probe_id", "created_at", "public", "group_id", "status",
                                  "duration_ms", "ttft_ms", "input_tokens", "output_tokens", "cached_tokens",
                                  "reasoning_tokens", "total_tokens"})
        reopened.close()
        again = self.lab()
        self.assertEqual(again._db.execute("SELECT count(*) FROM channel_metrics").fetchone()[0], 1)
        self.assertEqual(again.dashboard(ctx(1, "admin"))["summary"]["runs"], 1)

    def test_terminal_receipt_full_result_and_metric_become_visible_in_one_commit(self):
        runner, release, started, _ = self.blocking_runner()
        lab = self.lab(runner)
        probe = lab.save_probe(payload(publish=True))
        request_id = str(uuid.uuid4())
        run = lab.start_probe(1, probe["id"], request_id)
        self.assertTrue(started.wait(1))
        # A second SQLite reader sees the previous committed state even after
        # the writer has already changed the full run and receipt in its tx.
        reader = sqlite3.connect(str(self.directory / "lab.sqlite3"), check_same_thread=False)
        observed = []

        def inspect_write(statement):
            if statement.startswith("INSERT INTO channel_metrics"):
                observed.append((
                    lab._db.in_transaction,
                    reader.execute("SELECT status FROM runs WHERE id=?", (run["id"],)).fetchone()[0],
                    reader.execute("SELECT status FROM requests WHERE request_id=?", (request_id,)).fetchone()[0],
                    reader.execute("SELECT count(*) FROM channel_metrics WHERE run_id=?", (run["id"],)).fetchone()[0],
                ))

        try:
            with lab._lock:
                lab._db.set_trace_callback(inspect_write)
            release.set()
            self.finished(lab, run["id"])
            self.assertEqual(observed, [(True, "running", "running", 0)])
            self.assertEqual(reader.execute("SELECT status FROM runs WHERE id=?", (run["id"],)).fetchone()[0], "passed")
            self.assertEqual(reader.execute("SELECT status FROM requests WHERE request_id=?", (request_id,)).fetchone()[0], "passed")
            self.assertEqual(reader.execute("SELECT count(*) FROM channel_metrics WHERE run_id=?", (run["id"],)).fetchone()[0], 1)
            self.assertEqual(lab.start_probe(1, probe["id"], request_id)["id"], run["id"])
            self.assertEqual(reader.execute("SELECT count(*) FROM channel_metrics WHERE run_id=?", (run["id"],)).fetchone()[0], 1)
        finally:
            with lab._lock:
                lab._db.set_trace_callback(None)
            reader.close()

    def test_metrics_preserve_original_public_and_group_authorization_after_probe_changes(self):
        lab = self.lab()
        available = [{"id": 4, "name": "Original"}, {"id": 5, "name": "Current"}]
        probe = lab.save_probe(payload(group_id=4, publish=False), groups=available)
        private = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
        self.finished(lab, private["id"])
        lab.save_probe({"publish": True}, probe["id"], groups=available)
        original_group = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
        self.finished(lab, original_group["id"])
        self.error(409, 'probe_identity_locked', lab.save_probe, {'group_id': 5}, probe['id'], groups=available)
        # Simulate a pre-lock legacy row that had already been moved; historical visibility still matters.
        with lab._lock, lab._db:
            lab._db.execute("UPDATE probes SET config_json=json_set(config_json,'$.group_id',5) WHERE id=?", (probe['id'],))
        current = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
        self.finished(lab, current["id"])
        self.assertEqual(lab.dashboard(ctx(1, "admin"))["summary"]["runs"], 3)
        self.assertEqual(lab.dashboard(ctx(2, groups=[5]))["summary"]["runs"], 1)
        self.assertEqual(lab.dashboard(ctx(2, groups=[4, 5]))["summary"]["runs"], 2)
        lab.delete_probe(probe["id"])
        self.assertEqual(lab.dashboard(ctx(1, "admin"))["summary"]["runs"], 0)

    def test_lock_survives_unpublish_pause_and_history_pruning(self):
        lab = self.lab()
        groups = [{'id': 1, 'name': 'Group'}, {'id': 2, 'name': 'Other'}]
        def no_catalog(*args):
            self.fail('a metadata edit or locked change must never provision a key')
        for trigger in ('publish', 'enabled', 'manual'):
            with self.subTest(trigger=trigger):
                probe = lab.save_probe(payload(group_id=1, kind='reasoning', preset_id='custom',
                                                prompt='Q1', expected_answer='A1'), groups=groups)
                pid = probe['id']
                self.assertFalse(probe['identity_locked'])
                self.assertEqual(probe['name'], 'Group')
                lab.save_probe({'model': 'model-6.1'}, pid, groups)
                if trigger == 'manual':
                    run = lab.start_probe(1, pid, str(uuid.uuid4()))
                    self.finished(lab, run['id'])
                else:
                    lab.save_probe({trigger: True}, pid, groups)
                lab.save_probe({'enabled': False, 'publish': False}, pid, groups)
                with lab._lock, lab._db:
                    lab._prune_locked(time.time() + 35 * 3600)
                for key, value in dict(group_id=2, model='model-6.2', kind='drawing',
                                       preset_id='candy', prompt='Q2', expected_answer='A2').items():
                    self.error(409, 'probe_identity_locked', lab.save_site_probe,
                               {key: value, 'identity_locked': False}, pid, groups, no_catalog, lambda group: self.assertEqual(group, 1))
                updated = lab.save_site_probe({'interval_minutes': 15}, pid, groups, no_catalog, lambda group: self.assertEqual(group, 1))
                self.assertEqual((updated['model'], updated['interval_minutes'], updated['identity_locked']),
                                 ('model-6.1', 15, True))
                self.assertEqual(lab._db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)

    def test_legacy_unproven_draft_locks_on_upgrade_and_restart(self):
        lab = self.lab()
        probe = lab.save_probe(payload())
        lab.close()
        with closing(sqlite3.connect(self.directory / 'lab.sqlite3')) as db, db:
            # Reproduce the deployed pre-lock schema, including rows with expired history.
            db.execute('ALTER TABLE probes DROP COLUMN identity_locked')
        upgraded = self.lab()
        self.assertTrue(upgraded.list_probes()['probes'][0]['identity_locked'])
        self.error(409, 'probe_identity_locked', upgraded.save_probe, {'model': 'replacement'}, probe['id'])
        upgraded.close()
        reopened = self.lab()
        self.assertTrue(reopened.list_probes()['probes'][0]['identity_locked'])
        self.assertFalse(reopened.save_probe(payload())['identity_locked'])

    def test_retiring_unlisted_model_only_removes_its_samples_from_rates(self):
        lab = self.lab(lambda spec, _: result(status='failed' if spec['model'] == 'model-6.1' else 'passed'))
        groups = [{'id': 3, 'name': 'Same name'}]
        old, new = [lab.save_probe(payload(group_id=3, model=model, publish=True, enabled=True), groups=groups)
                    for model in ('model-6.1', 'model-6.2')]
        self.assertEqual(old['name'], new['name'])
        for probe in (old, new):
            run = lab.start_probe(1, probe['id'], str(uuid.uuid4()))
            self.finished(lab, run['id'])
        def retired_catalog(*args):
            self.fail('retired model unexpectedly required the model catalog')
        adjusted = lab.save_site_probe({'interval_minutes': 35}, old['id'], groups, retired_catalog, lambda group: self.assertEqual(group, 3))
        self.assertTrue(adjusted['enabled'])
        self.assertGreater(lab._probe_row(old['id'])['next_run_at'], time.time() + 2000)
        viewer = ctx(2, groups=[3])
        self.assertEqual(lab.dashboard(viewer)['summary']['pass_rate'], 50)
        lab.delete_probe(old['id'])
        dashboard = lab.dashboard(viewer)
        self.assertEqual(dashboard['summary']['pass_rate'], 100)
        models = dashboard['groups'][0]['models']
        self.assertEqual([(m['probe_id'], m['model'], len(m['history'])) for m in models],
                         [(new['id'], 'model-6.2', 1)])
        self.assertEqual(dashboard['groups'][0]['stats']['runs'], 1)

    def test_update_blocks_only_new_admissions_and_due_schedule_resumes_when_switch_finishes(self):
        release, started = threading.Event(), threading.Event()
        self.events.append(release)
        def runner(*args):
            started.set()
            release.wait(4)
            return result()
        lab = self.lab(runner)
        gate = self.directory / 'maintenance'
        lab._update_gate = gate
        data = payload()
        receipt = lab.start_self(1, data)
        self.assertTrue(started.wait(2))
        lab.cancel_run(receipt['id'], ctx())
        gate.write_text('release-identity', encoding='ascii')
        self.assertEqual(lab.start_self(1, data)['id'], receipt['id'])
        self.error(503, 'update_in_progress', lab.start_self, 2, payload())
        probe = lab.save_probe(payload(enabled=True))
        self.error(503, 'update_in_progress', lab.start_probe, 1, probe['id'], str(uuid.uuid4()))
        with lab._lock, lab._db:
            lab._db.execute('UPDATE probes SET next_run_at=0')
        lab._scheduler_tick()
        self.assertEqual(lab._db.execute('SELECT count(*) FROM runs').fetchone()[0], 1)
        release.set()
        self.finished(lab, receipt['id'])
        self.assertTrue(lab.list_probes()['probes'][0]['enabled'])
        gate.unlink()
        lab._scheduler_tick()
        self.assertEqual(lab._db.execute('SELECT count(*) FROM runs WHERE probe_id=?', (probe['id'],)).fetchone()[0], 1)
        lab._scheduler_tick()
        self.assertEqual(lab._db.execute('SELECT count(*) FROM runs WHERE probe_id=?', (probe['id'],)).fetchone()[0], 1)

    def test_metrics_expire_after_thirty_hours_independently_of_full_records(self):
        lab = self.lab()
        probe = lab.save_probe(payload(publish=True))
        first = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
        self.finished(lab, first["id"])
        second = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
        self.finished(lab, second["id"])
        now = time.time()
        with lab._lock, lab._db:
            lab._db.execute("UPDATE channel_metrics SET created_at=? WHERE run_id=?", (now - 31 * 3600, first["id"]))
            lab._db.execute("UPDATE channel_metrics SET created_at=? WHERE run_id=?", (now - 29 * 3600, second["id"]))
            lab._prune_locked(now)
        retained = list(lab._db.execute("SELECT run_id FROM channel_metrics"))
        self.assertEqual([row[0] for row in retained], [second["id"]])
        self.assertEqual(lab.dashboard(ctx(1, "admin"))["summary"]["runs"], 0)

    def test_group_summaries_and_channel_rates_exclude_self_and_all_drawing_usage(self):
        responses = iter([
            result(status="passed", total_tokens=10, duration_ms=100, ttft_ms=10),
            result(status="failed", total_tokens=20, duration_ms=200, ttft_ms=20),
            result(status="error", total_tokens=30, duration_ms=300),
            result(status="completed", total_tokens=40, duration_ms=400, ttft_ms=40),
            result(status="cancelled", total_tokens=50, duration_ms=500),
            result(status="passed", total_tokens=999999, duration_ms=999999, ttft_ms=999999),
            result(status="completed", total_tokens=999999, duration_ms=999999, ttft_ms=999999,
                   artifact_html="<svg></svg>"),
        ])
        lab = self.lab(lambda *_: next(responses))
        available = [{"id": 1, "name": "One"}, {"id": 2, "name": "Two"}]
        probes = [lab.save_probe(payload(group_id=group, publish=True), groups=available) for group in (1, 2)]
        for probe in (probes[0], probes[1], probes[0], probes[0], probes[0]):
            run = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
            self.finished(lab, run["id"])
        own = lab.start_self(1, payload(publish=True))
        self.finished(lab, own["id"])
        drawing = lab.save_probe(payload(kind="drawing", preset_id="svg", publish=True, group_id=1), groups=available)
        picture = lab.start_probe(1, drawing["id"], str(uuid.uuid4()))
        self.finished(lab, picture["id"])
        dashboard = lab.dashboard(ctx(2, groups=[1, 2]))
        one, two = sorted(dashboard["groups"], key=lambda item: item["id"])
        self.assertEqual(dashboard["summary"]["runs"], 5)
        self.assertEqual(dashboard["summary"]["scored"], 2)
        self.assertEqual(dashboard["summary"]["pass_rate"], 50)
        self.assertEqual(dashboard["summary"]["total_tokens"], 150)
        self.assertEqual(dashboard["summary"]["avg_duration_ms"], 300)
        self.assertAlmostEqual(one["stats"]["success_rate"], 200 / 3)
        self.assertEqual(one["summary"]["pass_rate"], 100)
        self.assertEqual(one["summary"]["total_tokens"], 130)
        self.assertEqual(two["summary"]["pass_rate"], 0)
        self.assertEqual(two["stats"]["success_rate"], 100)
        self.assertEqual(lab._db.execute("SELECT count(*) FROM channel_metrics").fetchone()[0], 5)

    def test_each_drawing_probe_exposes_sixty_rounds_newest_first_and_no_statistics(self):
        lab = self.lab(lambda *_: result(status="completed", artifact_html="<svg></svg>"))
        probes = [lab.save_probe(payload(kind="drawing", preset_id="svg", publish=True)) for _ in range(2)]
        newest_ids = {}
        for probe in probes:
            run = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
            self.finished(lab, run["id"])
            source = lab._run_row(run["id"])
            now = time.time()
            for index in range(64):
                newest = self.copy_record(lab, source, created_at=now + index)
            newest_ids[probe["id"]] = newest
        with lab._lock, lab._db:
            lab._prune_locked(time.time())
        dashboard = lab.dashboard(ctx(1, "admin"))
        group = dashboard["groups"][0]
        self.assertEqual(group["models"], [])
        self.assertNotIn("artworks", group)
        self.assertEqual(dashboard["summary"]["runs"], 0)
        self.assertEqual(len(group["drawing_models"]), 2)
        for model in group["drawing_models"]:
            newest = newest_ids[model["probe_id"]]
            self.assertEqual(len(model["history"]), 60)
            self.assertEqual(model["history"][0]["id"], newest)
            self.assertTrue(all(item["has_artifact"] for item in model["history"]))
            self.assertEqual(model["history"], sorted(model["history"], key=lambda item: (item["created_at"], item["id"]), reverse=True))
            self.assertNotIn("stats", model)
            self.assertNotIn("window_stats", model)

    def test_request_receipt_recovery_is_private_and_survives_history_expiry(self):
        lab = self.lab()
        value = payload()
        run = lab.start_self(1, value)
        self.finished(lab, run["id"])
        self.assertEqual(lab.get_request(value["request_id"], ctx())["id"], run["id"])
        self.error(404, "request_not_found", lab.get_request, value["request_id"], ctx(2, "admin"))
        with lab._lock, lab._db:
            lab._db.execute("DELETE FROM runs WHERE id=?", (run["id"],))
        receipt = lab.get_request(value["request_id"], ctx())
        self.assertEqual(receipt, {"id": run["id"], "status": "passed"})
        self.assertEqual(lab.start_self(1, value), receipt)

    def test_old_text_rates_hidden_without_changing_usage_or_saved_history(self):
        lab = self.lab(lambda *_: result(tokens_per_second=999, output_tokens=23))
        run = lab.start_self(1, payload())
        detail = self.finished(lab, run["id"])
        self.assertIsNone(detail["tokens_per_second"])
        self.assertEqual(detail["output_tokens"], 23)
        row = lab._db.execute("SELECT result_json FROM runs WHERE id=?", (run["id"],)).fetchone()
        self.assertEqual(json.loads(row[0])["tokens_per_second"], 999)
        lab._runner = lambda *_: result(tokens_per_second=12, text_rate_version=1, output_tokens=23)
        run = lab.start_self(1, payload())
        self.assertEqual(self.finished(lab, run["id"])["tokens_per_second"], 12)
        self.assertEqual(lab.list_runs(ctx())["runs"][0]["tokens_per_second"], 12)

    def test_four_workers_and_total_admissions_sixteen(self):
        runner, release, started, calls = self.blocking_runner()
        lab = self.lab(runner)
        runs = [lab.start_self(user_id, payload()) for user_id in range(1, 17)]
        deadline = time.monotonic() + 1
        while len(calls) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(calls), 4)
        self.error(429, "queue_full", lab.start_self, 17, payload())
        release.set()
        for user_id, run in enumerate(runs, start=1):
            self.finished(lab, run["id"], ctx(user_id))
        self.assertEqual(len(calls), 16)

    def test_worker_limit_is_shared_across_lab_instances(self):
        runner, release, started, calls = self.blocking_runner()
        first, second = self.lab(runner, name="first"), self.lab(runner, name="second")
        runs = [(first, first.start_self(user_id, payload()), user_id) for user_id in range(1, 5)]
        runs += [(second, second.start_self(user_id, payload()), user_id) for user_id in range(5, 9)]
        deadline = time.monotonic() + 1
        while len(calls) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(calls), 4)
        release.set()
        for lab, run, user_id in runs:
            self.finished(lab, run["id"], ctx(user_id))
        self.assertEqual(len(calls), 8)

    def test_authorization_public_self_and_public_group_scoping(self):
        lab = self.lab()
        private = lab.start_self(1, payload())
        self.finished(lab, private["id"])
        self.error(404, "run_not_found", lab.get_run, private["id"], ctx(2))
        self.assertEqual(lab.get_run(private["id"], ctx(99, "admin"))["id"], private["id"])
        public = lab.start_self(1, payload(publish=True))
        self.finished(lab, public["id"])
        self.assertEqual(lab.get_run(public["id"], ctx(2))["id"], public["id"])
        self.assertNotIn("user_id", lab.get_run(public["id"], ctx(2)))
        probe = lab.save_probe(payload(group_id=7, publish=True), groups=[{"id": 7, "name": "Private group"}])
        grouped = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
        self.finished(lab, grouped["id"])
        self.error(404, "run_not_found", lab.get_run, grouped["id"], ctx(2))
        self.assertEqual(lab.get_run(grouped["id"], ctx(2, groups=[7]))["id"], grouped["id"])
        self.assertEqual(len(lab.list_runs(ctx(2), "public")["runs"]), 1)
        self.assertEqual(len(lab.list_runs(ctx(2, groups=[7]), "public")["runs"]), 2)
        self.assertEqual(len(lab.dashboard(ctx(2))["groups"]), 0)
        self.assertEqual(lab.dashboard(ctx(2, groups=[7]))["groups"][0]["id"], 7)

    def test_overview_omits_available_groups_without_configured_probes(self):
        lab = self.lab()
        for identity in (ctx(1, "admin", [2, 3]), ctx(2, groups=[2, 3])):
            overview = lab.dashboard(identity)
            self.assertEqual(overview["groups"], [])
            self.assertEqual(overview["summary"]["probes"], 0)
        # Public self-test history is not a configured monitoring probe.
        run = lab.start_self(1, payload(publish=True))
        self.finished(lab, run["id"])
        self.assertEqual(lab.dashboard(ctx(1, "admin", [2, 3]))["groups"], [])
        # A group absent from the overview remains valid for creating a probe.
        probe = lab.save_probe(payload(group_id=3, publish=True),
                               groups=[{"id": 2, "name": "Unused"}, {"id": 3, "name": "New group"}])
        overview = lab.dashboard(ctx(1, "admin", [2, 3]))
        self.assertEqual([group["id"] for group in overview["groups"]], [3])
        self.assertEqual(overview["groups"][0]["models"][0]["probe_id"], probe["id"])

    def test_overview_includes_paused_probes_without_runs_and_does_not_edit_them(self):
        lab = self.lab()
        probe = lab.save_probe(payload(group_id=4, enabled=False, publish=True),
                               groups=[{"id": 4, "name": "Paused group"}])
        original = lab.list_probes()
        for identity in (ctx(1, "admin"), ctx(2, groups=[4])):
            overview = lab.dashboard(identity)
            self.assertEqual(overview["summary"]["probes"], 1)
            self.assertEqual(overview["summary"]["enabled"], 0)
            self.assertEqual([group["id"] for group in overview["groups"]], [4])
            model = overview["groups"][0]["models"][0]
            self.assertEqual(model["probe_id"], probe["id"])
            self.assertFalse(model["enabled"])
            self.assertEqual(model["history"], [])
        self.assertEqual(lab.list_probes(), original)

    def test_overview_removes_group_only_when_last_probe_deleted_despite_history(self):
        lab = self.lab()
        available = [{"id": 4, "name": "A"}, {"id": 5, "name": "B"}]
        first = lab.save_probe(payload(group_id=4, publish=True), groups=available)
        second = lab.save_probe(payload(group_id=4, publish=True), groups=available)
        other = lab.save_probe(payload(group_id=5, publish=True), groups=available)
        run = lab.start_probe(1, first["id"], str(uuid.uuid4()))
        self.finished(lab, run["id"])
        lab.delete_probe(first["id"])
        overview = lab.dashboard(ctx(2, groups=[4, 5]))
        self.assertEqual({group["id"] for group in overview["groups"]}, {4, 5})
        group = next(group for group in overview["groups"] if group["id"] == 4)
        self.assertEqual([model["probe_id"] for model in group["models"]], [second["id"]])
        lab.delete_probe(second["id"])
        overview = lab.dashboard(ctx(2, groups=[4, 5]))
        self.assertEqual([group["id"] for group in overview["groups"]], [5])
        self.assertEqual(overview["summary"]["probes"], 1)
        self.assertEqual(overview["groups"][0]["models"][0]["probe_id"], other["id"])
        self.error(404, "run_not_found", lab.get_run, run["id"], ctx())

    def test_overview_does_not_reveal_unauthorized_or_unpublished_probe_groups(self):
        lab = self.lab()
        available = [{"id": 4, "name": "Allowed"}, {"id": 5, "name": "Restricted"},
                     {"id": 6, "name": "Private"}, {"id": 7, "name": "Unused"}]
        lab.save_probe(payload(group_id=4, publish=True), groups=available)
        lab.save_probe(payload(group_id=5, publish=True), groups=available)
        lab.save_probe(payload(group_id=6, publish=False), groups=available)
        lab.save_probe(payload(group_id=None, publish=True), groups=available)
        overview = lab.dashboard(ctx(2, groups=[4, 6, 7]))
        self.assertEqual({group["id"] for group in overview["groups"]}, {4, None})
        self.assertEqual(overview["summary"]["probes"], 2)
        self.assertNotIn("Restricted", json.dumps(overview))
        self.assertNotIn("Private", json.dumps(overview))
        admin = lab.dashboard(ctx(1, "admin"))
        self.assertEqual({group["id"] for group in admin["groups"]}, {4, 5, 6, None})
        self.assertEqual(admin["summary"]["probes"], 4)

    def test_display_multiplier_rejects_invalid_numbers_and_allows_zero_or_auto(self):
        lab = self.lab()
        for value in (True, False, "2", "", float("nan"), float("inf"), -float("inf"), -1, 1_000_001, 10**400):
            with self.subTest(value=repr(value)):
                self.error(400, "invalid_multiplier", lab.save_probe, payload(display_multiplier=value))
        probe = lab.save_probe(payload(display_multiplier=0))
        self.assertEqual(probe["display_multiplier"], 0)
        self.assertEqual(lab.save_probe({"display_multiplier": 1_000_000}, probe["id"])["display_multiplier"], 1_000_000)
        self.assertIsNone(lab.save_probe({"display_multiplier": None}, probe["id"])["display_multiplier"])
        self.assertIsNone(lab.save_probe(payload())["display_multiplier"])

    def test_display_multiplier_automatic_values_are_current_authorized_and_not_assumed(self):
        lab = self.lab()
        available = [{"id": 2, "name": "Allowed"}, {"id": 3, "name": "Restricted"},
                     {"id": 4, "name": "Unpublished"}, {"id": 5, "name": "Unused"}]
        lab.save_probe(payload(group_id=2, publish=True), groups=available)
        lab.save_probe(payload(group_id=3, publish=True), groups=available)
        lab.save_probe(payload(group_id=4, publish=False), groups=available)
        identity = ctx(2, groups=[2, 4, 5], rates={2: 0, 3: 90, 4: 80, 5: 70})
        group = lab.dashboard(identity)["groups"]
        self.assertEqual([item["id"] for item in group], [2])
        self.assertEqual(group[0]["display_multiplier"], 0)
        self.assertEqual(group[0]["models"][0]["display_multiplier"], 0)
        self.assertEqual(lab.dashboard(ctx(2, groups=[2], rates={2: 2.5}))["groups"][0]["display_multiplier"], 2.5)
        self.assertEqual(lab.dashboard(ctx(2, groups=[5], rates={2: 2.5, 5: 1}))["groups"], [])
        for value in (None, True, "2", -1, float("nan"), float("inf"), 10**400):
            with self.subTest(value=repr(value)):
                group = lab.dashboard(ctx(2, groups=[2], rates={2: value}))["groups"][0]
                self.assertIsNone(group["display_multiplier"])
                self.assertIsNone(group["models"][0]["display_multiplier"])
        # Even an administrator's automatic display rate must come from its verified group catalog.
        admin_groups = lab.dashboard(ctx(1, "admin", rates={2: 2.5}))["groups"]
        self.assertTrue(all(item["display_multiplier"] is None for item in admin_groups))

    def test_display_multiplier_manual_priority_reset_and_mixed_group_values(self):
        lab = self.lab()
        available = [{"id": 2, "name": "Allowed"}]
        first = lab.save_probe(payload(group_id=2, publish=True, display_multiplier=0), groups=available)
        second = lab.save_probe(payload(group_id=2, publish=True), groups=available)
        identity = ctx(2, groups=[2], rates={2: 2.5})
        group = lab.dashboard(identity)["groups"][0]
        values = {model["probe_id"]: model["display_multiplier"] for model in group["models"]}
        self.assertEqual(values, {first["id"]: 0, second["id"]: 2.5})
        self.assertIsNone(group["display_multiplier"])
        lab.save_probe({"display_multiplier": None}, first["id"], groups=available)
        group = lab.dashboard(identity)["groups"][0]
        self.assertEqual(group["display_multiplier"], 2.5)
        self.assertTrue(all(model["display_multiplier"] == 2.5 for model in group["models"]))
        lab.save_probe({"display_multiplier": 3}, first["id"], groups=available)
        group = lab.dashboard(ctx(2, groups=[2]))["groups"][0]
        self.assertIsNone(group["display_multiplier"])
        self.assertEqual({model["probe_id"]: model["display_multiplier"] for model in group["models"]},
                         {first["id"]: 3, second["id"]: None})

    def test_display_multiplier_survives_edits_runs_and_toggles_without_affecting_cost(self):
        captured = []

        def runner(spec, cancel):
            captured.append(dict(spec))
            return result(input_tokens=100, output_tokens=20, cached_tokens=25, total_tokens=120, cost_usd=999)

        lab = self.lab(runner)
        probe = lab.save_probe(payload(display_multiplier=9, input_price=2, cached_price=1, output_price=10))
        self.assertEqual(lab.save_probe({"name": "Renamed"}, probe["id"])["display_multiplier"], 9)
        self.assertEqual(lab.save_probe({"enabled": True}, probe["id"])["display_multiplier"], 9)
        run = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
        detail = self.finished(lab, run["id"])
        self.assertAlmostEqual(detail["cost_usd"], (75 * 2 + 25 + 20 * 10) / 1_000_000)
        self.assertNotIn("display_multiplier", captured[0])
        self.assertNotIn("display_multiplier", detail)
        snapshot = lab._db.execute("SELECT spec_json FROM runs WHERE id=?", (run["id"],)).fetchone()[0]
        self.assertNotIn("display_multiplier", snapshot)
        self.assertEqual(lab.save_probe({"enabled": False}, probe["id"])["display_multiplier"], 9)
        self.assertEqual(lab.list_probes()["probes"][0]["display_multiplier"], 9)
        self_run = lab.start_self(1, payload(display_multiplier=500))
        self.assertIsNone(self.finished(lab, self_run["id"])["cost_usd"])
        self.assertNotIn("display_multiplier", captured[1])

    def test_public_reader_cannot_cancel_and_cancellation_is_idempotent(self):
        runner, release, started, calls = self.blocking_runner()
        lab = self.lab(runner)
        run = lab.start_self(1, payload(publish=True))
        self.assertTrue(started.wait(1))
        self.error(404, "run_not_found", lab.cancel_run, run["id"], ctx(2))
        self.assertEqual(lab.cancel_run(run["id"], ctx())["status"], "cancelled")
        self.assertEqual(lab.cancel_run(run["id"], ctx())["status"], "cancelled")
        self.assertEqual(self.finished(lab, run["id"])["status"], "cancelled")

    def test_scheduler_delays_first_run_and_does_not_overlap_manual_run(self):
        runner, release, started, calls = self.blocking_runner()
        lab = self.lab(runner)
        probe = lab.save_probe(payload(enabled=True, interval_minutes=5, publish=True))
        lab._scheduler_tick()
        self.assertEqual(calls, [])
        run = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
        self.assertTrue(started.wait(1))
        with lab._lock, lab._db:
            lab._db.execute("UPDATE probes SET next_run_at=? WHERE id=?", (time.time() - 1, probe["id"]))
        lab._scheduler_tick()
        self.assertEqual(len(calls), 1)
        self.error(409, "probe_busy", lab.start_probe, 2, probe["id"], str(uuid.uuid4()))
        release.set()
        self.finished(lab, run["id"])
        lab._scheduler_tick()
        deadline = time.monotonic() + 2
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(calls), 2)
        self.assertGreater(lab._probe_row(probe["id"])["next_run_at"], time.time() + 290)
        lab._scheduler_tick()
        self.assertEqual(len(calls), 2)

    def test_self_deletion_removes_public_source_but_preserves_private_receipt(self):
        calls = []
        lab = self.lab(lambda *_: calls.append(1) or result(status="completed", artifact_html="<svg></svg>"))
        value = payload(kind="drawing", preset_id="svg", publish=True)
        run = lab.start_self(1, value)
        detail = self.finished(lab, run["id"])
        self.assertTrue(detail["can_delete"])
        self.assertTrue(lab.list_runs(ctx(), "mine")["runs"][0]["can_delete"])
        self.assertTrue(lab.list_runs(ctx(), "public")["runs"][0]["can_delete"])
        self.assertFalse(lab.get_run(run["id"], ctx(2))["can_delete"])
        self.assertFalse(lab.get_run(run["id"], ctx(2, "admin"))["can_delete"])
        self.assertEqual(lab.delete_run(run["id"], ctx()), {"ok": True})
        self.assertEqual(lab.delete_run(run["id"], ctx()), {"ok": True})
        self.assertEqual(lab.list_runs(ctx()), {"runs": []})
        self.assertEqual(lab.list_runs(ctx(2), "public"), {"runs": []})
        for identity in (ctx(), ctx(2), ctx(2, "admin")):
            self.error(404, "run_not_found", lab.get_run, run["id"], identity)
        self.error(404, "run_not_found", lab.delete_run, run["id"], ctx(2, "admin"))
        self.assertEqual(lab.get_request(value["request_id"], ctx()), {"id": run["id"], "status": "completed"})
        self.assertEqual(lab.start_self(1, value), {"id": run["id"], "status": "completed"})
        self.assertEqual(calls, [1])

    def test_delete_permission_is_owner_self_only_and_mine_excludes_probe_runs(self):
        lab = self.lab()
        run = lab.start_self(1, payload(publish=True))
        self.finished(lab, run["id"])
        for identity in (ctx(2), ctx(2, "admin")):
            self.error(404, "run_not_found", lab.delete_run, run["id"], identity)
        probe = lab.save_probe(payload(publish=True))
        monitored = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
        self.assertFalse(self.finished(lab, monitored["id"])["can_delete"])
        for identity in (ctx(), ctx(1, "admin")):
            self.error(404, "run_not_found", lab.delete_run, monitored["id"], identity)
        self.assertEqual([item["id"] for item in lab.list_runs(ctx())["runs"]], [run["id"]])
        self.assertEqual(len(lab.list_runs(ctx(2), "public")["runs"]), 2)

    def test_active_and_cancelling_self_run_cannot_be_deleted(self):
        release, started = threading.Event(), threading.Event()
        self.events.append(release)

        def runner(spec, cancel):
            started.set()
            release.wait(3)
            return result()

        lab = self.lab(runner)
        run = lab.start_self(1, payload(publish=True))
        self.assertTrue(started.wait(2))
        self.assertFalse(lab.get_run(run["id"], ctx())["can_delete"])
        self.error(409, "run_active", lab.delete_run, run["id"], ctx())
        lab.cancel_run(run["id"], ctx())
        self.assertFalse(lab.get_run(run["id"], ctx())["can_delete"])
        self.error(409, "run_active", lab.delete_run, run["id"], ctx())
        release.set()
        self.assertTrue(self.finished(lab, run["id"])["can_delete"])
        self.assertEqual(lab.delete_run(run["id"], ctx()), {"ok": True})

    def test_probe_deletion_discards_late_result_and_keeps_other_sources(self):
        release, started = threading.Event(), threading.Event()
        self.events.append(release)

        def runner(spec, cancel):
            if spec["model"] == "deleting-probe":
                started.set()
                release.wait(3)
                # Simulate an upstream completing after cancellation was sent.
                return result(status="completed", artifact_html="<svg><title>late</title></svg>")
            return result()

        lab = self.lab(runner)
        self_run = lab.start_self(1, payload())
        self.finished(lab, self_run["id"])
        other = lab.save_probe(payload(publish=True))
        other_run = lab.start_probe(1, other["id"], str(uuid.uuid4()))
        self.finished(lab, other_run["id"])
        probe = lab.save_probe(payload(model="deleting-probe", publish=True))
        request_id = str(uuid.uuid4())
        run = lab.start_probe(1, probe["id"], request_id)
        self.assertTrue(started.wait(2))
        self.assertEqual(lab.delete_probe(probe["id"]), {"ok": True})
        self.assertEqual(lab.get_request(request_id, ctx()), {"id": run["id"], "status": "cancelled"})
        self.error(404, "run_not_found", lab.get_run, run["id"], ctx(1, "admin"))
        release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with lab._lock:
                active = run["id"] in lab._tasks
            if not active:
                break
            time.sleep(0.01)
        self.assertFalse(active)
        self.assertNotIn(probe["id"], lab._active_probes)
        self.assertIsNone(lab._db.execute("SELECT id FROM runs WHERE id=?", (run["id"],)).fetchone())
        self.assertIsNone(lab._db.execute("SELECT run_id FROM channel_metrics WHERE run_id=?", (run["id"],)).fetchone())
        self.assertEqual(lab.get_request(request_id, ctx())["status"], "cancelled")
        self.assertEqual(lab.start_probe(1, probe["id"], request_id), {"id": run["id"], "status": "cancelled"})
        self.assertEqual(lab.get_run(self_run["id"], ctx())["status"], "passed")
        self.assertEqual(lab.get_run(other_run["id"], ctx())["status"], "passed")
        # A fresh request proves the released task admission/worker remains usable.
        next_run = lab.start_probe(1, other["id"], str(uuid.uuid4()))
        self.assertEqual(self.finished(lab, next_run["id"])["status"], "passed")

    def test_old_deleted_or_missing_probe_history_is_hidden_then_removed_on_restart(self):
        lab = self.lab()
        probes = [lab.save_probe(payload(publish=True)) for _ in range(2)]
        run_ids = []
        for probe in probes:
            run = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
            self.finished(lab, run["id"])
            run_ids.append(run["id"])
        self_run = lab.start_self(1, payload(publish=True))
        self.finished(lab, self_run["id"])
        with lab._lock, lab._db:
            lab._db.execute("UPDATE probes SET deleted=1 WHERE id=?", (probes[0]["id"],))
            lab._db.execute("DELETE FROM probes WHERE id=?", (probes[1]["id"],))
        for run_id in run_ids:
            self.error(404, "run_not_found", lab.get_run, run_id, ctx(1, "admin"))
        self.assertEqual([item["id"] for item in lab.list_runs(ctx(), "public")["runs"]], [self_run["id"]])
        self.assertEqual([item["id"] for item in lab.list_runs(ctx())["runs"]], [self_run["id"]])
        lab.close()
        reopened = self.lab()
        self.assertEqual(reopened._db.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
        self.assertEqual(reopened._db.execute("SELECT count(*) FROM channel_metrics").fetchone()[0], 0)
        self.assertEqual(reopened._db.execute("SELECT count(*) FROM requests").fetchone()[0], 3)
        self.assertEqual(reopened.get_run(self_run["id"], ctx())["status"], "passed")

    def test_group_history_hides_request_errors_and_artifactless_drawing_only(self):
        responses = iter([
            result(), result(status="failed"), result(status="error"), result(status="cancelled"),
            result(status="completed", artifact_html="<svg></svg>"), result(status="error"),
            result(status="completed", artifact_html=None),
        ])
        lab = self.lab(lambda *_: next(responses))
        answer = lab.save_probe(payload(publish=True))
        drawing = lab.save_probe(payload(kind="drawing", preset_id="svg", publish=True))
        for probe in (answer, answer, answer, answer, drawing, drawing, drawing):
            run = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
            self.finished(lab, run["id"])
        group = lab.dashboard(ctx(1, "admin"))["groups"][0]
        self.assertEqual([item["status"] for item in group["models"][0]["history"]], ["passed", "failed"])
        self.assertEqual(group["models"][0]["stats"]["pass_rate"], 50)
        self.assertAlmostEqual(group["stats"]["success_rate"], 200 / 3)
        self.assertEqual(len(group["drawing_models"][0]["history"]), 1)
        self.assertTrue(group["drawing_models"][0]["history"][0]["has_artifact"])
        self.assertEqual(lab._db.execute("SELECT count(*) FROM channel_metrics").fetchone()[0], 4)

    def test_deleted_probe_removes_history_key_metrics_and_stops_schedule(self):
        lab = self.lab()
        probe = lab.save_probe(payload(enabled=True, publish=True))
        run = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
        self.finished(lab, run["id"])
        self.assertEqual(lab.delete_probe(probe["id"]), {"ok": True})
        self.assertEqual(lab.list_probes(), {"probes": []})
        self.error(404, "run_not_found", lab.get_run, run["id"], ctx())
        self.assertEqual(lab.list_runs(ctx(), "public"), {"runs": []})
        self.assertEqual(lab._db.execute("SELECT count(*) FROM channel_metrics").fetchone()[0], 0)
        self.assertEqual(lab.get_request(lab._db.execute("SELECT request_id FROM requests").fetchone()[0], ctx()),
                         {"id": run["id"], "status": "passed"})
        row = lab._db.execute("SELECT * FROM probes WHERE id=?", (probe["id"],)).fetchone()
        self.assertIsNone(row["key_ciphertext"])
        self.assertIsNone(row["next_run_at"])
        self.error(404, "probe_not_found", lab.start_probe, 1, probe["id"], str(uuid.uuid4()))

    def test_empty_and_missing_samples_remain_null_and_completed_is_not_scored(self):
        lab = self.lab(lambda *_: result(status="completed", duration_ms=None))
        empty = lab.dashboard(ctx(1, "admin"))
        self.assertEqual(empty["summary"]["runs"], 0)
        self.assertIsNone(empty["summary"]["pass_rate"])
        self.assertNotIn("cache_rate", empty["summary"])
        probe = lab.save_probe(payload(kind="drawing", preset_id="svg", publish=True))
        run = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
        detail = self.finished(lab, run["id"])
        for field in ("ttft_ms", "input_tokens", "output_tokens", "cached_tokens", "total_tokens", "cost_usd"):
            self.assertIsNone(detail[field])
        dashboard = lab.dashboard(ctx(1, "admin"))
        group = dashboard["groups"][0]
        self.assertEqual(group["models"], [])
        self.assertEqual(group["summary"]["runs"], 0)
        self.assertIsNone(group["summary"]["pass_rate"])
        self.assertEqual(group["drawing_models"][0]["history"], [])
        self.assertNotIn("stats", group["drawing_models"][0])

    def test_aggregates_only_valid_samples_and_calculates_price(self):
        responses = iter([
            result(input_tokens=100, output_tokens=20, cached_tokens=25, total_tokens=120,
                   ttft_ms=100, duration_ms=1000, tokens_per_second=20, cost_usd=999),
            result(status="failed", input_tokens=200, cached_tokens=None, output_tokens=30,
                   total_tokens=230, ttft_ms=None, duration_ms=2000),
            result(status="error", ttft_ms=float("nan"), input_tokens=True, total_tokens=-1),
        ])
        lab = self.lab(lambda *_: next(responses))
        probe = lab.save_probe(payload(publish=True, input_price=2, cached_price=1, output_price=10))
        details = []
        for _ in range(3):
            run = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
            details.append(self.finished(lab, run["id"]))
        self.assertAlmostEqual(details[0]["cost_usd"], (75 * 2 + 25 + 20 * 10) / 1_000_000)
        self.assertIsNone(details[1]["cost_usd"])
        self.assertIsNone(details[2]["ttft_ms"])
        self.assertIsNone(details[2]["input_tokens"])
        dashboard = lab.dashboard(ctx(1, "admin"))
        stats = dashboard["groups"][0]["models"][0]["stats"]
        self.assertEqual(stats["passed"], 1)
        self.assertEqual(stats["scored"], 2)
        self.assertEqual(stats["pass_rate"], 50)
        window = dashboard["groups"][0]["stats"]
        self.assertAlmostEqual(window["success_rate"], 200 / 3)
        self.assertNotIn("window_stats", dashboard["groups"][0]["models"][0])
        self.assertEqual(details[0]["cached_tokens"], 25)
        self.assertEqual(window["avg_ttft_ms"], 100)
        self.assertEqual(dashboard["summary"]["total_tokens"], 350)
        history = dashboard["groups"][0]["models"][0]["history"]
        self.assertEqual([item["id"] for item in history], [item["id"] for item in details[:2]])
        self.assertEqual([item["status"] for item in history], ["passed", "failed"])
        self.assertNotIn("trend", dashboard["groups"][0])

    def test_restart_interrupts_leftovers_without_reissuing_request(self):
        lab = self.lab()
        value = payload()
        run = lab.start_self(1, value)
        self.finished(lab, run["id"])
        with lab._lock, lab._db:
            lab._db.execute("UPDATE runs SET status='running',finished_at=NULL WHERE id=?", (run["id"],))
            lab._db.execute("UPDATE requests SET status='running' WHERE run_id=?", (run["id"],))
        lab.close()
        calls = []
        reopened = self.lab(lambda *_: calls.append(1) or result())
        detail = reopened.get_run(run["id"], ctx())
        self.assertEqual(detail["status"], "error")
        self.assertIn("重启", detail["error_message"])
        self.assertEqual(reopened.start_self(1, value), {"id": run["id"], "status": "error"})
        self.assertEqual(calls, [])

    def test_retention_limits_history_but_preserves_idempotency_receipt(self):
        lab = self.lab()
        value = payload()
        first = lab.start_self(1, value)
        self.finished(lab, first["id"])
        source = lab._run_row(first["id"])
        columns = [name for name in source.keys() if name != "has_artifact"]
        with lab._lock, lab._db:
            for number in range(65):
                duplicate = dict(source)
                duplicate.update(id=str(uuid.uuid4()), request_id=str(uuid.uuid4()), created_at=time.time() + number)
                lab._db.execute(f"INSERT INTO runs({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                                [duplicate[name] for name in columns])
            lab._prune_locked(time.time())
        self.assertEqual(lab._db.execute("SELECT count(*) FROM runs").fetchone()[0], 60)
        self.error(404, "run_not_found", lab.get_run, first["id"], ctx())
        self.assertEqual(lab.start_self(1, value)["id"], first["id"])
        with lab._lock, lab._db:
            lab._db.execute("UPDATE runs SET created_at=?", (time.time() - 31 * 3600,))
            lab._prune_locked(time.time())
        self.assertEqual(lab.list_runs(ctx()), {"runs": []})

    def test_thirty_hour_retention_applies_to_all_terminal_runs_but_not_live_runs(self):
        lab = self.lab()
        probe = lab.save_probe(payload(publish=True))
        request_id = str(uuid.uuid4())
        run = lab.start_probe(1, probe["id"], request_id)
        self.finished(lab, run["id"])
        source = lab._run_row(run["id"])
        now = time.time()
        expired = []
        for probe_id in (None, probe["id"]):
            for status in ("passed", "failed", "error", "cancelled", "completed"):
                expired.append(self.copy_record(lab, source, probe_id=probe_id, status=status,
                                                 created_at=now - 31 * 3600, finished_at=now))
        live = [self.copy_record(lab, source, status=status, created_at=now - 31 * 3600, finished_at=None)
                for status in ("queued", "running")]
        retained = [self.copy_record(lab, source, probe_id=probe_id, created_at=now - 29 * 3600)
                    for probe_id in (None, probe["id"])]
        boundary = self.copy_record(lab, source, created_at=now - 30 * 3600)
        with lab._lock, lab._db:
            lab._db.execute("UPDATE runs SET created_at=? WHERE id=?", (now - 31 * 3600, run["id"]))
            lab._prune_locked(now)
            present = {row[0] for row in lab._db.execute("SELECT id FROM runs")}
        self.assertTrue(set(live + retained + [boundary]).issubset(present))
        self.assertFalse(set(expired + [run["id"]]) & present)
        self.assertEqual(lab.start_probe(1, probe["id"], request_id), {"id": run["id"], "status": "passed"})
        self.assertFalse(lab._tasks)

    def test_expired_records_are_hidden_between_cleanup_sweeps(self):
        lab = self.lab()
        probe = lab.save_probe(payload(publish=True))
        run = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
        self.finished(lab, run["id"])
        with lab._lock, lab._db:
            lab._db.execute("UPDATE runs SET created_at=? WHERE id=?", (time.time() - 31 * 3600, run["id"]))
        self.error(404, "run_not_found", lab.get_run, run["id"], ctx())
        self.assertEqual(lab.list_runs(ctx())["runs"], [])
        self.assertEqual(lab.dashboard(ctx(1, "admin"))["groups"][0]["models"][0]["history"], [])
        self.assertIsNotNone(lab._db.execute("SELECT id FROM runs WHERE id=?", (run["id"],)).fetchone())

    def test_idle_cleanup_is_throttled_to_five_minutes(self):
        lab = self.lab()
        with patch.object(lab, "_prune_locked", wraps=lab._prune_locked) as prune:
            for _ in range(3):
                lab._scheduler_tick()
            self.assertEqual(prune.call_count, 0)
            lab._last_prune_monotonic = time.monotonic() - 301
            lab._scheduler_tick()
            self.assertEqual(prune.call_count, 1)
            lab._scheduler_tick()
            self.assertEqual(prune.call_count, 1)

    def test_background_scheduler_cleans_expired_history_without_new_requests(self):
        calls = []
        lab = self.lab(lambda *_: calls.append(1) or result())
        run = lab.start_self(1, payload())
        self.finished(lab, run["id"])
        with lab._lock, lab._db:
            lab._db.execute("UPDATE runs SET created_at=? WHERE id=?", (time.time() - 31 * 3600, run["id"]))
            lab._last_prune_monotonic = time.monotonic() - 301
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            with lab._lock:
                exists = lab._db.execute("SELECT id FROM runs WHERE id=?", (run["id"],)).fetchone()
            if exists is None:
                break
            time.sleep(0.02)
        self.assertIsNone(exists)
        self.assertEqual(calls, [1])
        self.assertIsNotNone(lab._db.execute("SELECT run_id FROM requests WHERE run_id=?", (run["id"],)).fetchone())

    def test_dashboard_hours_only_changes_monitoring_not_model_results_or_artworks(self):
        answers = iter([
            result(status="passed", input_tokens=100, cached_tokens=25, ttft_ms=100),
            result(status="failed", input_tokens=200, cached_tokens=100, ttft_ms=300),
            result(status="passed", input_tokens=100, cached_tokens=50, ttft_ms=500),
            result(status="completed", artifact_html="<svg></svg>", input_tokens=100, cached_tokens=0, ttft_ms=200),
        ])
        lab = self.lab(lambda *_: next(answers))
        reasoning = lab.save_probe(payload(publish=True))
        drawing = lab.save_probe(payload(kind="drawing", preset_id="svg", publish=True))
        now, records = time.time(), []
        for probe, age in ((reasoning, 1800), (reasoning, 7200), (reasoning, 25 * 3600), (drawing, 7200)):
            run = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
            self.finished(lab, run["id"])
            records.append(run["id"])
            with lab._lock, lab._db:
                lab._db.execute("UPDATE runs SET created_at=?,finished_at=? WHERE id=?", (now - age, now - age + 1, run["id"]))
                lab._db.execute("UPDATE channel_metrics SET created_at=? WHERE run_id=?", (now - age, run["id"]))
        hour, day = (lab.dashboard(ctx(1, "admin"), hours=hours) for hours in (1, 24))
        hour_group, day_group = hour["groups"][0], day["groups"][0]
        self.assertEqual(hour_group["models"][0]["history"], day_group["models"][0]["history"])
        self.assertEqual(hour_group["models"][0]["stats"], day_group["models"][0]["stats"])
        self.assertEqual(hour_group["drawing_models"], day_group["drawing_models"])
        self.assertEqual([item["id"] for item in hour_group["drawing_models"][0]["history"]], [records[3]])
        reasoning_model = next(item for item in hour_group["models"] if item["probe_id"] == reasoning["id"])
        self.assertEqual(reasoning_model["stats"]["scored"], 3)
        self.assertEqual(reasoning_model["stats"]["passed"], 2)
        self.assertAlmostEqual(reasoning_model["stats"]["pass_rate"], 200 / 3)
        self.assertEqual(hour_group["stats"]["runs"], 1)
        self.assertEqual(day_group["stats"]["runs"], 2)
        self.assertNotIn("window_stats", reasoning_model)
        self.assertNotIn("window_stats", day_group["models"][0])
        self.assertEqual(hour_group["stats"]["avg_ttft_ms"], 100)
        self.assertEqual(day_group["stats"]["avg_ttft_ms"], 200)
        self.assertEqual(hour["summary"], day["summary"])
        self.assertEqual(hour_group["summary"], day_group["summary"])
        self.assertEqual(hour["summary"]["pass_rate"], 50)
        for overview in (hour, day):
            for forbidden in ("passed", "scored", "pass_rate"):
                self.assertNotIn(forbidden, overview["groups"][0]["stats"])
            self.assertNotIn("cache_rate", overview["summary"])
            self.assertNotIn("trend", overview["groups"][0])
            self.assertEqual(set(overview["groups"][0]["stats"]), {"runs", "success_rate", "avg_ttft_ms"})

    def test_each_probe_retains_sixty_results_independently(self):
        lab = self.lab()
        probes = [lab.save_probe(payload(publish=True)) for _ in range(2)]
        for probe in probes:
            run = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
            self.finished(lab, run["id"])
            source = lab._run_row(run["id"])
            columns = [name for name in source.keys() if name != "has_artifact"]
            with lab._lock, lab._db:
                for number in range(62):
                    duplicate = dict(source)
                    duplicate.update(id=str(uuid.uuid4()), request_id=str(uuid.uuid4()), created_at=time.time() + number)
                    lab._db.execute(f"INSERT INTO runs({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                                    [duplicate[name] for name in columns])
                lab._prune_locked(time.time())
        counts = lab._db.execute("SELECT count(*) FROM runs GROUP BY probe_id").fetchall()
        self.assertEqual([row[0] for row in counts], [60, 60])
        models = lab.dashboard(ctx(1, "admin"))["groups"][0]["models"]
        self.assertEqual([len(model["history"]) for model in models], [60, 60])

    def test_summary_queries_do_not_load_output_artifact_or_prompt(self):
        content = "large-test-artwork-" * 20000
        prompt = "large-test-prompt-" * 1000
        lab = self.lab(lambda *_: result(status="completed", output_text=content,
                                         artifact_html="<p>" + content + "</p>"))
        probe = lab.save_probe(payload(kind="drawing", preset_id="svg", publish=True, prompt=prompt))
        run = lab.start_probe(1, probe["id"], str(uuid.uuid4()))
        detail = self.finished(lab, run["id"])
        self.assertEqual(detail["output_text"], content)
        self.assertEqual(detail["prompt"], prompt)
        original = ModelLab._summary
        projected_rows = []

        def inspect_projection(row):
            self.assertLess(len(row["result_json"]), 1000)
            self.assertLess(len(row["spec_json"]), 1000)
            self.assertNotIn("output_text", json.loads(row["result_json"]))
            self.assertNotIn("artifact_html", json.loads(row["result_json"]))
            self.assertNotIn("prompt", json.loads(row["spec_json"]))
            projected_rows.append(row)
            return original(row)

        with patch.object(ModelLab, "_summary", side_effect=inspect_projection):
            listing = lab.list_runs(ctx(), "public")
            dashboard = lab.dashboard(ctx(1, "admin"))
        self.assertGreaterEqual(len(projected_rows), 2)
        self.assertTrue(listing["runs"][0]["has_artifact"])
        self.assertTrue(dashboard["groups"][0]["drawing_models"][0]["history"][0]["has_artifact"])
        self.assertEqual(lab.get_run(run["id"], ctx())["artifact_html"], "<p>" + content + "</p>")


if __name__ == "__main__":
    unittest.main()
