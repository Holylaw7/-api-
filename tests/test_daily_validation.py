import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app import engine as engine_module
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

    def seed_observed_upstream_names(self):
        """Batches exactly as the 2026-09-21 authenticated session returned them."""
        self.store.freeze_manifest(manifest())
        weights = dict(DEFAULT_WEIGHTS)
        for clock, phase, pct in (("09:16:00", "order_entry", 1), ("09:24:00", "no_cancel", 2),
                                  ("09:24:40", "no_cancel", 3)):
            stamp = at(clock)
            self.store.batch(DAY, "live", stamp.isoformat(), "live", {
                "timestamp": int(stamp.timestamp() * 1000), "auction_phase": phase, "data_status": "live",
                "item": [{"thscode": CODE, "name": "样本股", "auction_pct": pct, "auction_amount": 20_000_000,
                          "auction_turnover_pct": .3, "auction_volume_ratio": 3}],
                "_strategy_weights": weights})
        stamp = at("09:25:05")
        self.store.batch(DAY, "live", stamp.isoformat(), "final", {
            "timestamp": int(stamp.timestamp() * 1000), "auction_phase": "matched", "data_status": "final",
            "item": [{"thscode": CODE, "name": "样本股", "auction_pct": 4, "auction_amount": 20_000_000,
                      "auction_turnover_pct": .3, "auction_volume_ratio": 3}],
            "_strategy_weights": weights})

    @staticmethod
    def freeze_as_pre_fix_engine(daily, clock="09:27:00"):
        """Freeze while upstream names are still outside the allowlist."""
        current = engine_module.normalize_auction_metadata

        def older(data, received_at):
            metadata = current(data, received_at)
            if data.get("auction_phase") in ("order_entry", "no_cancel", "matched"):
                metadata.update(data_status="not_ready", auction_phase="unknown")
            return metadata

        with patch("app.engine.normalize_auction_metadata", side_effect=older):
            return daily.freeze(DAY, at(clock))

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

    def test_late_final_snapshot_tool_refuses_other_dates_and_renders_differences(self):
        import contextlib
        import io

        from tools import late_final_snapshot

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = late_final_snapshot.main(["--date", "2026-01-05", "--data-dir", str(self.root)])
        self.assertEqual(code, 2)
        self.assertIn("没有历史日期参数", buffer.getvalue())
        self.assertFalse((self.root / "research" / "late-final").exists())
        value = {"date": DAY, "retrieved_at": at("09:40:00").isoformat(), "source": "fixture",
                 "upstream": {"auction_phase": "closed", "data_status": "final"}, "codes": 2,
                 "comparison": {"frozen_archive": "x", "differences": 1,
                                "rows": [{"thscode": CODE, "name": "样本股", "archive_pct": 9.9, "late_pct": 0.0,
                                          "delta_pp": -9.9, "archive_amount": 100, "late_amount": 200}]},
                 "definition": "仅人工核对", "warnings": ["不是原始证据"]}
        text = late_final_snapshot.render_markdown(value)
        self.assertIn("| 000001.SZ | 样本股 | 9.9 | 0.0 | -9.9 |", text)
        self.assertIn("与冻结档案不同的股票：**1** 只", text)
        self.assertIn("不是原始证据", text)

    def test_field_coverage_report_tool_summarizes_saved_batches(self):
        import contextlib
        import io

        from tools import field_coverage_report

        self.store.freeze_manifest(manifest())
        weights = dict(DEFAULT_WEIGHTS)
        for clock, with_ratio in (("09:16:00", True), ("09:24:40", False)):
            stamp = at(clock)
            item = {"thscode": CODE, "name": "样本股", "auction_pct": 2, "auction_amount": 20_000_000,
                    "auction_turnover_pct": .3}
            if with_ratio:
                item["auction_volume_ratio"] = 3
            self.store.batch(DAY, "live", stamp.isoformat(), "live",
                             {"timestamp": int(stamp.timestamp() * 1000), "auction_phase": "live",
                              "data_status": "live", "item": [item], "_strategy_weights": weights})
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = field_coverage_report.main(["--date", DAY, "--database", str(self.root / "market.sqlite")])
        text = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn(f"| {DAY} | 1（manifest_context） | 2 | 0/1 | 0/1 | 1/1 |", text)
        self.assertIn("否（缺 auction_volume_ratio）", text)
        self.assertIn("只读 batches + 盘前清单", text)

    def test_frozen_archive_audits_raw_field_availability(self):
        self.store.freeze_manifest(manifest())
        weights = dict(DEFAULT_WEIGHTS)
        for clock, with_ratio in (("09:16:00", True), ("09:24:00", False), ("09:24:40", False)):
            stamp = at(clock)
            item = {"thscode": CODE, "name": "样本股", "auction_pct": 2, "auction_amount": 20_000_000,
                    "auction_turnover_pct": .3}
            if with_ratio:
                item["auction_volume_ratio"] = 3
            self.store.batch(DAY, "live", stamp.isoformat(), "live",
                             {"timestamp": int(stamp.timestamp() * 1000), "auction_phase": "live",
                              "data_status": "live", "item": [item], "_strategy_weights": weights})
        value = self.daily.freeze(DAY, at("09:27:00"))
        coverage = value["field_coverage"]
        self.assertEqual(coverage["batches"], 3)
        self.assertEqual(coverage["fields"]["auction_volume_ratio"]["batches_with_value"], 1)
        self.assertEqual(coverage["fields"]["auction_volume_ratio"]["first_value_at"], at("09:16:00").isoformat())
        self.assertIsNone(coverage["fields"]["open_price"]["last_value_at"])
        self.assertEqual(coverage["fields"]["auction_turnover_pct"]["batches_with_value"], 3)
        checkpoint = coverage["checkpoints"]["09:24:50"]["auction_volume_ratio"]
        self.assertEqual((checkpoint["with_value"], checkpoint["candidate_count"]), (0, 1))
        self.assertIsNone(checkpoint["last_value_at"])
        self.assertEqual(coverage["checkpoints"]["09:24:50"]["auction_turnover_pct"]["with_value"], 1)
        text = self.daily.export(DAY, format="markdown")[1]
        self.assertIn("## 原始字段可得性", text)
        self.assertIn("| auction_volume_ratio | 1 |", text)

    def test_freeze_writes_one_ranking_markdown_summary(self):
        self.seed()
        value = self.daily.freeze(DAY, at("09:27:00"))
        summary = self.daily.root / (DAY + "-auction.md")
        text = summary.read_text(encoding="utf-8")
        self.assertIn("# 每日竞价评分与收盘核验", text)
        self.assertIn("| 名次 | 股票代码 | 名称 | 竞价评分 | 因子覆盖 | 竞价涨幅% | 竞价金额（元） | 严格连板 | 观察批次 | 当日涨停池成员 |", text)
        self.assertIn("| 1 | 000001.SZ | 样本股 |", text)
        self.assertIn("前五名：1. 000001.SZ 样本股", text)
        self.assertIn("## 09:24:50", text)
        self.assertIn("| 1 | 1 | 1 | 0 | 否 | 否 |", text)
        initial = summary.read_bytes()
        self.assertEqual(value, self.daily.freeze(DAY, at("16:00:00")))
        self.assertEqual(initial, summary.read_bytes())
        name, markdown = self.daily.export(DAY, format="markdown")
        self.assertEqual(name, DAY + "-" + value["frozen_id"] + ".md")
        self.assertEqual(markdown, summary.read_text(encoding="utf-8"))

    def test_correction_rebuilds_a_session_frozen_before_the_metadata_fix(self):
        self.seed_observed_upstream_names()
        frozen = self.freeze_as_pre_fix_engine(self.daily)
        frozen_path = self.daily.root / (DAY + "-auction.json")
        before = frozen_path.read_bytes()
        self.assertEqual([session["quality"]["scored_count"] for session in frozen["sessions"]], [0, 0])
        with self.assertRaises(ValueError):
            self.daily.correct(DAY, at("09:40:00"), "短")
        value = self.daily.correct(DAY, at("09:40:00"), "上游阶段命名修复后按当日原批次重建评分")
        self.assertEqual(value["kind"], "auction_snapshot_correction")
        self.assertEqual(value["corrects_frozen_id"], frozen["frozen_id"])
        self.assertEqual(value["reason"], "上游阶段命名修复后按当日原批次重建评分")
        for session in value["sessions"]:
            self.assertEqual(session["method"], "replayed")
            self.assertEqual(session["quality"]["scored_count"], 1)
            self.assertEqual(session["supersedes"]["scored_count"], 0)
        self.assertEqual(before, frozen_path.read_bytes())
        self.assertTrue((self.daily.root / (DAY + "-" + value["id"] + ".json")).exists())
        text = (self.daily.root / (DAY + "-" + value["id"] + ".md")).read_text(encoding="utf-8")
        self.assertIn("校正原因：上游阶段命名修复后按当日原批次重建评分", text)
        self.assertIn("被校正的原冻结档案：" + frozen["frozen_id"], text)
        self.assertIn("| 1 | 000001.SZ | 样本股 |", text)
        original_text = (self.daily.root / (DAY + "-auction.md")).read_text(encoding="utf-8")
        self.assertIn(f"竞价存档：{frozen['frozen_id']}", original_text)
        self.assertIn("| — | 000001.SZ | 样本股 | — | 0% |", original_text)
        self.assertEqual(self.daily.get(DAY)["id"], value["id"])
        repeated = self.daily.correct(DAY, at("09:45:00"), "上游阶段命名修复后按当日原批次重建评分")
        self.assertEqual(repeated, value)
        self.assertEqual(len(self.daily.corrections(DAY)), 1)
        self.assertEqual(frozen_path.read_bytes(), before)
        self.assertEqual(original_text, (self.daily.root / (DAY + "-auction.md")).read_text(encoding="utf-8"))

    def test_correction_needs_a_frozen_archive_and_a_finished_session(self):
        self.seed()
        with self.assertRaises(ValueError):
            self.daily.correct(DAY, at("09:26:59"), "冻结前不能校正每日档案")
        with self.assertRaises(ValueError):
            self.daily.correct(DAY, at("09:40:00"), "没有冻结档案就不应创建版本")
        self.assertEqual(self.daily.corrections(DAY), [])
        self.assertFalse(list(self.daily.root.glob(DAY + "-*.json")))

    def test_morning_ranking_view_stays_separate_from_the_frozen_archive(self):
        session = {"engine_source_sha256": "b" * 64, "weights": dict(DEFAULT_WEIGHTS),
                   "strategy_provenance": {"weights_source": "live_engine",
                                           "weights_at": at("09:26:00").isoformat()},
                   "rows": [
                       {"thscode": CODE, "name": "样本股", "score": 71.5, "provisional_score": None,
                        "previous_limit_up": True,
                        "rank": 1, "auction_pct": 3.0, "auction_amount": 20_000_000, "continue_day_cnt": 2,
                        "phase": "final", "quality": {"factor_coverage": 1.0, "observation_count": 5}},
                       {"thscode": "600519.SH", "name": "缺失终态", "score": None, "provisional_score": 55.0,
                        "previous_limit_up": False,
                        "rank": None, "auction_pct": 4.0, "auction_amount": 30_000_000, "continue_day_cnt": None,
                        "phase": "locked", "quality": {"factor_coverage": .9, "observation_count": 40}}],
                   "warnings": ["本机已接收 2 个原始批次，其中上游未就绪 0 个；终态覆盖 1/2 只。"]}
        with self.assertRaises(ValueError):
            self.daily.publish_morning_ranking(DAY, at("09:24:00"), session)
        with self.assertRaises(ValueError):
            self.daily.publish_morning_ranking(DAY, at("09:26:00"), {**session, "rows": []})
        value = self.daily.publish_morning_ranking(DAY, at("09:26:02"), session)
        self.assertEqual(value["kind"], "live_ranking_view")
        self.assertEqual(value["status"], "live_snapshot")
        path = self.daily.morning_ranking_path(DAY)
        text = path.read_text(encoding="utf-8")
        self.assertIn("# 早盘竞价排名", text)
        self.assertIn("## 09:26:00 实时排名", text)
        self.assertIn("| 1 | 000001.SZ | 样本股 | 是 | 71.5 | — |", text)
        self.assertIn("| — | 600519.SH | 缺失终态 | 否 | — | 55.0 |", text)
        self.assertIn("09:20后", text)
        self.assertIn("不是不可变证据", text)
        self.assertIn("| 2 | 2 | 1 | 0 | 否 | 否 |", text)
        self.assertIn("范围：本机实时采集 2 只，其中昨日涨停候选 1 只", text)
        self.assertEqual(text.count("本机已接收 2 个原始批次"), 1)
        self.assertFalse((self.daily.root / (DAY + "-auction.json")).exists())
        self.assertFalse((self.daily.root / (DAY + "-auction.md")).exists())
        self.assertEqual([], self.daily.list())
        refreshed = self.daily.publish_morning_ranking(
            DAY, at("09:26:30"), {**session, "rows": [{**session["rows"][0], "score": 60.0}]})
        self.assertEqual(refreshed["generated_at"], at("09:26:30").isoformat())
        self.assertIn("| 1 | 000001.SZ | 样本股 | 是 | 60.0 | — |", path.read_text(encoding="utf-8"))
        self.assertFalse(list(self.daily.root.glob(DAY + "-*.json")))

    def test_label_after_correction_keeps_labels_on_rebuilt_scores(self):
        self.seed_observed_upstream_names()
        frozen = self.freeze_as_pre_fix_engine(self.daily)
        correction = self.daily.correct(DAY, at("09:40:00"), "元数据归一化修复后重建当日评分")
        frozen_path = self.daily.root / (DAY + "-auction.json")
        before = frozen_path.read_bytes()
        value = self.daily.label(report(), at("15:11:00"))
        self.assertEqual(value["status"], "ready")
        self.assertEqual(value["scores_basis"], "correction")
        self.assertEqual(value["scores_source_id"], correction["id"])
        self.assertEqual(value["auction_frozen_id"], frozen["frozen_id"])
        for session, rebuilt in zip(value["sessions"], correction["sessions"]):
            self.assertEqual(session["rows"][0]["score"], rebuilt["rows"][0]["score"])
            self.assertIsNotNone(session["rows"][0]["score"])
            self.assertTrue(session["rows"][0]["label"])
        self.assertTrue(any("校正版本" in warning for warning in value["warnings"]))
        self.assertEqual(before, frozen_path.read_bytes())
        self.assertEqual(self.daily.get(DAY)["id"], value["id"])


if __name__ == "__main__":
    unittest.main()
