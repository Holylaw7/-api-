"""Service/HTTP research integration with temporary storage and no remote calls."""
import copy
from datetime import datetime
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse
import urllib.request

from app.config import DEFAULT
from app.engine import AuctionEngine
from app.research_data import MAX_IMPORT_BYTES, validate_import
from app.server import LocalServer
from app.service import Service, SH
from tests.test_research import CODES, DAY, NOW, PREVIOUS, dataset
from tests.test_official_workflow import report


def at(clock, day=DAY):
    return datetime.fromisoformat(day + "T" + clock + "+08:00")


class ResearchServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.patches = [
            patch("app.service.load_config", return_value=copy.deepcopy(DEFAULT)),
            patch("app.service.finance_key", return_value=""),
            patch("app.service.credential_status", return_value={}),
            patch("app.ai_gateway.credential_dir", return_value=self.root / "credentials"),
            patch.dict("os.environ", {"DEEPSEEK_API_KEY": "", "OPENAI_API_KEY": "", "CUSTOM_AI_API_KEY": ""}),
            patch("app.service.now_sh", return_value=NOW),
            patch("app.provider.HiThinkProvider.get", side_effect=AssertionError("Remote data is forbidden in this test")),
        ]
        for item in self.patches:
            item.start()
        self.service = Service(self.root)
        self.service.shutdown.set()
        self.service.thread.join(2)
        self.service.shutdown.clear()
        self.queued = {}

    def tearDown(self):
        self.service.close()
        self.service.store.close()
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def queue(self, operation, *args):
        def queued(name, worker):
            self.queued[name] = worker
            return True

        with patch.object(self.service, "_job", side_effect=queued):
            self.assertTrue(operation(*args))

    def seed_engine(self):
        clean = validate_import(dataset(), NOW)["sessions"][0]
        manifest = dict(clean["manifest"], source="local_preparation", weights=dict(self.service.engine.weights))
        self.service.store.freeze_manifest(manifest)
        self.service.context = manifest["context"]
        self.service.session_date = DAY
        self.service.prepared_date = DAY
        self.service.codes = CODES[:]
        self.service.mode = "live"
        self.service.running = True
        for received, stage, data in clean["batches"][:-1]:
            payload = dict(data, _strategy_weights=dict(self.service.engine.weights))
            self.service.store.batch(DAY, "live", received, stage, payload)
            self.service.engine.ingest(payload, datetime.fromisoformat(received), manifest["context"])
        return manifest

    def payload(self, clock, pct):
        received = at(clock)
        return {"timestamp": int(received.timestamp() * 1000), "auction_phase": "live", "data_status": "ready",
                "item": [{"thscode": code, "name": "协议测试证券", "auction_price": 10,
                          "auction_pct": pct, "auction_amount": 2_000_000,
                          "auction_turnover_pct": .2, "auction_volume_ratio": 2} for code in CODES]}

    def test_research_and_import_guards_cover_demo_and_entire_0926_minute(self):
        with patch.object(self.service, "_job") as job, patch.object(self.service.research_library, "import_dataset") as imported:
            for mode, clock in (("demo", "16:00:00"), ("live", "09:10:00"), ("live", "09:26:59")):
                self.service.mode = mode
                with self.subTest(mode=mode, clock=clock), patch("app.service.now_sh", return_value=at(clock)):
                    with self.assertRaises(ValueError):
                        self.service.run_research()
                    with self.assertRaises(ValueError):
                        self.service.import_research(dataset())
            job.assert_not_called()
            imported.assert_not_called()
        self.service.mode = "live"
        with patch("app.service.now_sh", return_value=at("09:27:00")):
            self.queue(self.service.run_research)
            with patch.object(self.service.research_library, "import_dataset", return_value={"imported_days": 1}) as imported:
                self.assertEqual(self.service.import_research(dataset()), {"imported_days": 1})
                imported.assert_called_once()

    def test_running_research_blocks_import_without_touching_existing_archives(self):
        self.service.jobs["research"] = {"status": "running"}
        with patch.object(self.service.research_library, "import_dataset") as imported, self.assertRaises(ValueError):
            self.service.import_research(dataset())
        imported.assert_not_called()
        self.assertFalse((self.root / "research" / "imports.json").exists())

    def test_research_captures_weights_and_exposes_only_compact_summary(self):
        expected = dict(self.service.config["weights"])
        engine = self.service.engine
        self.queue(self.service.run_research)
        self.service.config["weights"] = dict(expected, gap=.4)
        result = {"id": "a" * 24, "status": "insufficient_data", "generated_at": NOW.isoformat(),
                  "sessions": [{"private_large_rows": [1, 2, 3]}], "optimization": {"sensitive_fixture": "NOT_IN_SSE"}}
        with patch.object(self.service.research_library, "run", return_value=result) as run:
            self.queued["research"]()
        self.assertEqual(run.call_args.args[0], expected)
        self.assertIs(self.service.engine, engine)
        state = self.service.snapshot()
        self.assertEqual(state["research"], {key: result[key] for key in ("id", "status", "generated_at")})
        self.assertNotIn("NOT_IN_SSE", json.dumps(state))

    def test_research_cancellation_is_bound_to_mode_generation_and_protection(self):
        self.queue(self.service.run_research)

        def run(weights, now, *, should_stop, progress):
            self.assertFalse(should_stop())
            self.service.demo_generation += 1
            self.assertTrue(should_stop())
            raise ValueError("cancelled fixture")

        before = copy.deepcopy(self.service.research_summary)
        with patch.object(self.service.research_library, "run", side_effect=run), self.assertRaises(ValueError):
            self.queued["research"]()
        self.assertEqual(self.service.research_summary, before)

    def test_first_preparation_manifest_is_not_replaced_by_later_context(self):
        original = validate_import(dataset(), NOW)["sessions"][0]["manifest"]["context"]
        provider = Mock()
        provider.calendar.return_value = [PREVIOUS, DAY]
        provider.pool.return_value = list(copy.deepcopy(original).values())
        with patch.object(self.service, "_provider", return_value=provider), patch("app.service.now_sh", return_value=at("09:05:00")):
            self.service._prepare()
        first = self.service.store.manifest(DAY)
        self.assertTrue(first["point_in_time"])
        self.assertEqual(first["previous_date"], PREVIOUS)
        changed = list(copy.deepcopy(original).values())
        changed[0]["continue_day_cnt"] = 2
        changed[0]["continue_day_text"] = "2连板"
        provider.pool.return_value = changed
        with patch.object(self.service, "_provider", return_value=provider), patch("app.service.now_sh", return_value=at("16:00:00")):
            self.service._prepare()
        self.assertEqual(self.service.store.manifest(DAY), first)
        self.assertEqual(self.service.context[CODES[0]]["continue_day_cnt"], 2)

    def test_capture_is_before_later_ingest_and_batch_keeps_actual_strategy_weights(self):
        self.seed_engine()
        weights = AuctionEngine.validate_weights(dict(DEFAULT["weights"], gap=.3))
        self.service.engine.weights = weights
        payload = self.payload("09:24:55", 9)
        provider = Mock()
        provider.auction.return_value = payload
        clocks = iter([at("09:24:51"), at("09:24:55")])
        with patch.object(self.service, "_provider", return_value=provider):
            self.service.collect_cycle("live", clock=lambda: next(clocks))
        saved = self.service.store.decision(DAY, "09:24:50")
        self.assertIsNotNone(saved)
        self.assertEqual(saved["weights"], weights)
        self.assertTrue(all(row["auction_pct"] == 1.5 for row in saved["rows"]))
        self.assertTrue(all(row["auction_pct"] == 9 for row in self.service.engine.rankings(at("09:24:55"))))
        self.assertEqual(self.service.store.batches(DAY)[-1][2]["_strategy_weights"], weights)
        self.assertNotIn("_strategy_weights", payload)

    def test_batch_received_exactly_at_cutoff_is_included_in_frozen_ranking(self):
        self.seed_engine()
        provider = Mock()
        provider.auction.return_value = self.payload("09:24:50", 8)
        clocks = iter([at("09:24:49"), at("09:24:50")])
        with patch.object(self.service, "_provider", return_value=provider):
            self.service.collect_cycle("live", clock=lambda: next(clocks))
        self.service._capture_due_decisions(at("09:24:51"))
        saved = self.service.store.decision(DAY, "09:24:50")
        self.assertIsNotNone(saved)
        self.assertTrue(all(row["auction_pct"] == 8 for row in saved["rows"]))

    def test_capture_never_queries_future_engine_or_different_session(self):
        self.seed_engine()
        self.service.engine.ingest(self.payload("09:24:55", 9), at("09:24:55"), self.service.context)
        self.service._capture_due_decisions(at("09:25:00"))
        self.assertIsNone(self.service.store.decision(DAY, "09:24:50"))
        self.service._capture_due_decisions(at("09:27:00", "2026-09-19"))
        self.assertIsNone(self.service.store.decision(DAY, "09:26:00"))
        self.assertIsNone(self.service.store.decision("2026-09-19", "09:24:50"))

    def test_recorded_decision_is_immutable_across_subsequent_weight_changes(self):
        self.seed_engine()
        self.service._capture_due_decisions(at("09:24:51"))
        first = self.service.store.decision(DAY, "09:24:50")
        self.service.engine.weights = AuctionEngine.validate_weights(dict(DEFAULT["weights"], gap=.5))
        self.service._capture_due_decisions(at("09:24:59"))
        self.assertEqual(self.service.store.decision(DAY, "09:24:50"), first)

    def test_after_close_review_labels_then_researches_without_modifying_auction_engine(self):
        self.seed_engine()
        before = copy.deepcopy(self.service.engine.history)
        provider = Mock()
        provider.calendar.return_value = [PREVIOUS, DAY]
        outcome = report()
        with patch.object(self.service, "_provider", return_value=provider), \
                patch("app.service.build_review", return_value=outcome), \
                patch.object(self.service, "_save_markdown"), \
                patch.object(self.service.daily_validation, "label", return_value={"status": "ready"}) as label, \
                patch.object(self.service, "run_research", return_value=True) as research:
            self.queue(self.service.run_review, DAY)
            self.queued["review"]()
        label.assert_called_once()
        self.assertEqual(label.call_args.args[0]["date"], DAY)
        research.assert_called_once()
        self.assertEqual(self.service.engine.history, before)
        self.assertEqual(self.service.store.batches(DAY)[0][2]["_strategy_weights"], self.service.engine.weights)

    def test_review_with_unavailable_daily_data_does_not_start_fake_research(self):
        provider = Mock()
        provider.calendar.return_value = [PREVIOUS, DAY]
        with patch.object(self.service, "_provider", return_value=provider), \
                patch("app.service.build_review", return_value=report()), \
                patch.object(self.service, "_save_markdown"), \
                patch.object(self.service.daily_validation, "label", return_value={"status": "unavailable"}), \
                patch.object(self.service, "run_research") as research:
            self.queue(self.service.run_review, DAY)
            self.queued["review"]()
        research.assert_not_called()

    def test_manual_review_before_1510_does_not_fail_due_to_late_label_requirement(self):
        provider = Mock()
        provider.calendar.return_value = [PREVIOUS, DAY]
        with patch("app.service.now_sh", return_value=at("15:05:00")), \
                patch.object(self.service, "_provider", return_value=provider), \
                patch("app.service.build_review", return_value=report()), \
                patch.object(self.service, "_save_markdown"), \
                patch.object(self.service.daily_validation, "label") as label, \
                patch.object(self.service, "run_research") as research:
            self.queue(self.service.run_review, DAY)
            self.queued["review"]()
        label.assert_not_called()
        research.assert_not_called()
        self.assertEqual(self.service.review["date"], DAY)

    def test_service_restart_route_needs_an_explicit_restart_entry(self):
        server = LocalServer(("127.0.0.1", 0), self.service)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f"http://127.0.0.1:{server.server_port}"

        def post(path, body):
            request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "X-Local-App": "auction-lab"})
            return opener.open(request, timeout=5)

        self.service.running = True
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                post("/api/service/restart", {})
            self.assertEqual(error.exception.code, 400)
            self.assertIn("未提供自动重启入口", error.exception.read().decode("utf-8"))
            self.assertTrue(self.service.running, "拒绝时不能停止采集")
            restarted = threading.Event()
            server.restart = lambda auto_start: restarted.set()
            with post("/api/service/restart", {}) as response:
                payload = json.load(response)
            self.assertTrue(payload["ok"])
            self.assertIs(payload["restarting"], True)
            self.assertTrue(restarted.wait(3), "本机重启入口应被触发")
            self.assertFalse(self.service.running, "请求重启后应停止采集")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_research_http_routes_and_import_body_size_limits(self):
        self.service.research_library.run(self.service.config["weights"], NOW)
        server = LocalServer(("127.0.0.1", 0), self.service)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f"http://127.0.0.1:{server.server_port}"

        def post(path, body, headers=None):
            request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "X-Local-App": "auction-lab", **(headers or {})})
            return opener.open(request, timeout=3)

        try:
            with opener.open(base + "/api/research/template", timeout=3) as response:
                self.assertEqual(json.load(response)["sessions"], [])
            with opener.open(base + "/api/research", timeout=3) as response:
                self.assertEqual(json.load(response)["status"], "insufficient_data")
            with opener.open(base + "/api/research/daily", timeout=3) as response:
                self.assertEqual(json.load(response), {"items": []})
            with opener.open(base + "/api/research/daily?date=" + DAY, timeout=3) as response:
                self.assertEqual(json.load(response)["status"], "unavailable")
            for path in ("/api/research/daily?date=../config", "/api/research/daily/export?date=" + DAY):
                with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as rejected:
                    opener.open(base + path, timeout=3)
                self.assertEqual(rejected.exception.code, 400)
            with patch.object(self.service, "run_research", return_value=True) as run:
                with post("/api/research/run", {}) as response:
                    self.assertTrue(json.load(response)["started"])
                run.assert_called_once()
            with patch.object(self.service, "import_research", return_value={"imported_days": 1}) as imported:
                with post("/api/research/import", {"dataset": dataset(), "padding": " " * 70000}) as response:
                    self.assertEqual(json.load(response)["imported_days"], 1)
                imported.assert_called_once()
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                post("/api/research/run", {"padding": " " * 70000})
            self.assertEqual(rejected.exception.code, 413)
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                connection.request("POST", "/api/research/import", body=b"", headers={
                    "X-Local-App": "auction-lab", "Content-Length": str(MAX_IMPORT_BYTES + 1)})
                response = connection.getresponse()
                self.assertEqual(response.status, 413)
                response.read()
            finally:
                connection.close()
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                post("/api/research/run", {}, {"Origin": "https://external.invalid"})
            self.assertEqual(rejected.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)


if __name__ == "__main__":
    unittest.main()
