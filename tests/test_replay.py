import copy
import unittest
from datetime import datetime

from app.engine import AuctionEngine, DEFAULT_WEIGHTS, SHANGHAI
from app.replay import pool_transitions, replay_session


DAY = "2026-09-18"
PREVIOUS = "2026-09-17"
CODE = "000001.SZ"
OTHER = "600000.SH"
CALENDAR = ["2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16", PREVIOUS, DAY]


def at(clock, day=DAY):
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=SHANGHAI)


def pool_row(code=CODE, count=2, label="2连板"):
    return {"thscode": code, "name": "样本股", "continue_day_text": label, "continue_day_cnt": count}


def manifest():
    return {"version": 1, "date": DAY, "previous_date": PREVIOUS, "mode": "live",
            "prepared_at": at("09:10:00").isoformat(), "point_in_time": True,
            "source": "local_preparation", "context_complete": True,
            "calendar": CALENDAR[:], "codes": [CODE],
            "context": {CODE: {**pool_row(), "context_date": PREVIOUS}}}


def outcome(day=DAY, rows=None):
    return {"date": day, "complete": True, "retrieved_at": at("15:10:00", day).isoformat(),
            "rows": [pool_row()] if rows is None else rows}


def batch(clock, pct=2, amount=10_000_000, code=CODE, day=DAY, final=False):
    stamp = at(clock, day)
    return (stamp.isoformat(), "final" if final else "live", {
        "timestamp": int(stamp.timestamp() * 1000), "auction_phase": "final" if final else "live",
        "data_status": "ready", "item": [{"thscode": code, "name": "样本股",
        "auction_pct": pct, "auction_amount": amount, "auction_turnover_pct": .3,
        "auction_volume_ratio": 3, "auction_price": 10 + pct / 10}]})


def full_batches():
    return [batch("09:16:00", amount=20_000_000), batch("09:24:00", pct=1),
            batch("09:24:20", pct=2), batch("09:24:40", pct=3)]


class ReplayTests(unittest.TestCase):
    def replay(self, **changes):
        values = {"manifest": manifest(), "batches": full_batches(), "outcome": outcome(),
                  "weights": DEFAULT_WEIGHTS}
        values.update(changes)
        return replay_session(**values)

    def test_exact_current_engine_scores_and_factors_and_no_mutation(self):
        source, inputs, label = manifest(), full_batches(), outcome()
        original = copy.deepcopy((source, inputs, label))
        engine = AuctionEngine(DEFAULT_WEIGHTS)
        for received, stage, payload in inputs:
            engine.ingest(payload, datetime.fromisoformat(received), source["context"])
        expected = engine.rankings(at("09:24:50"))[0]
        result = self.replay(manifest=source, batches=inputs, outcome=label)
        actual = result["rows"][0]
        for key in ("score", "factors", "quality", "rank", "auction_pct", "context_date"):
            self.assertEqual(actual[key], expected[key])
        self.assertIs(actual["label"], True)
        self.assertTrue(result["quality"]["eligible_for_optimization"])
        self.assertEqual((source, inputs, label), original)
        self.assertNotIn("raw", actual)
        self.assertNotIn("history", actual)

    def test_future_and_other_date_batches_never_enter_early_prediction(self):
        inputs = full_batches() + [batch("09:24:51", pct=-9), batch("09:25:10", pct=-10, final=True),
                                  batch("09:24:40", pct=9, day=PREVIOUS)]
        result = self.replay(batches=inputs)
        self.assertEqual(result["rows"][0]["auction_pct"], 3)
        self.assertEqual(result["quality"]["ignored_batches"]["after_checkpoint"], 2)
        self.assertEqual(result["quality"]["ignored_batches"]["other_date"], 1)

    def test_today_label_cannot_replace_yesterday_context_or_candidates(self):
        result = self.replay(outcome=outcome(rows=[pool_row(OTHER, 5, "5连板")]))
        self.assertEqual([row["thscode"] for row in result["rows"]], [CODE])
        self.assertFalse(result["rows"][0]["label"])
        self.assertEqual(result["rows"][0]["factors"]["continuity"]["value"], 2)

    def test_unobserved_candidates_are_kept_with_unknown_scores(self):
        source = manifest()
        source["context"][OTHER] = {**pool_row(OTHER), "context_date": PREVIOUS}
        result = self.replay(manifest=source)
        missing = next(row for row in result["rows"] if row["thscode"] == OTHER)
        self.assertIsNone(missing["score"])
        self.assertEqual(missing["quality"]["status"], "unobserved")
        self.assertIn("采集范围", missing["missing_reason"])
        self.assertEqual(result["quality"]["candidate_count"], 2)
        self.assertEqual(result["quality"]["unobserved_count"], 1)
        self.assertTrue(result["quality"]["eligible_for_optimization"])

    def test_noncandidate_quote_cannot_enter_rankings(self):
        source = manifest()
        source["codes"].append(OTHER)
        result = self.replay(manifest=source, batches=full_batches() + [batch("09:24:50", code=OTHER, pct=10)])
        self.assertEqual([row["thscode"] for row in result["rows"]], [CODE])

    def test_missing_incomplete_wrong_date_and_intraday_labels_are_unknown(self):
        bad = [None, {**outcome(), "complete": False}, outcome(PREVIOUS),
               {**outcome(), "retrieved_at": at("15:09:59").isoformat()},
               {**outcome(), "rows": [pool_row(), pool_row()]},
               {**outcome(), "rows": [{"thscode": "000001"}]},
               {**outcome(), "mode": "demo"}]
        for value in bad:
            with self.subTest(value=value):
                result = self.replay(outcome=value)
                self.assertIsNone(result["rows"][0]["label"])
                self.assertFalse(result["quality"]["outcome_verified"])
                self.assertFalse(result["quality"]["eligible_for_optimization"])

    def test_verified_empty_outcome_is_false_not_missing(self):
        result = self.replay(outcome=outcome(rows=[]))
        self.assertIs(result["rows"][0]["label"], False)
        self.assertTrue(result["quality"]["outcome_verified"])

    def test_late_preparation_reconstructs_but_never_optimizes(self):
        source = manifest()
        source["prepared_at"] = at("09:15:01").isoformat()
        result = self.replay(manifest=source)
        self.assertEqual(result["mode"], "reconstructed")
        self.assertIsNotNone(result["rows"][0]["score"])
        self.assertFalse(result["quality"]["eligible_for_optimization"])

    def test_explicit_external_point_in_time_import_is_usable_but_disclosed(self):
        source = manifest()
        source["source"] = "historical_import"
        result = self.replay(manifest=source)
        self.assertEqual(result["origin"], "historical_import")
        self.assertEqual(result["mode"], "live")
        self.assertTrue(result["quality"]["eligible_for_optimization"])
        self.assertTrue(any("不能认证" in warning for warning in result["warnings"]))

    def test_future_context_is_never_fed_to_engine(self):
        source = manifest()
        source["context"][CODE]["context_date"] = DAY
        result = self.replay(manifest=source)
        self.assertIsNone(result["rows"][0]["factors"]["continuity"]["score"])
        self.assertFalse(result["quality"]["context_verified"])

    def test_previous_session_requires_calendar_adjacency_and_complete_pool(self):
        for mutate in (lambda source: source.update(previous_date="2026-09-16"),
                       lambda source: source.update(context_complete=False),
                       lambda source: source.update(calendar=[PREVIOUS])):
            source = manifest()
            mutate(source)
            result = self.replay(manifest=source)
            self.assertFalse(result["quality"]["context_verified"])
            self.assertFalse(result["quality"]["eligible_for_optimization"])

    def test_demo_invalid_dates_codes_checkpoints_and_naive_preparation(self):
        for changes in ({"mode": "demo"}, {"date": "2026-9-18"}, {"codes": ["000001"]},
                        {"calendar": [DAY, DAY]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.replay(manifest={**manifest(), **changes})
        for clock in ("9:24:50", "09:60:00", "09:14:00", "15:00:00"):
            with self.subTest(clock=clock), self.assertRaises(ValueError):
                replay_session(manifest(), full_batches(), outcome(), DEFAULT_WEIGHTS, checkpoint=clock)
        result = self.replay(manifest={**manifest(), "prepared_at": "2026-09-18T09:10:00"})
        self.assertFalse(result["quality"]["eligible_for_optimization"])

    def test_batches_preserve_storage_order_and_staleness(self):
        result = self.replay(batches=[batch("09:24:40", pct=3), batch("09:24:20", pct=9)])
        self.assertEqual(result["rows"][0]["auction_pct"], 3)
        self.assertEqual(result["quality"]["engine_out_of_order_responses"], 1)
        self.assertFalse(result["quality"]["integrity_verified"])
        self.assertFalse(result["quality"]["eligible_for_optimization"])
        stale = self.replay(batches=[batch("09:24:00")])
        self.assertIsNone(stale["rows"][0]["score"])
        self.assertEqual(stale["rows"][0]["quality"]["status"], "stale")

    def test_cancellation_cannot_create_optimizable_partial_result(self):
        result = replay_session(manifest(), full_batches(), outcome(), DEFAULT_WEIGHTS, should_stop=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(result["quality"]["eligible_for_optimization"])
        self.assertEqual(result["quality"]["observed_count"], 0)

    def test_engine_rejections_disable_optimization_but_normal_duplicates_do_not(self):
        invalid = batch("09:24:45")
        invalid[2]["timestamp"] = int(at("09:24:45", PREVIOUS).timestamp() * 1000)
        result = self.replay(batches=full_batches() + [invalid])
        self.assertGreater(result["quality"]["engine_rejected_records"], 0)
        self.assertFalse(result["quality"]["eligible_for_optimization"])
        inputs = full_batches()
        result = self.replay(batches=inputs + [copy.deepcopy(inputs[-1])])
        self.assertEqual(result["quality"]["engine_duplicate_responses"], 1)
        self.assertTrue(result["quality"]["eligible_for_optimization"])

    def test_invalid_batch_and_not_ready_remain_visible(self):
        result = self.replay(batches=[("bad", "live", {}), *full_batches()])
        self.assertEqual(result["quality"]["ignored_batches"]["invalid"], 1)
        self.assertFalse(result["quality"]["eligible_for_optimization"])
        not_ready = batch("09:24:45")
        not_ready[2].update(data_status="not_ready", item=[], _requested_codes=[CODE])
        result = self.replay(batches=full_batches() + [not_ready])
        self.assertIsNone(result["rows"][0]["score"])


class PoolTransitionTests(unittest.TestCase):
    def test_adjacent_pool_baseline_strict_groups_and_weighted_rate(self):
        pools = {PREVIOUS: outcome(PREVIOUS, [pool_row(), pool_row(OTHER, 4, "5天4板")]),
                 DAY: outcome(DAY, [pool_row()])}
        result = pool_transitions(pools, CALENDAR)
        row = result["rows"][0]
        self.assertEqual(row["previous_date"], PREVIOUS)
        self.assertEqual((row["yesterday_count"], row["repeat_count"], row["repeat_rate_pct"]), (2, 1, 50))
        self.assertEqual(row["groups"]["2"]["repeat_rate_pct"], 100)
        self.assertEqual(row["groups"]["unknown"]["yesterday_count"], 1)
        self.assertEqual(row["groups"]["4"]["yesterday_count"], 0)
        self.assertEqual(result["summary"]["weighted_repeat_rate_pct"], 50)
        self.assertIn("不含竞价", result["definition"])

    def test_missing_middle_day_is_not_bridged(self):
        pools = {"2026-09-16": outcome("2026-09-16"), DAY: outcome()}
        result = pool_transitions(pools, CALENDAR)
        self.assertEqual(result["rows"], [])
        self.assertIsNone(result["summary"]["weighted_repeat_rate_pct"])

    def test_empty_previous_pool_has_unknown_rate_and_zero_count(self):
        result = pool_transitions({PREVIOUS: outcome(PREVIOUS, []), DAY: outcome()}, CALENDAR)
        self.assertEqual(result["rows"][0]["yesterday_count"], 0)
        self.assertIsNone(result["rows"][0]["repeat_rate_pct"])

    def test_five_day_occurrence_uses_only_prior_days(self):
        pools = {day: outcome(day) for day in CALENDAR}
        result = pool_transitions(pools, CALENDAR)
        background = result["rows"][-1]["prior_five_sessions"]
        self.assertTrue(background["complete"])
        self.assertEqual(background["rows"][0]["appearance_count"], 5)
        self.assertNotIn(DAY, background["dates"])
        pools["2026-09-14"]["complete"] = False
        result = pool_transitions(pools, CALENDAR)
        background = result["rows"][-1]["prior_five_sessions"]
        self.assertFalse(background["complete"])
        self.assertIsNone(background["rows"][0]["appearance_count"])
        self.assertEqual(background["rows"][0]["known_appearance_count"], 4)

    def test_invalid_date_rejected_and_nontrading_date_excluded(self):
        with self.assertRaises(ValueError):
            pool_transitions({"2026-99-99": {}}, CALENDAR)
        result = pool_transitions({"2026-09-19": outcome("2026-09-19")}, CALENDAR)
        self.assertEqual(result["rows"], [])
        self.assertIn("日历", result["skipped"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
