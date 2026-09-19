import copy
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app.provider import APIError, SHANGHAI, date_ms
from app.review import _market_summary, _price_trend, _snapshot_alignment, build_review, consecutive_info, number


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 9, 19, 16, tzinfo=SHANGHAI)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


def stock(code, text="首板", count=1, **extra):
    return {"thscode": code, "name": f"测试{code}", "continue_day_text": text, "continue_day_cnt": count,
            "seal_money": 100_000_000, "max_seal_money": 200_000_000, "limit_up_time": "09:35",
            **extra}


class FixtureProvider:
    def __init__(self):
        self.days = ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11",
                     "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]
        self.pools = {day: [] for day in self.days}
        self.pools.update({
            "2026-09-15": [stock("600001.SH")],
            "2026-09-16": [stock("600001.SH", "2连板", 2), stock("600002.SH")],
            "2026-09-17": [stock("600001.SH", "3连板", 3), stock("600002.SH", "2连板", 2),
                           stock("600004.SH", "5天4板", 4)] + [stock(f"601{i:03}.SH") for i in range(10)],
            "2026-09-18": [stock("600001.SH", "4连板", 4), stock("600002.SH", "3连板", 3),
                           stock("600003.SH"), stock("600004.SH", "5天4板", 4)],
        })
        self.calls = []
        self.failures = set()
        self.snapshot_date = "2026-09-18"

    def calendar(self):
        return self.days[:]

    def pool(self, kind, date):
        self.calls.append(("pool", kind, date))
        if (kind, date) in self.failures:
            raise APIError("上游数据尚未就绪", code=3002)
        if kind == "limit-up":
            return copy.deepcopy(self.pools.get(date, []))
        if kind == "limit-break":
            return [{"thscode": "602001.SH", "open_times": 3}]
        return []

    def market(self):
        self.calls.append(("market",))
        return {"timestamp": date_ms(self.snapshot_date) + 15 * 3600_000,
                "total": 4, "item": [{"thscode": f"60000{index}.SH", "price_change_ratio_pct": change,
                                       "turnover": 100 + index} for index, change in enumerate([1, -1, 0, None])]}

    def ladder(self):
        return {"window": {"board_caps": {"two_board": 4}},
                "item": [{"date": "20260918", "boards": {"three_board": [{"thscode": "600002.SH"}]}}]}

    def catalog(self, tag):
        prefix = 881000 if tag == "industry" else 886000
        return [{"thscode": f"{prefix + index}.TI", "name": f"{tag}{index}"} for index in range(40)]

    def indices(self, codes):
        self.calls.append(("indices", list(codes)))
        return {"timestamp": date_ms(self.snapshot_date) + 15 * 3600_000,
                "item": [{"thscode": code, "price_change_ratio_pct": int(code[:6]) % 7,
                           "turnover": int(code[:6]) * 1000} for code in codes]}

    def members(self, code):
        self.calls.append(("members", code))
        return [{"thscode": "600001.SH"}, {"thscode": "600003.SH"}, {"thscode": "600005.SH"}]

    def index_historical(self, code, start, end):
        self.calls.append(("index_historical", code, start, end))
        prior = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=1)).date().isoformat()
        return {"item": [{"date_ms": date_ms(prior), "close_price": 100, "turnover": 1000},
                         {"date_ms": date_ms(end), "close_price": 102, "turnover": 2000}]}

    def historical(self, code, start, end, *, adjust="none"):
        self.calls.append(("historical", code, start, end, adjust))
        return {"adjust": adjust,
                "item": [{"date_ms": date_ms(day), "close_price": 100 + index, "volume": 1000 + index * 100}
                         for index, day in enumerate(self.days) if start <= day <= end]}


@patch("app.review.datetime", FixedDateTime)
class ReviewTests(unittest.TestCase):
    def test_complete_pool_promotion_not_capped_ladder(self):
        fixture = FixtureProvider()
        progress = []
        report = build_review(fixture, "2026-09-18", "2026-09-17", progress.append)
        promotion = report["limit_up"]["promotion"]
        self.assertEqual(promotion["previous_count"], 13)
        self.assertEqual(promotion["promoted_count"], 3)
        self.assertEqual(promotion["rate_pct"], 23.08)
        self.assertEqual(len(report["trend"]["rows"]), 10)
        self.assertEqual(sum(call[:2] == ("pool", "limit-up") for call in fixture.calls), 10)
        self.assertTrue(progress)
        self.assertEqual(report["raw"]["pools_by_date"]["2026-09-17"], fixture.pools["2026-09-17"])

    def test_days_boards_label_not_blindly_consecutive(self):
        report = build_review(FixtureProvider(), "2026-09-18", "2026-09-17")
        row = next(row for row in report["limit_up"]["rows"] if row["thscode"] == "600004.SH")
        self.assertEqual(row["continue_day_cnt"], 4)
        self.assertEqual(row["consecutive_days"], 2)
        self.assertTrue(any("不等于" in warning for warning in row["quality"]))
        self.assertEqual(report["limit_up"]["max_consecutive"], 4)
        self.assertEqual(report["limit_up"]["consecutive_count"], 3)

    def test_missing_current_pool_not_empty_zero(self):
        fixture = FixtureProvider()
        fixture.failures.add(("limit-up", "2026-09-18"))
        report = build_review(fixture, "2026-09-18", "2026-09-17")
        self.assertEqual(report["status"], "partial")
        self.assertIsNone(report["limit_up"]["rows"])
        self.assertIsNone(report["market"]["limit_up_count"])
        self.assertIsNone(report["limit_up"]["promotion"]["rate_pct"])
        self.assertIsNone(report["trend"]["rows"][-1]["limit_up_count"])
        self.assertIsNone(report["sectors"]["rows"][0]["limit_up_count"])

    def test_missing_previous_pool_disables_promotion(self):
        fixture = FixtureProvider()
        fixture.failures.add(("limit-up", "2026-09-17"))
        report = build_review(fixture, "2026-09-18", "2026-09-17")
        self.assertEqual(report["limit_up"]["count"], 4)
        self.assertIsNone(report["limit_up"]["promotion"]["previous_count"])
        self.assertIsNone(report["limit_up"]["promotion"]["rate_pct"])
        self.assertEqual(report["trend"]["status"], "partial")

    def test_all_sectors_ranked_members_only30_and_no_fake_flow(self):
        fixture = FixtureProvider()
        report = build_review(fixture, "2026-09-18", "2026-09-17")
        sectors = report["sectors"]
        self.assertEqual(len(sectors["rows"]), 80)
        self.assertEqual(sectors["coverage"]["ranked_count"], 80)
        self.assertEqual(sectors["coverage"]["member_attribution_count"], 30)
        self.assertEqual(sum(call[0] == "members" for call in fixture.calls), 30)
        self.assertIsNone(sectors["net_flow"])
        self.assertEqual(sectors["net_flow_status"], "unavailable")
        self.assertTrue(all(row["net_flow"] is None for row in sectors["rows"]))
        self.assertEqual(sectors["rows"][0]["limit_up_count"], 2)
        self.assertIsNone(sectors["rows"][-1]["limit_up_count"])
        self.assertEqual(sectors["rows"][0]["factors"].keys(), {"price_strength", "turnover_participation"})

    def test_historical_date_does_not_use_current_market_or_members(self):
        fixture = FixtureProvider()
        report = build_review(fixture, "2026-09-17", "2026-09-16")
        self.assertEqual(report["market"]["status"], "date_mismatch")
        self.assertIsNone(report["market"]["advancing"])
        self.assertEqual(report["market"]["snapshot_date"], "2026-09-18")
        sectors = report["sectors"]
        self.assertTrue(sectors["coverage"]["historical_sample"])
        self.assertEqual(sectors["coverage"]["ranked_count"], 30)
        self.assertEqual(sectors["coverage"]["member_attribution_count"], 0)
        self.assertEqual(sum(call[0] == "members" for call in fixture.calls), 0)
        self.assertEqual(sum(call[0] == "indices" for call in fixture.calls), 0)
        history = [call for call in fixture.calls if call[0] == "index_historical"]
        self.assertEqual(len(history), 30)
        self.assertEqual(history[0][1], "881000.TI")
        self.assertTrue(all(row["limit_up_count"] is None for row in sectors["rows"]))

    def test_quote_null_changes_not_unchanged(self):
        report = build_review(FixtureProvider(), "2026-09-18", "2026-09-17")
        market = report["market"]
        self.assertEqual(market["advancing"], 1)
        self.assertEqual(market["declining"], 1)
        self.assertEqual(market["unchanged"], 1)
        self.assertEqual(market["missing_change"], 1)
        self.assertEqual(market["valid_count"], 3)

    def test_wrong_previous_day_corrected_from_calendar(self):
        report = build_review(FixtureProvider(), "2026-09-18", "2026-09-16")
        self.assertEqual(report["limit_up"]["promotion"]["previous_count"], 13)
        self.assertTrue(any("上一交易日与交易日历不符" in warning for warning in report["warnings"]))

    def test_missing_strength_fields_stay_null_with_coverage(self):
        fixture = FixtureProvider()
        fixture.pools["2026-09-18"][0].update(seal_money=None, max_seal_money=None, limit_up_time=None)
        report = build_review(fixture, "2026-09-18", "2026-09-17")
        row = next(row for row in report["limit_up"]["rows"] if row["thscode"] == "600001.SH")
        self.assertIsNone(row["factors"]["seal_size"])
        self.assertIsNone(row["factors"]["seal_retention"])
        self.assertEqual(row["factor_coverage_pct"], 25)
        self.assertTrue(row["quality"])

    def test_empty_successful_pool_is_real_zero(self):
        fixture = FixtureProvider()
        fixture.pools["2026-09-18"] = []
        report = build_review(fixture, "2026-09-18", "2026-09-17")
        self.assertEqual(report["limit_up"]["count"], 0)
        self.assertEqual(report["limit_up"]["rows"], [])
        self.assertEqual(report["limit_up"]["promotion"]["promoted_count"], 0)
        self.assertEqual(report["limit_up"]["promotion"]["rate_pct"], 0)

    def test_mixed_page_dates_not_merged_into_historical_breadth(self):
        market = FixtureProvider().market()
        market["page_timestamps"] = [date_ms("2026-09-17"), date_ms("2026-09-18")]
        result = _market_summary(market, "2026-09-18")
        self.assertEqual(result["status"], "date_mismatch")
        self.assertIsNone(result["total_turnover"])

    def test_unvalidated_numeric_consecutive_count_is_not_used(self):
        row = stock("600000.SH", "5天4板", 4)
        info = consecutive_info(row, "2026-09-18", ["2026-09-18"], {"2026-09-18": {"600000.SH": row}})
        self.assertEqual(info["days"], 1)
        self.assertTrue(info["lower_bound"])

    def test_nonfinite_and_bool_numbers_rejected(self):
        self.assertIsNone(number(float("nan")))
        self.assertIsNone(number(float("inf")))
        self.assertIsNone(number(True))

    def test_price_history_forward_adjustment_and_bounded_selection(self):
        fixture = FixtureProvider()
        fixture.pools["2026-09-18"] += [stock(f"603{index:03}.SH") for index in range(30)]
        report = build_review(fixture, "2026-09-18", "2026-09-17")
        calls = [call for call in fixture.calls if call[0] == "historical"]
        self.assertEqual(len(calls), 20)
        self.assertTrue(all(call[-1] == "forward" for call in calls))
        self.assertTrue(all(call[-2] == "2026-09-18" for call in calls))
        self.assertEqual(report["trend"]["price_coverage"]["requested"], 20)
        self.assertEqual(len(report["raw"]["stock_histories"]), 20)
        self.assertIsNone(report["limit_up"]["rows"][20]["price_trend"])

    def test_nontrading_snapshot_inference_is_explicit_and_warnings_dedup(self):
        fixture = FixtureProvider()
        fixture.days = _long_calendar()
        fixture.snapshot_date = "2026-09-19"
        report = build_review(fixture, "2026-09-18", "2026-09-17")
        self.assertEqual(report["status"], "partial")
        market = report["market"]
        self.assertEqual(market["snapshot_date"], "2026-09-19")
        self.assertEqual(market["effective_trade_date"], "2026-09-18")
        self.assertEqual(market["date_basis"], "inferred_latest_closed_session")
        self.assertFalse(market["date_verified"])
        self.assertEqual(market["data_status"], "provisional")
        self.assertEqual(market["advancing"], 1)
        self.assertEqual(report["sectors"]["coverage"]["ranked_count"], 80)
        self.assertEqual(report["sectors"]["coverage"]["inferred_date_count"], 80)
        self.assertEqual(report["sectors"]["status"], "partial")
        self.assertFalse(report["sectors"]["date_verified"])
        self.assertTrue(all(row["timestamp"] == date_ms("2026-09-19") + 15 * 3600_000 for row in report["sectors"]["rows"]))
        self.assertEqual(len(report["warnings"]), len(set(report["warnings"])))


def _long_calendar():
    result, day = [], datetime(2026, 8, 3)
    while day.date().isoformat() <= "2026-09-18":
        if day.weekday() < 5:
            result.append(day.date().isoformat())
        day += timedelta(days=1)
    return result


class SnapshotAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 19, 16, tzinfo=SHANGHAI)
        self.timestamp = date_ms("2026-09-19") + 15 * 3600_000
        self.calendar = _long_calendar()

    def check(self, *, date="2026-09-18", timestamp=None, calendar=None, pages=None, now=None):
        return _snapshot_alignment(self.timestamp if timestamp is None else timestamp, date,
                                   calendar=self.calendar if calendar is None else calendar,
                                   now=now or self.now, page_timestamps=pages)

    def test_sole_latest_closed_day_nontrading_exception(self):
        result = self.check()
        self.assertTrue(result["accepted"])
        self.assertFalse(result["date_verified"])
        self.assertEqual(result["data_status"], "provisional")
        self.assertEqual(result["snapshot_timestamp"], self.timestamp)

    def test_older_requested_day_never_uses_exception(self):
        self.assertFalse(self.check(date="2026-09-17")["accepted"])

    def test_trading_today_never_uses_exception(self):
        self.assertFalse(self.check(calendar=self.calendar + ["2026-09-19"])["accepted"])

    def test_missing_or_incomplete_calendar_not_nontrading_evidence(self):
        for calendar in ([], self.calendar[-10:], ["2026-09-18"], self.calendar + ["invalid"]):
            self.assertFalse(self.check(calendar=calendar)["accepted"])

    def test_stale_or_future_calendar_rejected(self):
        self.assertFalse(self.check(date="2026-09-18", now=datetime(2026, 10, 5, 16, tzinfo=SHANGHAI),
                                   timestamp=date_ms("2026-10-05") + 15 * 3600_000)["accepted"])
        self.assertFalse(self.check(calendar=self.calendar + ["2026-09-21"])["accepted"])

    def test_undated_page_not_inferred(self):
        self.assertFalse(self.check(pages=[self.timestamp, None])["accepted"])
        self.assertFalse(self.check(pages=[])["accepted"])

    def test_only_target_and_today_page_dates_can_be_inferred(self):
        self.assertTrue(self.check(pages=[date_ms("2026-09-18"), self.timestamp])["accepted"])
        self.assertFalse(self.check(pages=[date_ms("2026-09-17"), self.timestamp])["accepted"])

    def test_future_timestamp_rejected(self):
        self.assertFalse(self.check(timestamp=date_ms("2026-09-19") + 17 * 3600_000)["accepted"])
        self.assertFalse(self.check(timestamp=date_ms("2026-09-20"))["accepted"])

    def test_exact_date_does_not_require_inference_calendar(self):
        result = self.check(timestamp=date_ms("2026-09-18"), calendar=[])
        self.assertTrue(result["accepted"])
        self.assertTrue(result["date_verified"])
        self.assertEqual(result["date_basis"], "snapshot_timestamp")


class PriceTrendTests(unittest.TestCase):
    def setUp(self):
        self.days = []
        day = datetime(2026, 8, 3)
        while len(self.days) < 25:
            if day.weekday() < 5:
                self.days.append(day.date().isoformat())
            day += timedelta(days=1)
        self.bars = [{"date_ms": date_ms(day), "close_price": 100 + index, "volume": 1000}
                     for index, day in enumerate(self.days)]
        self.bars[-1]["volume"] = 2000

    def calculate(self, bars=None):
        return _price_trend({"adjust": "forward", "item": self.bars if bars is None else bars},
                            self.days[-1], self.days[0], self.days)

    def test_full_price_trend_values(self):
        result = self.calculate()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["ma5"], 122)
        self.assertEqual(result["ma10"], 119.5)
        self.assertEqual(result["ma20"], 114.5)
        self.assertAlmostEqual(result["return_5d_pct"], (124 / 119 - 1) * 100, places=4)
        self.assertEqual(result["volume_ratio_5d"], 2)
        self.assertEqual(result["max_drawdown_20d_pct"], 0)
        self.assertTrue(result["above_ma20"])

    def test_future_and_out_of_window_bars_ignored(self):
        bars = self.bars + [{"date_ms": date_ms("2027-01-01"), "close_price": 9999, "volume": 9_999_999}]
        result = self.calculate(bars)
        self.assertEqual(result["close"], 124)
        self.assertEqual(result["ma5"], 122)
        self.assertEqual(result["bar_count"], 25)
        self.assertTrue(any("未来日线不参与" in warning for warning in result["warnings"]))

    def test_missing_session_does_not_pull_older_bar_into_ma(self):
        result = self.calculate(self.bars[:-3] + self.bars[-2:])
        self.assertIsNone(result["ma5"])
        self.assertIsNone(result["ma20"])
        self.assertIsNone(result["volume_ratio_5d"])
        self.assertIsNotNone(result["return_5d_pct"])

    def test_null_price_and_volume_remain_unknown(self):
        bars = copy.deepcopy(self.bars)
        bars[-1]["close_price"] = None
        bars[-1]["volume"] = None
        result = self.calculate(bars)
        self.assertIsNone(result["close"])
        self.assertIsNone(result["ma5"])
        self.assertIsNone(result["return_5d_pct"])
        self.assertIsNone(result["volume_ratio_5d"])
        self.assertIsNone(result["above_ma5"])

    def test_no_target_date_is_not_latest_quote_fallback(self):
        result = self.calculate(self.bars[:-1])
        self.assertEqual(result["status"], "date_mismatch")
        self.assertIsNone(result["close"])
        self.assertEqual(result["as_of"], self.days[-2])

    def test_drawdown_uses_running_peak_then_lower_close(self):
        bars = copy.deepcopy(self.bars)
        bars[-4]["close_price"] = 200
        bars[-3]["close_price"] = 100
        result = self.calculate(bars)
        self.assertEqual(result["max_drawdown_20d_pct"], 50)

    def test_short_history_retains_shorter_valid_windows(self):
        result = self.calculate(self.bars[-7:])
        self.assertIsNotNone(result["ma5"])
        self.assertIsNone(result["ma10"])
        self.assertIsNone(result["ma20"])
        self.assertIsNone(result["max_drawdown_20d_pct"])

    def test_conflicting_adjustment_refused(self):
        result = _price_trend({"adjust": "none", "item": self.bars}, self.days[-1], self.days[0], self.days)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["close"])


if __name__ == "__main__":
    unittest.main()
