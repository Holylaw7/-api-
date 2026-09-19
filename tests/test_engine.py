import math
import unittest
from datetime import datetime, timedelta

from app.engine import AuctionEngine, SHANGHAI, normalize_auction_metadata


def at(clock="09:15:00", date="2026-09-18"):
    return datetime.fromisoformat(f"{date}T{clock}").replace(tzinfo=SHANGHAI)


def stock(code="000001.SZ", **changes):
    row = {
        "thscode": code, "name": "样本股", "auction_pct": 2.0,
        "auction_amount": 10_000_000, "auction_price": 10.2,
        "auction_turnover_pct": .2, "auction_volume_ratio": 2,
        "auction_volume": 1_000_000, "auction_unmatched": 800,
    }
    row.update(changes)
    return row


def batch(now, rows=None, **changes):
    result = {
        "timestamp": int(now.timestamp() * 1000), "auction_phase": "live",
        "data_status": "ready", "item": [stock()] if rows is None else rows,
    }
    result.update(changes)
    return result


class EngineTests(unittest.TestCase):
    def test_each_batch_is_immediately_ranked_without_waiting_for_universe(self):
        engine = AuctionEngine()
        first = engine.ingest(batch(at()), at())
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["rank"], 1)
        self.assertIsNotNone(first[0]["score"])
        second = engine.ingest(batch(at("09:15:03"), [stock("600000.SH", auction_pct=7)]), at("09:15:03"))
        self.assertEqual(len(second), 2)
        self.assertEqual(second[0]["thscode"], "600000.SH")

    def test_phase_boundary_and_peak_amount_retention(self):
        engine = AuctionEngine()
        for clock, amount in [("09:15:00", 10_000_000), ("09:19:59", 20_000_000)]:
            rows = engine.ingest(batch(at(clock), [stock(auction_amount=amount)]), at(clock))
            self.assertEqual(rows[0]["phase"], "cancellable")
            self.assertIsNone(rows[0]["factors"]["retention"]["value"])
        row = engine.ingest(batch(at("09:20:00")), at("09:20:00"))[0]
        self.assertEqual(row["phase"], "locked")
        self.assertEqual(row["factors"]["retention"]["value"], .5)
        self.assertEqual(row["factors"]["retention"]["score"], 50)

    def test_no_late_trend_until_three_buckets_and_thirty_seconds(self):
        engine = AuctionEngine()
        for seconds in [0, 10, 20]:
            now = at("09:20:00") + timedelta(seconds=seconds)
            row = engine.ingest(batch(now, [stock(auction_pct=2 + seconds / 60)]), now)[0]
            self.assertIsNone(row["factors"]["late_momentum"]["score"])
        now = at("09:20:30")
        row = engine.ingest(batch(now, [stock(auction_pct=2.5)]), now)[0]
        self.assertAlmostEqual(row["factors"]["late_momentum"]["value"], 1)
        self.assertAlmostEqual(row["factors"]["late_momentum"]["score"], 75)

    def test_early_prices_do_not_create_late_momentum(self):
        engine = AuctionEngine()
        for clock, pct in [("09:18:00", -5), ("09:19:00", 5), ("09:20:00", 8)]:
            row = engine.ingest(batch(at(clock), [stock(auction_pct=pct)]), at(clock))[0]
        self.assertIsNone(row["factors"]["late_momentum"]["score"])

    def test_delayed_final_never_backdates_into_momentum(self):
        engine = AuctionEngine()
        for clock, pct in [("09:24:00", 1), ("09:24:20", 2), ("09:24:40", 3)]:
            row = engine.ingest(batch(at(clock), [stock(auction_pct=pct)]), at(clock))[0]
        expected = row["factors"]["late_momentum"]["value"]
        final_at = at("09:25:05")
        row = engine.ingest(batch(final_at, [stock(auction_pct=-5)], auction_phase="final"), final_at)[0]
        self.assertEqual(row["phase"], "final")
        self.assertEqual(row["factors"]["late_momentum"]["value"], expected)
        self.assertEqual(row["auction_pct"], -5)
        self.assertEqual(engine.history["000001.SZ"][-1]["received_at"], final_at.isoformat())

    def test_final_before_0925_is_rejected(self):
        engine = AuctionEngine()
        self.assertEqual(engine.ingest(batch(at(), auction_phase="final"), at()), [])
        self.assertEqual(engine.summary()["rejected_records"], 1)

    def test_actual_closed_final_metadata_is_accepted_and_preserved(self):
        engine = AuctionEngine()
        now = at("09:25:03")
        data = batch(now, auction_phase="closed", data_status="final")
        row = engine.ingest(data, now)[0]
        self.assertEqual(row["phase"], "final")
        self.assertEqual(row["data_status"], "ready")
        self.assertEqual(row["raw_auction_phase"], "closed")
        self.assertEqual(row["raw_data_status"], "final")
        self.assertIsNotNone(row["score"])
        observation = engine.history["000001.SZ"][-1]
        self.assertEqual(observation["raw_auction_phase"], "closed")
        self.assertEqual(observation["raw_data_status"], "final")
        self.assertEqual(data["auction_phase"], "closed")
        self.assertEqual(data["data_status"], "final")

    def test_actual_final_metadata_cannot_be_used_before_0925(self):
        engine = AuctionEngine()
        now = at("09:24:59")
        self.assertEqual(engine.ingest(batch(now, auction_phase="closed", data_status="final"), now), [])
        self.assertEqual(normalize_auction_metadata({"auction_phase":"final", "data_status":"final"}, now)["data_status"], "not_ready")

    def test_metadata_allowlist_does_not_guess_unknown_status_or_phase(self):
        for phase, status in [("mystery", "ready"), ("live", "mystery"),
                              ("live", "final"), ("closed", "live"),
                              (None, "ready"), ("final", "not_ready")]:
            with self.subTest(phase=phase, status=status):
                metadata = normalize_auction_metadata({"auction_phase":phase, "data_status":status}, at("09:25:00"))
                self.assertEqual(metadata["data_status"], "not_ready")

    def test_live_status_is_accepted_only_with_explicit_live_phase(self):
        engine = AuctionEngine()
        row = engine.ingest(batch(at(), data_status="live"), at())[0]
        self.assertEqual(row["data_status"], "ready")
        self.assertEqual(row["raw_data_status"], "live")
        self.assertIsNotNone(row["score"])

    def test_same_payload_replay_does_not_manufacture_observations(self):
        engine = AuctionEngine()
        data = batch(at())
        engine.ingest(data, at())
        row = engine.ingest(data, at("09:15:10"))[0]
        self.assertEqual(row["quality"]["observation_count"], 1)
        self.assertEqual(row["quality"]["last_receipt_age_seconds"], 10)
        self.assertEqual(engine.summary()["duplicate_responses"], 1)

    def test_old_payload_after_not_ready_does_not_reset_observation_age(self):
        engine = AuctionEngine()
        original = batch(at())
        engine.ingest(original, at())
        engine.ingest(batch(at("09:15:10"), [], data_status="not_ready", _requested_codes=["000001.SZ"]), at("09:15:10"))
        row = engine.ingest(original, at("09:15:40"))[0]
        self.assertEqual(row["quality"]["observation_count"], 1)
        self.assertEqual(row["quality"]["last_receipt_age_seconds"], 40)
        self.assertIsNone(row["score"])

    def test_unchanged_new_responses_are_observations_with_freshness_unknown(self):
        engine = AuctionEngine()
        for second in range(10):
            now = at() + timedelta(seconds=second)
            row = engine.ingest(batch(now), now)[0]
        self.assertEqual(row["quality"]["observation_count"], 10)
        self.assertEqual(row["quality"]["changed_observations"], 1)
        self.assertTrue(any("连续快照" in flag for flag in row["quality"]["flags"]))
        self.assertEqual(row["quality"]["upstream_freshness"], "unknown")

    def test_missing_invalid_and_zero_are_distinct(self):
        engine = AuctionEngine()
        now = at()
        row = engine.ingest(batch(now, [stock(auction_amount=None, auction_pct=math.nan,
                                               auction_turnover_pct=-1, auction_volume_ratio=0)]), now)[0]
        self.assertIsNone(row["auction_amount"])
        self.assertIsNone(row["auction_pct"])
        self.assertIsNone(row["auction_turnover_pct"])
        self.assertEqual(row["factors"]["volume_ratio"]["score"], 0)
        self.assertIsNone(row["score"])
        self.assertIsNone(row["rank"])
        self.assertTrue(any("无效数值" in flag for flag in row["quality"]["flags"]))

    def test_not_ready_does_not_insert_zero_or_advance_history(self):
        engine = AuctionEngine()
        engine.ingest(batch(at()), at())
        now = at("09:15:05")
        row = engine.ingest(batch(now, [stock(auction_amount=0)], data_status="not_ready"), now)[0]
        self.assertEqual(row["auction_amount"], 10_000_000)
        self.assertEqual(row["quality"]["observation_count"], 1)
        self.assertIsNone(row["score"])
        self.assertEqual(row["quality"]["status"], "not_ready")
        now = at("09:15:10")
        row = engine.ingest(batch(now), now)[0]
        self.assertIsNotNone(row["score"])

    def test_empty_not_ready_invalidates_only_requested_batch(self):
        engine = AuctionEngine()
        engine.ingest(batch(at(), [stock(), stock("600000.SH")]), at())
        now = at("09:15:05")
        rows = engine.ingest(batch(now, [], data_status="not_ready", _requested_codes=["000001.SZ"]), now)
        values = {row["thscode"]: row for row in rows}
        self.assertIsNone(values["000001.SZ"]["score"])
        self.assertIsNotNone(values["600000.SH"]["score"])

    def test_local_receipt_and_assembly_not_reported_as_quote_time(self):
        engine = AuctionEngine()
        row = engine.ingest(batch(at()), at("09:15:01"))[0]
        self.assertEqual(row["updated_at"], at("09:15:01").isoformat())
        self.assertEqual(row["response_assembled_at"], at().isoformat())
        self.assertIsNone(row["upstream_quote_at"])

    def test_stale_receipt_excluded_from_rankings(self):
        engine = AuctionEngine()
        engine.ingest(batch(at()), at())
        row = engine.rankings(at("09:15:31"))[0]
        self.assertIsNone(row["score"])
        self.assertIsNotNone(row["raw_score"])
        self.assertEqual(row["quality"]["status"], "stale")

    def test_late_start_and_large_gaps_do_not_claim_full_ten_minutes(self):
        engine = AuctionEngine()
        engine.ingest(batch(at("09:15:00")), at("09:15:00"))
        row = engine.ingest(batch(at("09:24:59")), at("09:24:59"))[0]
        self.assertEqual(row["quality"]["covered_seconds"], 30)
        self.assertEqual(row["quality"]["window_coverage"], .05)

    def test_previous_context_and_future_context_have_different_effects(self):
        good = AuctionEngine()
        context = {"000001.SZ": {"continue_day_cnt": 3, "continue_day_text": "3连板", "context_date": "2026-09-17"}}
        row = good.ingest(batch(at()), at(), context)[0]
        self.assertEqual(row["continue_day_cnt"], 3)
        self.assertEqual(row["factors"]["continuity"]["score"], 60)
        bad = AuctionEngine()
        context["000001.SZ"]["context_date"] = "2026-09-18"
        row = bad.ingest(batch(at()), at(), context)[0]
        self.assertIsNone(row["continue_day_cnt"])
        self.assertTrue(any("未来涨停池" in warning for warning in bad.summary()["warnings"]))

    def test_new_session_resets_previous_observations_and_context(self):
        engine = AuctionEngine()
        engine.ingest(batch(at()), at(), {"000001.SZ": {"continue_day_cnt": 3, "continue_day_text": "3连板", "context_date": "2026-09-17"}})
        tomorrow = at(date="2026-09-21")
        row = engine.ingest(batch(tomorrow), tomorrow)[0]
        self.assertEqual(row["quality"]["observation_count"], 1)
        self.assertIsNone(row["continue_day_cnt"])
        self.assertEqual(engine.summary()["session_date"], "2026-09-21")
        engine.ingest(batch(at()), at())
        self.assertEqual(engine.summary()["session_date"], "2026-09-21")
        self.assertEqual(engine.summary()["observation_count"], 1)

    def test_cannot_retroactively_request_past_ranking(self):
        engine = AuctionEngine()
        engine.ingest(batch(at("09:20:00")), at("09:20:00"))
        with self.assertRaises(ValueError):
            engine.rankings(at("09:15:00"))

    def test_late_old_response_cannot_override_latest_symbol(self):
        engine = AuctionEngine()
        engine.ingest(batch(at("09:15:10"), [stock(auction_pct=5)]), at("09:15:10"))
        row = engine.ingest(batch(at("09:15:05"), [stock(auction_pct=-5)]), at("09:15:11"))[0]
        self.assertEqual(row["auction_pct"], 5)
        self.assertEqual(row["quality"]["observation_count"], 1)

    def test_response_date_mismatch_is_rejected(self):
        engine = AuctionEngine()
        rows = engine.ingest(batch(at(date="2026-09-17")), at())
        self.assertEqual(rows, [])
        self.assertTrue(any("日期不一致" in warning for warning in engine.summary()["warnings"]))

    def test_unmatched_buy_direction_never_used_in_score(self):
        outputs = []
        for unmatched in [None, 1e10, -1e10]:
            engine = AuctionEngine()
            outputs.append(engine.ingest(batch(at(), [stock(auction_unmatched=unmatched)]), at())[0]["score"])
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])

    def test_zero_early_amount_does_not_divide_by_zero_or_fake_retention(self):
        engine = AuctionEngine()
        engine.ingest(batch(at(), [stock(auction_amount=0)]), at())
        row = engine.ingest(batch(at("09:20:00")), at("09:20:00"))[0]
        self.assertIsNone(row["factors"]["retention"]["value"])

    def test_rankings_return_copies_and_weight_edits_apply_immediately(self):
        engine = AuctionEngine()
        rows = engine.ingest(batch(at()), at())
        rows[0]["auction_pct"] = 500
        engine.weights = {"gap": 1}
        row = engine.rankings()[0]
        self.assertEqual(row["auction_pct"], 2)
        self.assertEqual(row["score"], 60)

    def test_invalid_weights_and_naive_times_are_rejected(self):
        for weights in [{"gap": -1}, {"gap": math.nan}, {"gap": True}, {"gap": 0}, {"unknown": 1},
                        {"gap": 1e308, "amount": 1e308}]:
            with self.subTest(weights=weights), self.assertRaises(ValueError):
                AuctionEngine(weights)
        with self.assertRaises(ValueError):
            AuctionEngine().ingest({}, datetime(2026, 9, 18, 9, 15))

    def test_multiday_boards_are_not_mistaken_for_consecutive_boards(self):
        for label, count, expected in [("5天4板", 4, None), ("4连板", 4, 4),
                                       ("首板", 1, 1), ("3天3板", 3, 3),
                                       ("3连板", 4, None), (None, 4, None)]:
            with self.subTest(label=label):
                engine = AuctionEngine()
                context = {"000001.SZ": {"continue_day_text": label, "continue_day_cnt": count,
                                           "context_date": "2026-09-17"}}
                row = engine.ingest(batch(at()), at(), context)[0]
                self.assertEqual(row["continue_day_cnt"], expected)

    def test_exact_45_percent_coverage_is_eligible_despite_float_rounding(self):
        engine = AuctionEngine()
        context = {"000001.SZ": {"continue_day_text": "首板", "continue_day_cnt": 1, "context_date": "2026-09-17"}}
        row = engine.ingest(batch(at(), [stock(auction_turnover_pct=None, auction_volume_ratio=None)]), at(), context)[0]
        self.assertEqual(row["quality"]["factor_coverage"], .45)
        self.assertIsNotNone(row["score"])

    def test_invalidated_context_removes_cached_continuity(self):
        engine = AuctionEngine()
        old = {"000001.SZ": {"continue_day_text": "3连板", "continue_day_cnt": 3,
                                "context_date": "2026-09-17"}}
        engine.ingest(batch(at()), at(), old)
        old["000001.SZ"]["context_date"] = "2026-09-18"
        rows = engine.ingest(batch(at("09:15:01"), [stock("600000.SH")]), at("09:15:01"), old)
        row = next(row for row in rows if row["thscode"] == "000001.SZ")
        self.assertIsNone(row["continue_day_cnt"])

    def test_stale_unconfirmed_final_preserves_explicit_provisional_score(self):
        engine = AuctionEngine()
        engine.ingest(batch(at("09:24:59")), at("09:24:59"))
        row = engine.rankings(at("09:26:00"))[0]
        self.assertIsNone(row["score"])
        self.assertIsNotNone(row["provisional_score"])
        self.assertTrue(any("待核验快照" in flag for flag in row["quality"]["flags"]))


if __name__ == "__main__":
    unittest.main()
