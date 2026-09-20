"""Meaningful malformed/missing evidence tests, with no runtime data writes."""
import copy
import json
import math
import unittest

from app.sentiment import build_sentiment


DAYS = ["2026-09-16", "2026-09-17", "2026-09-18"]


def stock(code="000001.SZ", text="首板", **values):
    return {"thscode": code, "name": "测试", "continue_day_text": text,
            "seal_money": 50, "max_seal_money": 100, "limit_up_reason": "官方原因A", **values}


def report(rows=None, *, pools=None, calendar=None):
    rows = [stock()] if rows is None else rows
    return {"date": DAYS[-1], "mode": "live", "status": "ready",
            "raw": {"calendar": DAYS if calendar is None else calendar,
                    "pools_by_date": {DAYS[0]: [], DAYS[1]: [], DAYS[2]: copy.deepcopy(rows)} if pools is None else pools},
            "limit_up": {"status": "ready", "count": len(rows), "rows": copy.deepcopy(rows)}}


class SentimentTests(unittest.TestCase):
    def test_complete_structure_and_exclusive_buckets(self):
        rows = [stock("000001.SZ", "首板"), stock("600001.SH", "2连板"),
                stock("600002.SH", "3连板"), stock("600003.SH", "4连板"),
                stock("600004.SH", "8连板")]
        # A one-session retained window permits explicit heights; every code is counted once.
        result = build_sentiment(report(rows, calendar=[DAYS[-1]], pools={DAYS[-1]: rows}))
        today = result["matrix"]["rows"][-1]
        self.assertEqual(today["buckets"], {"1": 1, "2": 1, "3": 1, "4": 1, "5+": 1, "unknown": 0})
        self.assertEqual(sum(today["buckets"].values()), today["limit_up_count"])
        self.assertEqual(today["max_consecutive"], 8)
        self.assertEqual(result["status"], "ready")

    def test_missing_pool_keeps_nulls_not_zero(self):
        sample = report()
        sample["raw"]["pools_by_date"][DAYS[1]] = None
        result = build_sentiment(sample)
        missing = result["matrix"]["rows"][1]
        self.assertIsNone(missing["limit_up_count"])
        self.assertTrue(all(value is None for value in missing["buckets"].values()))
        self.assertEqual(result["matrix"]["coverage"]["complete_days"], 2)
        self.assertEqual(result["matrix"]["coverage"]["coverage_pct"], 66.67)

    def test_successful_empty_pool_is_zero_but_ratios_unknown(self):
        result = build_sentiment(report([]))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["matrix"]["rows"][-1]["limit_up_count"], 0)
        self.assertEqual(sum(result["matrix"]["rows"][-1]["buckets"].values()), 0)
        self.assertEqual(result["retention"]["valid_count"], 0)
        self.assertIsNone(result["retention"]["median_pct"])
        self.assertIsNone(result["retention"]["below_50_pct"])
        self.assertEqual(result["reasons"]["known_count"], 0)

    def test_missing_current_rows_not_empty_statistics(self):
        sample = report()
        sample["limit_up"] = {"status": "unavailable", "count": None, "rows": None}
        result = build_sentiment(sample)
        for key in ("valid_count", "missing_count", "invalid_count", "above_100_count", "below_50_count"):
            self.assertIsNone(result["retention"][key])
        self.assertIsNone(result["reasons"]["known_count"])
        self.assertEqual(result["retention"]["status"], "unavailable")

    def test_nonconsecutive_label_does_not_become_four_boards(self):
        rows = [stock(text="5天4板", continue_day_cnt=4)]
        pools = {DAYS[0]: [], DAYS[1]: rows, DAYS[2]: rows}
        result = build_sentiment(report(rows, pools=pools))
        today = result["matrix"]["rows"][-1]
        self.assertEqual(today["buckets"]["2"], 1)
        self.assertEqual(today["buckets"]["4"], 0)
        self.assertEqual(today["lower_bound_count"], 0)
        self.assertTrue(any("N天M板" in text for text in today["warnings"]))

    def test_window_boundary_height_is_lower_bound(self):
        rows = [stock(text="", continue_day_cnt=9)]
        result = build_sentiment(report(rows, pools={day: rows for day in DAYS}))
        today = result["matrix"]["rows"][-1]
        self.assertEqual(today["max_consecutive"], 3)
        self.assertEqual(today["lower_bound_count"], 1)
        self.assertEqual(today["lower_bound_by_bucket"]["3"], 1)
        self.assertEqual(today["status"], "partial")

    def test_missing_prior_pool_stops_run_and_marks_lower_bound(self):
        rows = [stock(text="")]
        result = build_sentiment(report(rows, pools={DAYS[0]: rows, DAYS[1]: None, DAYS[2]: rows}))
        today = result["matrix"]["rows"][-1]
        self.assertEqual(today["max_consecutive"], 1)
        self.assertEqual(today["lower_bound_by_bucket"]["1"], 1)

    def test_conflicting_label_uses_complete_pool_records(self):
        rows = [stock(text="8连板")]
        result = build_sentiment(report(rows))
        today = result["matrix"]["rows"][-1]
        self.assertEqual(today["max_consecutive"], 1)
        self.assertTrue(any("冲突" in text for text in today["warnings"]))

    def test_noninteger_heights_not_taken_from_raw_or_precomputed(self):
        rows = [stock(text="2.5连板", continue_day_cnt=2.5, consecutive_days=2.5)]
        result = build_sentiment(report(rows, calendar=[]))
        self.assertEqual(result["matrix"]["rows"][-1]["buckets"]["unknown"], 1)
        self.assertIsNone(result["matrix"]["rows"][-1]["max_consecutive"])

    def test_without_calendar_do_not_claim_dates_are_adjacent(self):
        rows = [stock(text="5天4板")]
        result = build_sentiment(report(rows, calendar=[], pools={DAYS[0]: rows, DAYS[-1]: rows}))
        self.assertFalse(result["matrix"]["coverage"]["calendar_verified"])
        self.assertEqual(result["matrix"]["rows"][-1]["buckets"]["unknown"], 1)
        self.assertTrue(result["matrix"]["warnings"])

    def test_invalid_future_and_nontrading_dates_are_excluded(self):
        sample = report()
        sample["raw"]["pools_by_date"].update({"2026-09-19": [stock()], "not-a-date": [stock()], "2026-09-13": [stock()]})
        result = build_sentiment(sample)
        self.assertEqual([row["date"] for row in result["matrix"]["rows"]], DAYS)
        self.assertIn("排除3", result["matrix"]["warnings"][0])

    def test_window_bounded_at_ten_days(self):
        calendar = [f"2026-09-{day:02}" for day in range(1, 19)]
        result = build_sentiment(report([], calendar=calendar, pools={day: [] for day in calendar}))
        self.assertEqual(len(result["matrix"]["rows"]), 10)
        self.assertEqual(result["matrix"]["rows"][0]["date"], "2026-09-09")

    def test_duplicate_codes_do_not_double_count_or_prove_complete_pool(self):
        sample = report([stock(), stock()])
        result = build_sentiment(sample)
        row = result["matrix"]["rows"][-1]
        self.assertEqual(row["coverage"]["observed_count"], 1)
        self.assertEqual(row["coverage"]["duplicate_count"], 1)
        self.assertIsNone(row["limit_up_count"])
        self.assertIsNone(result["retention"]["total_count"])
        self.assertEqual(result["reasons"]["rows"][0]["count"], 1)

    def test_bad_codes_do_not_form_market_matrix(self):
        sample = report([stock(code="000001"), stock(code="000001.SZ"), {"thscode": 1}, None])
        result = build_sentiment(sample)
        row = result["matrix"]["rows"][-1]
        self.assertIsNone(row["limit_up_count"])
        self.assertEqual(row["coverage"]["invalid_count"], 3)
        self.assertEqual(row["coverage"]["observed_count"], 1)

    def test_retention_excludes_negative_zero_nonfinite_strings_and_above100(self):
        pairs = [(0, 100), (25, 100), (50, 100), (100, 100), (101, 100),
                 (-1, 100), (1, 0), (1, -1), (True, 100), (math.inf, 100),
                 (math.nan, 100), ("1", 100), (None, 100), (1, None)]
        rows = [stock(f"600{index:03}.SH", seal_money=seal, max_seal_money=peak) for index, (seal, peak) in enumerate(pairs)]
        result = build_sentiment(report(rows))["retention"]
        self.assertEqual(result["valid_count"], 4)
        self.assertEqual(result["median_pct"], 37.5)
        self.assertEqual(result["below_50_count"], 2)
        self.assertEqual(result["below_50_pct"], 50)
        self.assertEqual(result["missing_count"], 2)
        self.assertEqual(result["invalid_count"], 7)
        self.assertEqual(result["above_100_count"], 1)
        self.assertEqual(result["coverage_pct"], 28.57)
        self.assertEqual(result["valid_count"] + result["missing_count"] + result["invalid_count"] + result["above_100_count"], len(rows))

    def test_retention_threshold_uses_unrounded_ratio(self):
        result = build_sentiment(report([stock(seal_money=49.99999)]))["retention"]
        self.assertEqual(result["below_50_count"], 1)

    def test_retention_large_numbers_cannot_emit_nonfinite_json(self):
        rows = [stock(seal_money=10 ** 500, max_seal_money=1),
                stock("600001.SH", seal_money=1e308, max_seal_money=1e-308)]
        result = build_sentiment(report(rows))
        self.assertEqual(result["retention"]["invalid_count"], 1)
        self.assertEqual(result["retention"]["above_100_count"], 1)
        json.dumps(result, allow_nan=False)

    def test_conflicting_duplicate_cannot_choose_a_convenient_reason_or_seal(self):
        rows = [stock(), stock(seal_money=100, limit_up_reason="其他原因")]
        result = build_sentiment(report(rows))
        self.assertEqual(result["retention"]["valid_count"], 0)
        self.assertEqual(result["retention"]["excluded_conflict_count"], 1)
        self.assertEqual(result["reasons"]["known_count"], 0)
        self.assertEqual(result["reasons"]["rows"], [])
        self.assertEqual(result, build_sentiment(report(list(reversed(rows)))))

    def test_public_rows_and_original_pool_disagreement_not_full_coverage(self):
        sample = report()
        sample["raw"]["pools_by_date"][DAYS[-1]] = [stock("600001.SH")]
        result = build_sentiment(sample)
        self.assertIsNone(result["retention"]["total_count"])
        self.assertIsNone(result["reasons"]["coverage_pct"])

    def test_reason_group_uses_exact_official_text_and_bounded_stable_sort(self):
        rows = [stock(f"600{index:03}.SH", limit_up_reason="AI+光伏；官方完整说明") for index in range(14)]
        rows += [stock(f"601{index:03}.SH", limit_up_reason=f"原因{index:02}") for index in range(15)]
        result = build_sentiment(report(rows))["reasons"]
        self.assertEqual(result["known_count"], 29)
        self.assertEqual(result["group_count"], 16)
        self.assertEqual(result["displayed_count"], 12)
        self.assertEqual(result["rows"][0]["reason"], "AI+光伏；官方完整说明")
        self.assertEqual(result["rows"][0]["count"], 14)
        self.assertEqual(len(result["rows"][0]["codes"]), 10)
        self.assertEqual(result["rows"][0]["other_code_count"], 4)
        reversed_result = build_sentiment(report(list(reversed(rows))))["reasons"]
        self.assertEqual(result, reversed_result)

    def test_reason_missing_does_not_fall_back_to_tag_or_name(self):
        rows = [stock(limit_up_reason=None, tag="半导体", reason="行业", name="AI概念")]
        result = build_sentiment(report(rows))["reasons"]
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["known_count"], 0)
        self.assertEqual(result["missing_count"], 1)
        self.assertEqual(result["rows"], [])
        self.assertTrue(any("不从名称" in warning for warning in result["warnings"]))

    def test_reason_missing_and_long_text_coverage_explicit(self):
        rows = [stock(), stock("600001.SH", limit_up_reason=" "),
                stock("600002.SH", limit_up_reason="x" * 4001)]
        result = build_sentiment(report(rows))["reasons"]
        self.assertEqual(result["coverage_pct"], 33.33)
        self.assertEqual(result["missing_count"], 1)
        self.assertEqual(result["invalid_count"], 1)
        self.assertEqual(result["rows"][0]["share_pct"], 100)

    def test_unknown_mode_and_invalid_date_reject_data(self):
        for change in ({"mode": None}, {"mode": "other"}, {"date": "2026-02-30"}, {"date": "2026-9-18"}):
            sample = report()
            sample.update(change)
            result = build_sentiment(sample)
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["matrix"]["rows"], [])
            self.assertIsNone(result["retention"]["valid_count"])

    def test_demo_mode_explicitly_propagated(self):
        sample = report()
        sample["mode"] = "demo"
        result = build_sentiment(sample)
        self.assertEqual(result["mode"], "demo")
        self.assertTrue(any("DEMO" in text for text in result["warnings"]))

    def test_count_mismatch_does_not_offer_full_pool_coverage(self):
        sample = report()
        sample["limit_up"]["count"] = 200
        result = build_sentiment(sample)
        self.assertIsNone(result["retention"]["total_count"])
        self.assertIsNone(result["retention"]["coverage_pct"])
        self.assertEqual(result["retention"]["observed_count"], 1)
        self.assertEqual(result["retention"]["status"], "partial")

    def test_current_date_mismatch_rejects_statistics(self):
        sample = report()
        sample["limit_up"]["date"] = DAYS[0]
        result = build_sentiment(sample)
        self.assertIsNone(result["retention"]["valid_count"])
        self.assertIsNone(result["reasons"]["known_count"])
        self.assertTrue(any("日期与报告日期不符" in text for text in result["warnings"]))

    def test_input_is_not_mutated(self):
        sample = report()
        before = copy.deepcopy(sample)
        build_sentiment(sample)
        self.assertEqual(sample, before)


if __name__ == "__main__":
    unittest.main()
