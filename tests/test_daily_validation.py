import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.daily_validation import DailyValidation
from app.engine import AuctionEngine, DEFAULT_WEIGHTS, SHANGHAI
from app.replay import replay_session
from app.storage import Store

DAY, PREVIOUS, CODE = "2026-09-18", "2026-09-17", "000001.SZ"


def at(clock, day=DAY):
    return datetime.fromisoformat(day + "T" + clock).replace(tzinfo=SHANGHAI)


def row():
    return {"thscode": CODE, "name": "样本股", "context_date": PREVIOUS,
            "continue_day_text": "2连板", "continue_day_cnt": 2}


def manifest():
    return {"date": DAY, "previous_date": PREVIOUS, "mode": "live", "point_in_time": True,
            "prepared_at": at("09:05:00").isoformat(), "context_complete": True,
            "calendar": [PREVIOUS, DAY], "context": {CODE: row()}, "codes": [CODE],
            "source": "local_preparation", "weights": dict(DEFAULT_WEIGHTS)}


def report(rows=None):
    return {"date": DAY, "mode": "live", "generated_at": at("15:10:00").isoformat(),
            "raw": {"calendar": [PREVIOUS, DAY], "pools_by_date": {DAY: [row()] if rows is None else rows}}}


class DailyValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "market.sqlite")
        self.daily = DailyValidation(self.store, self.root)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def batch(self, clock, pct=2, weights=None, final=False):
        stamp = at(clock)
        value = {"timestamp": int(stamp.timestamp() * 1000), "auction_phase": "final" if final else "live",
                 "data_status": "ready", "item": [{"thscode": CODE, "name": "样本股",
                    "auction_pct": pct, "auction_amount": 20_000_000, "auction_turnover_pct": .3,
                    "auction_volume_ratio": 3}]}
        if weights is not None:
            value["_strategy_weights"] = weights
        self.store.batch(DAY, "live", stamp.isoformat(), "final" if final else "live", value)

    def seed(self, with_weights=True):
        self.store.freeze_manifest(manifest())
        weights = dict(DEFAULT_WEIGHTS) if with_weights else None
        for clock, pct in (("09:16:00", 1), ("09:24:00", 1), ("09:24:20", 2), ("09:24:40", 3)):
            self.batch(clock, pct, weights)
        self.batch("09:25:05", 4, weights, final=True)

    def decision(self, checkpoint="09:24:50", weights=None):
        current = AuctionEngine(weights or DEFAULT_WEIGHTS)
        for received, stage, payload in self.store.batches(DAY):
            stamp = datetime.fromisoformat(received)
            if stamp <= at(checkpoint):
                current.ingest(payload, stamp, manifest()["context"])
        return {"date": DAY, "mode": "live", "checkpoint": checkpoint,
                "captured_at": at("09:24:52" if checkpoint == "09:24:50" else "09:27:00").isoformat(),
                "weights": dict(current.weights), "rows": current.rankings(at(checkpoint)),
                "engine_source_sha256": "a" * 64}

    def test_freeze_immutable_with_provenance_and_raw_stays_only_sqlite(self):
        self.seed()
        value = self.daily.freeze(DAY, at("09:27:00"))
        path = self.daily.root / (DAY + "-auction.json")
        initial = path.read_bytes()
        self.batch("09:25:30", -8, {"gap": 1}, final=True)
        with patch("app.daily_validation.replay_session", side_effect=AssertionError("must not replay")):
            again = self.daily.freeze(DAY, at("16:00:00"))
        self.assertEqual(value, again)
        self.assertEqual(initial, path.read_bytes())
        self.assertEqual(len(value["engine_source_sha256"]), 64)
        self.assertEqual(len(value["batches_sha256"]), 64)
        self.assertEqual(value["scope"], "previous_limit_up_only")
        text = json.dumps(value)
        self.assertNotIn('"history"', text)
        self.assertNotIn('"raw"', text)
        self.assertEqual(value["sessions"][0]["rows"][0]["auction_pct"], 3)
        self.assertEqual(value["sessions"][1]["rows"][0]["auction_pct"], 4)

    def test_each_checkpoint_uses_its_own_latest_recorded_weights(self):
        self.seed()
        changed = dict(DEFAULT_WEIGHTS, gap=.30, amount=0)
        self.batch("09:25:30", 6, changed, final=True)
        value = self.daily.freeze(DAY, at("09:27:00"))
        earlier, later = value["sessions"]
        self.assertEqual(earlier["weights"]["gap"], DEFAULT_WEIGHTS["gap"])
        self.assertEqual(later["weights"]["gap"], .3)
        self.assertEqual(earlier["strategy_provenance"]["weight_changes"], 0)
        self.assertEqual(later["strategy_provenance"]["weight_changes"], 1)
        self.assertEqual(later["strategy_provenance"]["weights_source"], "batch")

    def test_missing_batch_weights_fall_back_only_to_manifest_with_notice(self):
        self.seed(False)
        value = self.daily.freeze(DAY, at("09:27:00"))
        self.assertTrue(all(session["strategy_provenance"]["legacy_fallback"] for session in value["sessions"]))
        self.assertTrue(any("清单权重" in warning for warning in value["warnings"]))

    def test_absent_manifest_batches_or_original_weights_never_creates_fake_file(self):
        self.assertEqual(self.daily.get(DAY)["status"], "unavailable")
        self.assertEqual(self.daily.freeze(DAY, at("09:27:00"))["status"], "unavailable")
        self.store.freeze_manifest({key: value for key, value in manifest().items() if key != "weights"})
        self.assertEqual(self.daily.freeze(DAY, at("09:27:00"))["status"], "unavailable")
        self.batch("09:24:40")
        self.assertEqual(self.daily.freeze(DAY, at("09:27:00"))["status"], "unavailable")
        self.assertFalse((self.daily.root / (DAY + "-auction.json")).exists())

    def test_postclose_label_creates_separate_version_and_preserves_scores(self):
        self.seed()
        frozen = self.daily.freeze(DAY, at("09:27:00"))
        before = (self.daily.root / (DAY + "-auction.json")).read_bytes()
        original_report = report()
        report_copy = copy.deepcopy(original_report)
        value = self.daily.label(original_report, at("15:11:00"))
        self.assertEqual(value["status"], "ready")
        self.assertEqual(original_report, report_copy)
        for old, new in zip(frozen["sessions"], value["sessions"]):
            self.assertEqual(old["rows"][0]["score"], new["rows"][0]["score"])
            self.assertEqual(old["rows"][0]["factors"], new["rows"][0]["factors"])
            self.assertIsNone(old["rows"][0]["label"])
            self.assertTrue(new["rows"][0]["label"])
            self.assertEqual(new["evaluation"]["precision_pct"], 100)
        self.assertEqual(before, (self.daily.root / (DAY + "-auction.json")).read_bytes())
        self.assertTrue((self.daily.root / (DAY + "-" + value["id"] + ".md")).exists())
        self.assertEqual(value, self.daily.get(DAY))

    def test_repeated_label_idempotent_new_evidence_preserves_all_versions(self):
        self.seed()
        first = self.daily.label(report(), at("15:11:00"))
        repeated = self.daily.label(report(), at("15:12:00"))
        self.assertEqual(first, repeated)
        newer = report([])
        newer["generated_at"] = at("15:13:00").isoformat()
        second = self.daily.label(newer, at("15:14:00"))
        self.assertNotEqual(first["id"], second["id"])
        self.assertFalse(second["sessions"][0]["rows"][0]["label"])
        self.assertTrue((self.daily.root / (DAY + "-" + first["id"] + ".json")).exists())
        self.assertEqual(self.daily.get(DAY)["id"], second["id"])

    def test_missing_or_duplicate_pool_future_report_and_intraday_are_unknown(self):
        self.seed()
        cases = [report([row(), row()]), {**report(), "generated_at": at("15:09:59").isoformat()},
                 {**report(), "generated_at": at("16:00:00").isoformat()},
                 {**report(), "raw": {"calendar": [PREVIOUS, DAY], "pools_by_date": {}}},
                 {**report(), "raw": {"calendar": [PREVIOUS], "pools_by_date": {DAY: []}}}]
        for source in cases:
            with self.subTest(source=source):
                value = self.daily.label(source, at("15:15:00"))
                self.assertEqual(value["status"], "partial")
                self.assertFalse(value["outcome_verified"])
                self.assertIsNone(value["sessions"][0]["rows"][0]["label"])

    def test_cancelled_or_premature_operations_do_not_publish_partial_files(self):
        self.seed()
        with self.assertRaises(ValueError):
            self.daily.freeze(DAY, at("09:26:59"))
        with self.assertRaises(ValueError):
            self.daily.freeze(DAY, at("09:27:00"), should_stop=lambda: True)
        self.assertFalse((self.daily.root / (DAY + "-auction.json")).exists())
        with self.assertRaises(ValueError):
            self.daily.label(report(), at("15:09:59"))

    def test_demo_report_is_rejected(self):
        self.seed()
        with self.assertRaises(ValueError):
            self.daily.label({**report(), "mode": "demo"}, at("15:15:00"))

    def test_original_decision_preserves_scores_factors_quality_despite_changed_replay(self):
        self.seed()
        recorded = self.decision()
        original = copy.deepcopy(recorded)

        def changed_replay(*args, **kwargs):
            session = replay_session(*args, **kwargs)
            for row in session["rows"]:
                row["score"] = 0
                row["factors"]["gap"]["score"] = 0
            return session

        with patch.object(self.store, "decision", create=True, side_effect=lambda day, checkpoint: recorded if checkpoint == "09:24:50" else None), \
                patch("app.daily_validation.replay_session", side_effect=changed_replay):
            value = self.daily.freeze(DAY, at("09:27:00"))
        session = value["sessions"][0]
        self.assertEqual(session["method"], "recorded_ranking")
        for key in ("score", "factors", "quality"):
            self.assertEqual(session["rows"][0][key], original["rows"][0][key])
        self.assertEqual(session["engine_source_sha256"], "a" * 64)
        self.assertEqual(session["strategy_provenance"]["weights_source"], "decision")
        self.assertEqual(recorded, original)
        recorded["rows"][0]["score"] = 99
        with patch.object(self.store, "decision", create=True, return_value=recorded):
            self.assertEqual(value, self.daily.freeze(DAY, at("16:00:00")))

    def test_future_observation_in_recorded_decision_cannot_enter_cutoff(self):
        self.seed()
        recorded = self.decision()
        recorded["rows"][0]["updated_at"] = at("09:24:51").isoformat()
        recorded["rows"][0]["score"] = 99
        with patch.object(self.store, "decision", create=True, return_value=recorded):
            value = self.daily.freeze(DAY, at("09:27:00"))
        self.assertEqual(value["sessions"][0]["method"], "replayed")
        self.assertNotEqual(value["sessions"][0]["rows"][0]["score"], 99)
        self.assertTrue(any("校验失败" in warning for warning in value["sessions"][0]["warnings"]))

    def test_actual_decision_weights_override_last_batch_weights(self):
        self.seed()
        recorded = self.decision(weights={"gap": 1})
        with patch.object(self.store, "decision", create=True, side_effect=lambda day, checkpoint: recorded if checkpoint == "09:24:50" else None):
            value = self.daily.freeze(DAY, at("09:27:00"))
        session = value["sessions"][0]
        self.assertEqual(session["weights"]["gap"], 1)
        self.assertTrue(session["strategy_provenance"]["decision_differs_from_last_batch_weights"])
        self.assertEqual(session["rows"][0]["score"], recorded["rows"][0]["score"])

    def test_future_capture_is_not_trusted_but_legitimate_delayed_capture_is(self):
        self.seed()
        recorded = self.decision()
        recorded["captured_at"] = at("09:28:00").isoformat()
        with patch.object(self.store, "decision", create=True, return_value=recorded):
            value = self.daily.freeze(DAY, at("09:27:00"))
        self.assertEqual(value["sessions"][0]["method"], "replayed")

    def test_missing_original_candidate_is_not_backfilled_from_replayed_rows(self):
        self.seed()
        recorded = self.decision()
        recorded["rows"] = []
        with patch.object(self.store, "decision", create=True, side_effect=lambda day, checkpoint: recorded if checkpoint == "09:24:50" else None):
            value = self.daily.freeze(DAY, at("09:27:00"))
        session = value["sessions"][0]
        self.assertEqual(session["method"], "recorded_ranking")
        self.assertIsNone(session["rows"][0]["score"])
        self.assertEqual(session["quality"]["observed_count"], 0)
        self.assertEqual(session["quality"]["unobserved_count"], 1)

    def test_bad_batch_integrity_stays_ineligible_after_labeling(self):
        self.seed()
        self.batch("09:24:10", 9)
        value = self.daily.label(report(), at("15:15:00"))
        self.assertFalse(value["sessions"][0]["quality"]["eligible_for_optimization"])
        self.assertGreater(value["sessions"][0]["quality"]["engine_out_of_order_responses"], 0)

    def test_listing_json_and_markdown_exports_keep_factors_and_escape_names(self):
        self.seed()
        value = self.daily.label(report(), at("15:15:00"))
        self.assertEqual(len(self.daily.list()), 1)
        name, data = self.daily.export(DAY, format="json")
        self.assertTrue(name.endswith(".json"))
        self.assertEqual(data["id"], value["id"])
        self.assertEqual(set(data["sessions"][0]["rows"][0]["factors"]), set(DEFAULT_WEIGHTS))
        name, text = self.daily.export(DAY, format="markdown")
        self.assertTrue(name.endswith(".md"))
        self.assertIn("09:24:50", text)
        self.assertIn("当日涨停池成员", text)
        with self.assertRaises(ValueError):
            self.daily.export(DAY, format="html")
        with self.assertRaises(ValueError):
            self.daily.get("../config")


if __name__ == "__main__":
    unittest.main()
