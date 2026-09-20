"""Temporary synthetic archives verify research integration, not market outcomes."""
import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

from app.config import atomic_json
from app.engine import DEFAULT_WEIGHTS
from app.optimization import optimize_sessions
from app.research import ResearchLibrary
from app.research_data import import_template, validate_import
from app.server import LocalServer
from app.storage import Store


NOW = datetime.fromisoformat("2026-09-20T16:00:00+08:00")
DAY = "2026-09-18"
PREVIOUS = "2026-09-17"
CODES = ["000001.SZ", "600000.SH"]


def stamp(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def dataset(day=DAY, previous=PREVIOUS):
    context = {
        code: {"thscode": code, "name": "协议测试证券", "context_date": previous,
               "continue_day_cnt": 1, "continue_day_text": "首板"}
        for code in CODES
    }
    batches = []
    clocks = ["09:15:00", "09:19:00", "09:22:50", "09:23:10", "09:23:30", "09:24:45", "09:25:10"]
    for index, clock in enumerate(clocks):
        received = f"{day}T{clock}+08:00"
        final = clock >= "09:25:00"
        batches.append({
            "received_at": received, "stage": "final" if final else "live",
            "data": {"timestamp": stamp(received), "auction_phase": "final" if final else "live", "data_status": "ready",
                     "item": [{"thscode": code, "name": "协议测试证券", "auction_pct": 1 + index / 10,
                               "auction_amount": 1_000_000 + index * 100_000,
                               "auction_turnover_pct": .2, "auction_volume_ratio": 2}
                              for code in CODES]},
        })
    return {
        "schema_version": 1,
        "provenance": {"provider": "synthetic-unittest-fixture", "dataset_id": "fixture-2026",
                       "timestamp_semantics": "observed_at", "amount_unit": "CNY",
                       "pct_unit": "percentage_points", "point_in_time_attested": True},
        "sessions": [{
            "manifest": {"date": day, "mode": "live", "prepared_at": day + "T09:05:00+08:00",
                         "previous_date": previous, "calendar": [previous, day], "context": context,
                         "codes": CODES[:], "context_complete": True, "point_in_time": True},
            "batches": batches,
            "outcome": {"date": day, "complete": True, "retrieved_at": day + "T15:11:00+08:00",
                        "rows": [{"thscode": CODES[0], "name": "协议测试证券"}]},
        }],
    }


def retained_report():
    sample = dataset()["sessions"][0]
    return {"date": DAY, "mode": "live", "generated_at": DAY + "T15:11:00+08:00", "status": "ready",
            "raw": {"calendar": [PREVIOUS, DAY],
                    "pools_by_date": {PREVIOUS: list(sample["manifest"]["context"].values()),
                                      DAY: sample["outcome"]["rows"]}}}


class ResearchLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.store = Store(self.data / "market.sqlite3")
        self.library = ResearchLibrary(self.store, self.data)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add_local(self, manifest=True, mode="live"):
        clean = validate_import(dataset(), NOW)["sessions"][0]
        local_manifest = dict(clean["manifest"], source="local_preparation")
        if manifest:
            self.store.freeze_manifest(local_manifest)
        for received, stage, payload in clean["batches"]:
            self.store.batch(DAY, mode, received, stage, payload)
        return local_manifest

    def test_empty_archive_reports_insufficient_data_and_exports_real_empty_result(self):
        self.assertEqual(self.library.latest(), {"status": "not_run"})
        with self.assertRaises(ValueError):
            self.library.export()
        result = self.library.run(DEFAULT_WEIGHTS, NOW)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["availability"]["live_batch_days"], 0)
        self.assertEqual(result["sessions"], [])
        self.assertIsNone(result["optimization"]["candidate_weights"])
        self.assertIsNone(result["optimization"]["baseline"]["precision"])
        self.assertTrue(any("未找到任何真实历史竞价" in warning for warning in result["warnings"]))
        filename, markdown = self.library.export(result["id"])
        self.assertEqual(filename, "auction-research-" + result["id"] + ".md")
        self.assertIn("不是竞价算法命中率", markdown)
        self.assertEqual(self.library.latest(), result)

    def test_retained_full_pools_provide_only_descriptive_transition_baseline(self):
        self.store.report(DAY, "live", retained_report())
        result = self.library.run(DEFAULT_WEIGHTS, NOW)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["availability"]["pool_days"], 2)
        transition = result["transitions"]["rows"][0]
        self.assertEqual(transition["yesterday_count"], 2)
        self.assertEqual(transition["repeat_count"], 1)
        self.assertEqual(transition["repeat_rate_pct"], 50)
        self.assertEqual(result["sessions"], [])
        self.assertEqual(result["optimization"]["sample_summary"]["common_rows"], 0)

    def test_local_replay_limits_primary_signal_and_keeps_original_database_unchanged(self):
        manifest = self.add_local()
        self.store.report(DAY, "live", retained_report())
        before = self.store.batches(DAY)
        result = self.library.run(DEFAULT_WEIGHTS, NOW)
        primary, terminal = result["sessions"]
        self.assertEqual(primary["checkpoint"], "09:24:50")
        self.assertEqual(terminal["checkpoint"], "09:26:00")
        self.assertEqual(primary["quality"]["processed_batches"], 6)
        self.assertEqual(terminal["quality"]["processed_batches"], 7)
        self.assertTrue(primary["quality"]["eligible_for_optimization"])
        self.assertEqual(primary["evaluation"]["selected_count"], 2)
        self.assertEqual(primary["evaluation"]["hits"], 1)
        self.assertEqual(result["optimization"]["sample_summary"]["complete_days"], 1)
        self.assertEqual(self.store.batches(DAY), before)
        self.assertEqual(self.store.manifest(DAY), manifest)

    def test_demo_batches_and_reports_cannot_create_live_experiment_samples(self):
        self.add_local(manifest=False, mode="demo")
        report = retained_report()
        report["mode"] = "demo"
        self.store.report(DAY, "demo", report)
        result = self.library.run(DEFAULT_WEIGHTS, NOW)
        self.assertEqual(result["availability"]["live_batch_days"], 0)
        self.assertEqual(result["availability"]["pool_days"], 0)
        self.assertEqual(result["sessions"], [])

    def test_identical_dataset_and_weights_reuse_archive_without_new_validation(self):
        self.library.import_dataset(dataset(), NOW)
        with patch("app.research.optimize_sessions", wraps=optimize_sessions) as optimize:
            first = self.library.run(DEFAULT_WEIGHTS, NOW)
            second = self.library.run(dict(DEFAULT_WEIGHTS), NOW + timedelta(hours=1))
        self.assertEqual(first, second)
        self.assertEqual(optimize.call_count, 1)
        self.assertEqual(first["generated_at"], NOW.isoformat())

    def test_same_holdout_dates_in_changed_experiment_are_marked_exploratory(self):
        self.library.import_dataset(dataset(), NOW)

        def finished(*args, **kwargs):
            result = optimize_sessions([], DEFAULT_WEIGHTS)
            result.update(status="completed", candidate_weights=dict(DEFAULT_WEIGHTS), holdout={"evaluation_count": 1})
            result["search"]["holdout_dates"] = [DAY]
            result["recommendation"].update(accepted=True, reason="synthetic ledger test only")
            return result

        with patch("app.research.optimize_sessions", side_effect=finished):
            first = self.library.run(DEFAULT_WEIGHTS, NOW)
            changed = dict(DEFAULT_WEIGHTS, gap=.30)
            second = self.library.run(changed, NOW + timedelta(minutes=1))
        self.assertNotEqual(first["id"], second["id"])
        self.assertTrue(first["optimization"]["recommendation"]["accepted"])
        self.assertEqual(second["status"], "exploratory_holdout_reuse")
        self.assertFalse(second["optimization"]["recommendation"]["accepted"])
        self.assertEqual(second["optimization"]["holdout_reuse_dates"], [DAY])
        ledger = json.loads((self.library.root / "holdouts.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["dates"], [DAY])

    def test_cancellation_preserves_previous_latest_report_and_import_archive(self):
        first = self.library.run(DEFAULT_WEIGHTS, NOW)
        self.library.import_dataset(dataset(), NOW)
        imports_before = (self.library.root / "imports.json").read_bytes()
        before = sorted(path.name for path in (self.library.root / "experiments").iterdir())

        def progress(_):
            state["stop"] = True

        state = {"stop": False}
        with self.assertRaisesRegex(ValueError, "中止"):
            self.library.run(DEFAULT_WEIGHTS, NOW + timedelta(minutes=1),
                             should_stop=lambda: state["stop"], progress=progress)
        self.assertEqual(self.library.latest(), first)
        self.assertEqual((self.library.root / "imports.json").read_bytes(), imports_before)
        self.assertEqual(sorted(path.name for path in (self.library.root / "experiments").iterdir()), before)

    def test_import_is_idempotent_and_never_writes_live_database(self):
        incoming = dataset()
        frozen = copy.deepcopy(incoming)
        first = self.library.import_dataset(incoming, NOW)
        contents = (self.library.root / "imports.json").read_bytes()
        second = self.library.import_dataset(copy.deepcopy(incoming), NOW)
        self.assertEqual(first["import_id"], second["import_id"])
        self.assertEqual((self.library.root / "imports.json").read_bytes(), contents)
        self.assertEqual(self.store.batch_dates(), [])
        self.assertIsNone(self.store.manifest(DAY))
        self.assertEqual(incoming, frozen)

    def test_import_refuses_existing_local_day_or_another_source_version(self):
        self.add_local()
        with self.assertRaisesRegex(ValueError, "本机真实竞价"):
            self.library.import_dataset(dataset(), NOW)
        self.assertFalse((self.library.root / "imports.json").exists())
        self.store.close()
        self.store = Store(self.data / "another.sqlite3")
        self.library = ResearchLibrary(self.store, self.data)
        self.library.import_dataset(dataset(), NOW)
        before = (self.library.root / "imports.json").read_bytes()
        changed = dataset()
        changed["provenance"]["dataset_id"] = "different-source-version"
        with self.assertRaisesRegex(ValueError, "另一数据版本"):
            self.library.import_dataset(changed, NOW)
        self.assertEqual((self.library.root / "imports.json").read_bytes(), before)

    def test_local_data_arriving_after_import_excludes_conflicting_day(self):
        self.library.import_dataset(dataset(), NOW)
        self.add_local()
        result = self.library.run(DEFAULT_WEIGHTS, NOW)
        self.assertEqual(result["sessions"], [])
        self.assertTrue(any("同时存在本机和导入来源" in item for item in result["warnings"]))

    def test_old_local_batches_without_manifest_do_not_abort_research(self):
        self.add_local(manifest=False)
        result = self.library.run(DEFAULT_WEIGHTS, NOW)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["optimization"]["sample_summary"]["complete_days"], 0)
        self.assertTrue(result["warnings"])

    def test_reconstructed_context_cannot_become_eligible_for_search(self):
        self.add_local(manifest=False)
        self.store.report(DAY, "live", retained_report())
        result = self.library.run(DEFAULT_WEIGHTS, NOW)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["optimization"]["sample_summary"]["complete_days"], 0)
        self.assertTrue(all(not item["quality"]["eligible_for_optimization"] for item in result["sessions"]))

    def test_bad_report_payloads_cannot_supply_complete_pool_evidence(self):
        bad_reports = []
        raw_none = retained_report()
        raw_none["raw"] = None
        bad_reports.append(raw_none)
        different_day = retained_report()
        different_day["date"] = PREVIOUS
        bad_reports.append(different_day)
        future_calendar = retained_report()
        future_calendar["raw"]["calendar"].append("2026-09-30")
        bad_reports.append(future_calendar)
        duplicate_calendar = retained_report()
        duplicate_calendar["raw"]["calendar"].append(DAY)
        bad_reports.append(duplicate_calendar)
        for report in bad_reports:
            with self.subTest(report=report):
                self.store.report(DAY, "live", report)
                pools, calendar = self.library._pool_evidence(NOW, None)
                self.assertEqual(pools, {})
                self.assertEqual(calendar, [])

    def test_partial_missing_or_unclosed_pool_evidence_not_labelled_as_empty(self):
        report = retained_report()
        report["raw"]["pools_by_date"][DAY] = None
        self.store.report(DAY, "live", report)
        pools, _ = self.library._pool_evidence(NOW, None)
        self.assertNotIn(DAY, pools)
        report["raw"]["pools_by_date"][DAY] = []
        report["generated_at"] = DAY + "T15:09:59+08:00"
        self.store.report(DAY, "live", report)
        pools, _ = self.library._pool_evidence(NOW, None)
        self.assertNotIn(DAY, pools)
        report["generated_at"] = DAY + "T15:10:00+08:00"
        self.store.report(DAY, "live", report)
        pools, _ = self.library._pool_evidence(NOW, None)
        self.assertEqual(pools[DAY]["rows"], [])

    def test_export_rejects_path_traversal_and_unrecognised_formats(self):
        self.library.run(DEFAULT_WEIGHTS, NOW)
        for unsafe in ("../latest", "..\\latest", "/tmp/file", "a" * 23, "A" * 24, "a" * 24 + ".json"):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                self.library.export(unsafe)
        with self.assertRaises(ValueError):
            self.library.export(format_name="../../config")
        filename, payload = self.library.export(format_name="json")
        self.assertTrue(filename.startswith("auction-research-"))
        self.assertEqual(payload["id"], self.library.latest()["id"])

    def ai_fixture(self, day_count=10, development_dates=None):
        report = self.library.run(DEFAULT_WEIGHTS, NOW)
        report["sessions"] = []
        for index in range(day_count):
            day = f"2026-09-{index + 1:02d}"
            row = {"thscode": CODES[0], "label": index % 2 == 0,
                   "name": "DEVELOPMENT_LABEL" if index < 5 else "HOLDOUT_LABEL_SENTINEL"}
            primary = {"date": day, "checkpoint": "09:24:50", "mode": "live",
                       "quality": {"eligible_for_optimization": True}, "rows": [row]}
            report["sessions"].append(primary)
            report["sessions"].append(dict(copy.deepcopy(primary), checkpoint="09:26:00"))
        if development_dates is not None:
            report["optimization"]["search"]["development_dates"] = development_dates
        atomic_json(self.library.root / "experiments" / (report["id"] + ".json"), report)
        return report

    def test_ai_development_export_omits_reserved_rows_labels_and_terminal_observations(self):
        report = self.ai_fixture()
        result = self.library.ai_dataset(report["id"])
        self.assertEqual(result["scope"], "development_only")
        self.assertEqual([item["date"] for item in result["sessions"]],
                         [f"2026-09-{index:02d}" for index in range(1, 6)])
        self.assertEqual(result["excluded_date_count"], 5)
        self.assertTrue(all(item["checkpoint"] == "09:24:50" for item in result["sessions"]))
        self.assertNotIn("HOLDOUT_LABEL_SENTINEL", json.dumps(result))
        self.assertNotIn("holdout", result)

    def test_ai_development_export_respects_fixed_split_and_rejects_bad_provenance(self):
        days = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
        report = self.ai_fixture(development_dates=days)
        primary = [item for item in report["sessions"] if item["checkpoint"] == "09:24:50"]
        primary[0]["mode"] = "demo"
        primary[1]["mode"] = "reconstructed"
        primary[2]["quality"]["eligible_for_optimization"] = False
        atomic_json(self.library.root / "experiments" / (report["id"] + ".json"), report)
        result = self.library.ai_dataset(report["id"])
        self.assertEqual([item["date"] for item in result["sessions"]], ["2026-09-04"])
        self.assertNotIn("HOLDOUT_LABEL_SENTINEL", json.dumps(result))

    def test_ai_export_has_no_training_rows_when_five_or_fewer_days_exist(self):
        report = self.ai_fixture(day_count=5)
        result = self.library.ai_dataset(report["id"])
        self.assertEqual(result["sessions"], [])
        self.assertEqual(result["excluded_date_count"], 5)

    def test_external_proposal_is_idempotent_archive_only_and_preserves_live_config(self):
        report = self.library.run(DEFAULT_WEIGHTS, NOW)
        config = {"weights": dict(DEFAULT_WEIGHTS), "watchlist": CODES[:], "enabled": True}
        atomic_json(self.data / "config.json", config)
        original = (self.data / "config.json").read_bytes()
        proposal = {"experiment_id": report["id"], "weights": dict(DEFAULT_WEIGHTS, gap=.3),
                    "source_model": "protocol-test-model", "rationale": "synthetic proposal fixture",
                    "unknown_private_payload": "MUST_NOT_BE_ARCHIVED"}
        first = self.library.save_proposal(proposal, NOW)
        path = self.library.root / "proposals" / (first["proposal_id"] + ".json")
        archived = path.read_bytes()
        second = self.library.save_proposal(proposal, NOW + timedelta(hours=1))
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "awaiting_future_validation")
        self.assertFalse(first["automatically_applied"])
        self.assertEqual(path.read_bytes(), archived)
        self.assertNotIn("MUST_NOT_BE_ARCHIVED", archived.decode("utf-8"))
        self.assertEqual((self.data / "config.json").read_bytes(), original)
        self.assertEqual(self.library.latest(), report)

    def test_external_proposal_requires_exact_factor_keys_and_existing_experiment(self):
        report = self.library.run(DEFAULT_WEIGHTS, NOW)
        for weights in ({"gap": 1}, {**DEFAULT_WEIGHTS, "new_factor": 1},
                        dict(DEFAULT_WEIGHTS, gap=True), dict(DEFAULT_WEIGHTS, gap=-1)):
            with self.subTest(weights=weights), self.assertRaises(ValueError):
                self.library.save_proposal({"experiment_id": report["id"], "weights": weights}, NOW)
        with self.assertRaises(ValueError):
            self.library.save_proposal({"experiment_id": "../config", "weights": DEFAULT_WEIGHTS}, NOW)
        self.assertFalse((self.library.root / "proposals").exists())

    def test_http_export_download_and_proposal_boundary_use_only_the_research_archive(self):
        report = self.library.run(DEFAULT_WEIGHTS, NOW)
        service = SimpleNamespace(research_library=self.library, mode="live", _auction_priority=lambda: False)
        server = LocalServer(("127.0.0.1", 0), service)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with opener.open(base + "/api/research/export?format=json", timeout=3) as response:
                self.assertEqual(json.load(response)["id"], report["id"])
                self.assertIn("auction-research-" + report["id"] + ".json", response.headers["Content-Disposition"])
            with opener.open(base + "/api/research/export?format=markdown", timeout=3) as response:
                self.assertEqual(response.headers.get_content_type(), "text/markdown")
                self.assertIn("竞价历史回放", response.read().decode("utf-8"))
            for query in ({"id": "../../config"}, {"format": "../../config"}, {"id": "a" * 24}):
                with self.subTest(query=query), self.assertRaises(urllib.error.HTTPError) as rejected:
                    opener.open(base + "/api/research/export?" + urllib.parse.urlencode(query), timeout=3)
                self.assertEqual(rejected.exception.code, 400)
            with opener.open(base + "/api/research/ai-dataset", timeout=3) as response:
                self.assertEqual(json.load(response)["sessions"], [])
            proposal = {"experiment_id": report["id"], "weights": DEFAULT_WEIGHTS, "source_model": "protocol-test"}
            request = urllib.request.Request(base + "/api/research/proposals", data=json.dumps(proposal).encode(),
                                             headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                opener.open(request, timeout=3)
            self.assertEqual(rejected.exception.code, 403)
            request.add_header("X-Local-App", "auction-lab")
            with patch("app.server.now_sh", return_value=NOW), opener.open(request, timeout=3) as response:
                saved = json.load(response)
            self.assertTrue(saved["ok"])
            self.assertFalse(saved["automatically_applied"])
            self.assertEqual(self.library.latest(), report)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)


class HistoricalImportValidationTests(unittest.TestCase):
    def test_empty_template_does_not_contain_or_claim_market_data(self):
        template = import_template()
        self.assertEqual(template["sessions"], [])
        self.assertFalse(template["provenance"]["point_in_time_attested"])
        with self.assertRaises(ValueError):
            validate_import(template, NOW)

    def test_boolean_schema_and_integer_attestation_are_not_accepted(self):
        for value in (True, "1", 1.0, None):
            incoming = dataset()
            incoming["schema_version"] = value
            with self.subTest(schema_version=value), self.assertRaises(ValueError):
                validate_import(incoming, NOW)
        incoming = dataset()
        incoming["provenance"]["point_in_time_attested"] = 1
        with self.assertRaises(ValueError):
            validate_import(incoming, NOW)

    def test_wrong_units_demo_future_unclosed_and_duplicate_sessions_rejected(self):
        mutations = [
            lambda item: item["provenance"].update(amount_unit="万元"),
            lambda item: item["provenance"].update(pct_unit="fraction"),
            lambda item: item["sessions"][0]["manifest"].update(mode="demo"),
            lambda item: item["sessions"][0]["manifest"].update(date="2026-09-21"),
            lambda item: item["sessions"][0]["outcome"].update(retrieved_at=DAY + "T15:09:59+08:00"),
            lambda item: item["sessions"][0]["outcome"].update(complete=False),
            lambda item: item["sessions"].append(copy.deepcopy(item["sessions"][0])),
        ]
        for mutate in mutations:
            incoming = dataset()
            mutate(incoming)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                validate_import(incoming, NOW)

    def test_context_dates_codes_and_receipt_order_are_checked(self):
        mutations = [
            lambda item: item["sessions"][0]["manifest"].update(previous_date="2026-09-16"),
            lambda item: item["sessions"][0]["manifest"]["context"][CODES[0]].update(context_date=DAY),
            lambda item: item["sessions"][0]["manifest"]["context"][CODES[0]].update(thscode=CODES[1]),
            lambda item: item["sessions"][0]["batches"].reverse(),
            lambda item: item["sessions"][0]["batches"][0].update(received_at=DAY + "T09:15:00"),
            lambda item: item["sessions"][0]["batches"][0]["data"]["item"][0].update(auction_amount=True),
            lambda item: item["sessions"][0]["batches"][0]["data"]["item"][0].update(auction_pct=float("nan")),
        ]
        for mutate in mutations:
            incoming = dataset()
            mutate(incoming)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                validate_import(incoming, NOW)

    def test_unknown_fields_are_not_persisted_as_market_evidence(self):
        incoming = dataset()
        incoming["unknown_top_level"] = "not-market-evidence"
        incoming["provenance"]["unexpected_config"] = "do-not-persist"
        incoming["sessions"][0]["manifest"]["private_annotation"] = "do-not-persist"
        incoming["sessions"][0]["batches"][0]["data"]["item"][0]["unverified_future_label"] = "do-not-persist"
        result = validate_import(incoming, NOW)
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        self.assertNotIn("do-not-persist", encoded)
        self.assertNotIn("not-market-evidence", encoded)


if __name__ == "__main__":
    unittest.main()
