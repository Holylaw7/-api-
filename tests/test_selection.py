import copy
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app.provider import APIError, SHANGHAI, date_ms
from app.selection import _passes, _trend_score, build_trend_pool


class SelectionClock(datetime):
    value = datetime(2026, 9, 19, 16, tzinfo=SHANGHAI)

    @classmethod
    def now(cls, tz=None):
        return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)


class Provider:
    def __init__(self, count=1):
        self.calendar = []
        day = datetime(2026, 8, 3)
        while day.date().isoformat() <= "2026-09-18":
            if day.weekday() < 5:
                self.calendar.append(day.date().isoformat())
            day += timedelta(days=1)
        self.rows = [{"thscode": f"600{index:03}.SH", "name": f"测试股票{index}", "seal_money": 100_000 - index}
                     for index in range(count)]
        self.calls = []
        self.fail_pool = set()
        self.fail_history = set()
        self.missing_history = set()
        self.future = False
        self.metadata = []

    def pool(self, kind, date):
        self.calls.append(("pool", kind, date))
        if date in self.fail_pool:
            raise APIError("尚未就绪", code=3002)
        return copy.deepcopy(self.rows)

    def tickers(self):
        self.calls.append(("tickers",))
        return copy.deepcopy(self.metadata)

    def historical(self, code, start, end, *, adjust):
        self.calls.append(("historical", code, start, end, adjust))
        if code in self.fail_history:
            raise APIError("日线请求失败", code=5001)
        bars = [{"date_ms": date_ms(day), "close_price": 100 + index, "volume": 1000}
                for index, day in enumerate(self.calendar) if start <= day <= end]
        if code in self.missing_history:
            bars = bars[-3:]
        if self.future:
            bars.append({"date_ms": date_ms("2026-09-21"), "close_price": 99999, "volume": 99_999_999})
        return {"adjust": adjust, "item": bars}


def report_for(provider, *, date="2026-09-18", timestamp_date="2026-09-18", market_rows=None):
    return {"source": "HiThink Financial-API", "date": date,
            "generated_at": "2026-09-19T15:30:00+08:00", "completed_at": "2026-09-19T15:35:00+08:00",
            "market": {}, "raw": {"market": {"timestamp": date_ms(timestamp_date) + 15 * 3600_000,
                                                "item": market_rows or []},
                                     "pools_by_date": {day: copy.deepcopy(provider.rows) for day in provider.calendar[-5:]}}}


@patch("app.selection.datetime", SelectionClock)
class SelectionTests(unittest.TestCase):
    def setUp(self):
        SelectionClock.value = datetime(2026, 9, 19, 16, tzinfo=SHANGHAI)

    def run_pool(self, provider, **kwargs):
        return build_trend_pool(provider, "2026-09-18", provider.calendar, **kwargs)

    def test_recent_complete_pools_dedup_and_explainable_trend(self):
        provider = Provider()
        result = self.run_pool(provider)
        self.assertEqual(len([call for call in provider.calls if call[0] == "pool"]), 5)
        history = [call for call in provider.calls if call[0] == "historical"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0][-2:], ("2026-09-18", "forward"))
        self.assertEqual(result["selected_count"], 1)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["evaluated_count"], 1)
        row = result["rows"][0]
        self.assertEqual(row["score"], row["trend_score"])
        self.assertGreaterEqual(row["score"], 0)
        self.assertLessEqual(row["score"], 100)
        self.assertEqual(row["trend"]["adjust"], "forward")
        self.assertIn("MA5 > MA10 > MA20", row["reason"])
        self.assertTrue(result["coverage"]["limited_universe"])

    def test_candidate60_selection30_bounded(self):
        provider = Provider(80)
        result = self.run_pool(provider)
        self.assertEqual(result["candidate_available_count"], 80)
        self.assertEqual(result["candidate_count"], 60)
        self.assertEqual(result["evaluated_count"], 60)
        self.assertEqual(result["matched_count"], 60)
        self.assertEqual(len(result["rows"]), 30)
        self.assertEqual(len([call for call in provider.calls if call[0] == "historical"]), 60)

    def test_excludes_st_delisting_and_invalid_or_incomplete_codes(self):
        provider = Provider()
        provider.rows += [{"thscode": "600991.SH", "name": "*ST测试"},
                          {"thscode": "600992.SH", "name": "测试退"},
                          {"thscode": "600993", "name": "不带后缀"},
                          {"thscode": "600994.SH", "name": "正常名称", "is_st": True},
                          {"thscode": "600995.HK", "name": "错误交易所"},
                          {"thscode": ["600996.SH"], "name": "错误代码格式"}]
        result = self.run_pool(provider)
        self.assertEqual(result["candidate_count"], 1)
        self.assertGreater(result["coverage"]["excluded_invalid_or_st"], 0)

    def test_pool_failure_remains_partial_and_missing_not_zero_evidence(self):
        provider = Provider()
        provider.fail_pool.add("2026-09-17")
        result = self.run_pool(provider)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"]["pool_days_available"], 4)
        self.assertEqual(result["selected_count"], 1)

    def test_failed_and_insufficient_histories_not_selected(self):
        provider = Provider(3)
        provider.fail_history.add("600000.SH")
        provider.missing_history.add("600001.SH")
        result = self.run_pool(provider)
        self.assertEqual(result["evaluated_count"], 3)
        self.assertEqual(result["valid_history_count"], 1)
        self.assertEqual(result["selected_count"], 1)
        self.assertEqual(result["coverage"]["history_failures"], 1)
        self.assertEqual(result["coverage"]["insufficient_history"], 1)
        self.assertEqual(result["status"], "partial")

    def test_future_daily_bars_not_used(self):
        provider = Provider()
        provider.future = True
        result = self.run_pool(provider)
        row = result["rows"][0]
        self.assertEqual(row["trend"]["as_of"], "2026-09-18")
        self.assertEqual(row["trend"]["close"], 134)
        self.assertTrue(any("未来日线" in warning for warning in row["trend"]["warnings"]))

    def test_cancellation_stops_new_history_calls_and_keeps_finished_rows(self):
        provider = Provider(20)
        stopped = lambda: sum(call[0] == "historical" for call in provider.calls) >= 3
        result = self.run_pool(provider, should_stop=stopped)
        self.assertEqual(result["evaluated_count"], 3)
        self.assertEqual(result["selected_count"], 3)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"]["not_evaluated_count"], 17)
        self.assertTrue(result["coverage"]["cancelled_or_protected"])

    def test_protected_window_starts_no_requests(self):
        for hour, minute in [(9, 10), (9, 15), (9, 26)]:
            SelectionClock.value = datetime(2026, 9, 19, hour, minute, tzinfo=SHANGHAI)
            provider = Provider()
            result = self.run_pool(provider)
            self.assertEqual(provider.calls, [])
            self.assertEqual(result["status"], "partial")

    def test_entering_protected_window_mid_job_stops_before_next_request(self):
        provider = Provider(3)
        SelectionClock.value = datetime(2026, 9, 19, 9, 9, tzinfo=SHANGHAI)

        def progress(message):
            if "2/3" in message:
                SelectionClock.value = datetime(2026, 9, 19, 9, 10, tzinfo=SHANGHAI)

        result = self.run_pool(provider, progress=progress)
        self.assertEqual(result["evaluated_count"], 1)
        self.assertEqual(result["status"], "partial")

    def test_future_or_unclosed_target_date_rejected_before_requests(self):
        provider = Provider()
        result = build_trend_pool(provider, "2026-09-21", provider.calendar + ["2026-09-21"])
        self.assertEqual(provider.calls, [])
        self.assertEqual(result["status"], "partial")
        SelectionClock.value = datetime(2026, 9, 18, 14, tzinfo=SHANGHAI)
        result = self.run_pool(provider)
        self.assertEqual(provider.calls, [])
        self.assertEqual(result["status"], "partial")

    def test_report_active_top40_then_recent_pool_fill(self):
        provider = Provider(30)
        active = [{"thscode": f"601{index:03}.SH", "name": f"活跃{index}", "turnover": 1_000_000 - index}
                  for index in range(50)]
        report = report_for(provider, market_rows=active)
        result = self.run_pool(provider, report=report)
        histories = [call[1] for call in provider.calls if call[0] == "historical"]
        self.assertEqual(histories[:40], [row["thscode"] for row in active[:40]])
        self.assertTrue(all(code.startswith("600") for code in histories[40:]))
        self.assertEqual(result["candidate_count"], 60)
        self.assertFalse(any(call[0] == "pool" for call in provider.calls))

    def test_misaligned_report_snapshot_not_candidate_source(self):
        provider = Provider()
        report = report_for(provider, timestamp_date="2026-09-17",
                            market_rows=[{"thscode": "601001.SH", "name": "错误日活跃", "turnover": 99999}])
        result = self.run_pool(provider, report=report)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["rows"][0]["thscode"], "600000.SH")
        self.assertTrue(any("快照不满足目标日" in warning for warning in result["warnings"]))

    def test_explicit_valid_nontrading_inference_can_supply_candidates(self):
        provider = Provider()
        report = report_for(provider, timestamp_date="2026-09-19",
                            market_rows=[{"thscode": "601001.SH", "name": "暂定活跃", "turnover": 99999}])
        report["market"] = {"date_basis": "inferred_latest_closed_session", "data_status": "provisional",
                            "date_verified": False, "effective_trade_date": "2026-09-18"}
        result = self.run_pool(provider, report=report)
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any("有限日期推断" in warning for warning in result["warnings"]))

    def test_inference_flag_alone_cannot_override_wrong_date(self):
        provider = Provider()
        report = report_for(provider, timestamp_date="2026-09-21",
                            market_rows=[{"thscode": "601001.SH", "name": "错误活跃", "turnover": 99999}])
        report["market"] = {"date_basis": "inferred_latest_closed_session", "data_status": "provisional",
                            "date_verified": False, "effective_trade_date": "2026-09-18"}
        result = self.run_pool(provider, report=report)
        self.assertEqual(result["candidate_count"], 1)

    def test_snapshot_names_resolved_from_catalog_and_st_filtered(self):
        provider = Provider()
        provider.metadata = [{"thscode": "601001.SH", "name": "ST活跃"}, {"thscode": "601002.SH", "name": "正常活跃"}]
        report = report_for(provider, market_rows=[{"thscode": "601001.SH", "turnover": 99999},
                                                   {"thscode": "601002.SH", "turnover": 88888}])
        result = self.run_pool(provider, report=report)
        self.assertEqual(result["candidate_count"], 2)
        self.assertTrue(any(call[0] == "tickers" for call in provider.calls))
        self.assertFalse(any(row["thscode"] == "601001.SH" for row in result["rows"]))

    def test_score_coverage_uses_factor_weights_not_count(self):
        provider = Provider()
        original_history = provider.historical

        def missing_volume(*args, **kwargs):
            response = original_history(*args, **kwargs)
            response["item"][-1]["volume"] = None
            return response

        provider.historical = missing_volume
        result = self.run_pool(provider)
        self.assertEqual(result["selected_count"], 1)
        self.assertEqual(result["rows"][0]["trend"]["score_factor_coverage_pct"], 90)
        self.assertIsNone(result["rows"][0]["trend"]["score_factors"]["volume_confirmation"])


class ScoreTests(unittest.TestCase):
    def test_percentage_thresholds_are_percentage_points(self):
        trend = {"close": 15, "ma5": 14, "ma10": 13, "ma20": 12,
                 "return_5d_pct": 30, "max_drawdown_20d_pct": 15, "volume_ratio_5d": 2}
        self.assertTrue(_passes(trend))
        self.assertFalse(_passes({**trend, "return_5d_pct": 30.01}))
        self.assertFalse(_passes({**trend, "return_5d_pct": 0}))
        self.assertFalse(_passes({**trend, "max_drawdown_20d_pct": 15.01}))
        self.assertFalse(_passes({**trend, "ma10": 14}))
        self.assertFalse(_passes({**trend, "ma20": None}))

    def test_score_fixed_scales_missing_volume_renormalized(self):
        trend = {"ma5": 110, "ma20": 100, "return_5d_pct": 15,
                 "max_drawdown_20d_pct": 0, "volume_ratio_5d": 2}
        score, factors = _trend_score(trend)
        self.assertEqual(score, 100)
        self.assertTrue(all(value == 100 for value in factors.values()))
        score, factors = _trend_score({**trend, "volume_ratio_5d": None})
        self.assertEqual(score, 100)
        self.assertIsNone(factors["volume_confirmation"])


if __name__ == "__main__":
    unittest.main()
