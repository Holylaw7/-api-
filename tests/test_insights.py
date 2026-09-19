import copy
import unittest

from app.insights import compare_reports, readiness


def report(day, codes=None):
    codes = codes if codes is not None else ["000001.SZ"]
    return {"date": day, "mode": "live", "status": "ready",
            "market": {"status": "ready", "effective_trade_date": day, "date_verified": True,
                       "data_status": "ready", "advancing": 2000, "declining": 2100,
                       "unchanged": 0, "total_turnover": 1_000_000,
                       "limit_down_count": 0, "limit_break_count": 2, "seal_rate_pct": 50},
            "limit_up": {"status": "ready", "count": len(codes), "rows": [
                {"thscode": code, "name": code, "score": 70, "consecutive_days": 1,
                 "consecutive_lower_bound": False} for code in codes]},
            "sectors": {"status": "ready", "rows": [sector("881001.TI", day)]},
            "raw": {"calendar": ["2026-09-16", "2026-09-17", "2026-09-18"]}}


def sector(code, day, **extra):
    return {"thscode": code, "name": code, "rank": 1, "score": 80,
            "price_change_ratio_pct": 1, "turnover": 100, "effective_trade_date": day,
            "date_verified": True, "data_status": "ready", **extra}


def snapshot(day="2026-09-18", clock="09:22:00"):
    return {"mode": "live", "now": f"{day}T{clock}+08:00", "configured": True, "running": True,
            "calendar": {"dates": ["2026-09-16", "2026-09-17", "2026-09-18"],
                         "checked_on": day, "today_is_trading": day == "2026-09-18"},
            "auction": {"date": day, "universe_count": 2, "coverage": 1, "final_count": 0,
                        "summary": {"observation_count": 5, "last_received_at": f"{day}T{clock}+08:00"}},
            "stocks": {"trend_pool": {"date": "2026-09-17", "status": "ready"}}}


def check_map(result):
    return {row["id"]: row for row in result["checks"]}


class ReportComparisonTests(unittest.TestCase):
    def setUp(self):
        self.current = report("2026-09-18", ["000001.SZ", "600001.SH"])
        self.previous = report("2026-09-17", ["000001.SZ", "600002.SH"])

    def test_full_pools_compare_by_complete_code(self):
        result = compare_reports(self.current, self.previous)
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["adjacent_sessions"])
        pool = result["limit_up"]
        self.assertEqual((pool["retained_count"], pool["new_count"], pool["exited_count"]), (1, 1, 1))
        self.assertEqual(pool["retained"][0]["thscode"], "000001.SZ")
        self.assertIsNone(pool["new"][0]["previous"])
        self.assertIsNone(pool["exited"][0]["current"])

    def test_inputs_unchanged_and_output_independent(self):
        original = copy.deepcopy((self.current, self.previous))
        output = compare_reports(self.current, self.previous)
        output["limit_up"]["retained"][0]["current"]["score"] = 1
        self.assertEqual((self.current, self.previous), original)

    def test_zero_and_empty_pools_are_valid_not_missing(self):
        self.current = report("2026-09-18", [])
        self.previous = report("2026-09-17", [])
        result = compare_reports(self.current, self.previous)
        self.assertEqual(result["limit_up"]["status"], "ready")
        self.assertEqual(result["limit_up"]["retained_count"], 0)
        unchanged = next(row for row in result["market"]["rows"] if row["id"] == "unchanged")
        self.assertEqual(unchanged["delta"], 0)

    def test_invalid_or_incomplete_pool_never_infers_new_or_exit(self):
        variants = [None, [], [{"thscode": "000001"}], [{"thscode": "000001.SZ"}] * 2,
                    [{"thscode": "000001.SZ"}, None]]
        for rows in variants:
            with self.subTest(rows=rows):
                current = copy.deepcopy(self.current)
                current["limit_up"]["rows"] = rows
                pool = compare_reports(current, self.previous)["limit_up"]
                self.assertEqual(pool["status"], "unavailable")
                self.assertIsNone(pool["new_count"])
                self.assertIsNone(pool["exited_count"])
        self.current["limit_up"]["status"] = "partial"
        self.assertEqual(compare_reports(self.current, self.previous)["limit_up"]["status"], "unavailable")

    def test_missing_or_mismatching_modes_are_rejected(self):
        for mode in [None, "demo", "unknown"]:
            self.previous["mode"] = mode
            result = compare_reports(self.current, self.previous)
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["market"]["rows"], [])

    def test_reversed_equal_and_invalid_dates_are_rejected(self):
        for day in ["2026-09-18", "2026-09-19", "2026-02-30", "2026-9-17", None]:
            self.previous["date"] = day
            self.assertEqual(compare_reports(self.current, self.previous)["status"], "unavailable")

    def test_non_adjacent_reports_do_not_claim_daily_continuity(self):
        self.previous = report("2026-09-16")
        result = compare_reports(self.current, self.previous)
        self.assertFalse(result["adjacent_sessions"])
        self.assertTrue(any("未证实为相邻交易日" in message for message in result["warnings"]))
        self.assertEqual(result["limit_up"]["retained_count"], 1)
        self.current.pop("raw")
        self.assertIsNone(compare_reports(self.current, self.previous)["adjacent_sessions"])

    def test_strict_consecutive_only_uses_verified_field_and_keeps_lower_bound(self):
        row = self.current["limit_up"]["rows"][0]
        row.update(consecutive_days=None, continue_day_cnt=4, continue_day_text="5天4板")
        result = compare_reports(self.current, self.previous)
        value = result["limit_up"]["retained"][0]["current"]
        self.assertIsNone(value["consecutive_days"])
        row.update(consecutive_days=2, consecutive_lower_bound=True)
        result = compare_reports(self.current, self.previous)
        self.assertTrue(result["limit_up"]["retained"][0]["current"]["consecutive_lower_bound"])
        maximum = next(row for row in result["market"]["rows"] if row["id"] == "max_consecutive")
        self.assertEqual(maximum["status"], "provisional")

    def test_snapshot_dates_missing_never_generate_changes(self):
        self.current["market"].pop("effective_trade_date")
        self.current["sectors"]["rows"][0]["effective_trade_date"] = "2026-09-17"
        result = compare_reports(self.current, self.previous)
        advancing = next(row for row in result["market"]["rows"] if row["id"] == "advancing")
        self.assertEqual(advancing["status"], "unavailable")
        self.assertIsNone(advancing["delta"])
        row = result["sectors"]["rows"][0]
        self.assertEqual(row["status"], "unavailable")
        for key in ("rank_change", "change_delta_pp", "turnover_change_pct"):
            self.assertIsNone(row[key])

    def test_provisional_alignment_is_explicit_in_every_affected_row(self):
        self.current["market"]["date_verified"] = False
        self.current["sectors"]["rows"][0]["date_verified"] = False
        result = compare_reports(self.current, self.previous)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["sectors"]["rows"][0]["status"], "provisional")
        self.assertEqual(result["market"]["rows"][0]["status"], "provisional")

    def test_sector_deltas_and_zero_baseline(self):
        row = self.current["sectors"]["rows"][0]
        row.update(rank=2, price_change_ratio_pct=-1, turnover=0)
        prior = self.previous["sectors"]["rows"][0]
        prior.update(rank=5, price_change_ratio_pct=3, turnover=100)
        result = compare_reports(self.current, self.previous)["sectors"]["rows"][0]
        self.assertEqual((result["rank_change"], result["change_delta_pp"], result["turnover_change_pct"]), (3, -4, -100))
        prior["turnover"] = 0
        self.assertIsNone(compare_reports(self.current, self.previous)["sectors"]["rows"][0]["turnover_change_pct"])

    def test_sector_matching_is_full_before_thirty_row_output_cap(self):
        self.current["sectors"]["rows"] = [sector(f"881{i:03}.TI", "2026-09-18", price_change_ratio_pct=i) for i in range(60)]
        self.previous["sectors"]["rows"] = [sector(f"881{i:03}.TI", "2026-09-17") for i in range(60)]
        result = compare_reports(self.current, self.previous)["sectors"]
        self.assertEqual(result["coverage"]["common_count"], 60)
        self.assertEqual(result["coverage"]["comparable_count"], 60)
        self.assertEqual(len(result["rows"]), 30)
        self.assertEqual(result["rows"][0]["thscode"], "881059.TI")

    def test_invalid_numbers_do_not_become_zero(self):
        self.current["market"]["total_turnover"] = float("nan")
        self.current["sectors"]["rows"][0].update(turnover=-1, price_change_ratio_pct=True, rank=float("inf"))
        result = compare_reports(self.current, self.previous)
        turnover = next(row for row in result["market"]["rows"] if row["id"] == "total_turnover")
        self.assertIsNone(turnover["current"])
        self.assertIsNone(turnover["delta"])
        row = result["sectors"]["rows"][0]
        self.assertIsNone(row["current_turnover"])
        self.assertIsNone(row["change_delta_pp"])
        self.assertIsNone(row["current_rank"])


class ReadinessTests(unittest.TestCase):
    def test_trading_session_ready_from_local_state_only(self):
        value = snapshot()
        result = readiness(value, {"prepared_date": "2026-09-18"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["checked_at"], value["now"])

    def test_unknown_calendar_not_classified_from_weekday(self):
        value = snapshot()
        value["calendar"]["checked_on"] = "2026-09-17"
        result = readiness(value)
        self.assertEqual(check_map(result)["calendar"]["status"], "warn")
        self.assertNotIn("final", check_map(result))

    def test_rest_day_does_not_require_current_auction_or_final(self):
        value = snapshot("2026-09-20", "16:00:00")
        value["auction"] = {}
        value["running"] = False
        result = readiness(value)
        self.assertEqual(result["status"], "info")
        self.assertNotIn("final", check_map(result))
        self.assertFalse(any(row["status"] == "error" for row in result["checks"]))

    def test_active_receipt_age_and_coverage_alerts(self):
        value = snapshot()
        value["auction"]["summary"]["last_received_at"] = "2026-09-18T09:21:20+08:00"
        value["auction"]["coverage"] = .5
        checks = check_map(readiness(value))
        self.assertEqual(checks["freshness"]["status"], "warn")
        self.assertEqual(checks["coverage"]["status"], "warn")
        self.assertIn("40.0", checks["freshness"]["message"])

    def test_after_close_missing_final_is_warning(self):
        value = snapshot(clock="16:00:00")
        checks = check_map(readiness(value))
        self.assertEqual(checks["final"]["status"], "warn")
        value["auction"]["final_count"] = 2
        self.assertEqual(check_map(readiness(value))["final"]["status"], "ok")

    def test_previous_session_observations_do_not_satisfy_today(self):
        value = snapshot(clock="16:00:00")
        value["auction"].update(date="2026-09-17", final_count=2)
        checks = check_map(readiness(value))
        self.assertEqual(checks["observations"]["status"], "warn")
        self.assertEqual(checks["final"]["status"], "warn")

    def test_disabled_monitor_in_live_window_is_actionable(self):
        value = snapshot()
        value["running"] = False
        self.assertEqual(check_map(readiness(value))["monitor"]["status"], "error")

    def test_prepared_date_mismatch_not_covered_by_nonempty_pool(self):
        value = snapshot()
        checks = check_map(readiness(value, {"prepared_date": "2026-09-17"}))
        self.assertEqual(checks["universe"]["status"], "error")

    def test_future_receipt_does_not_look_fresh(self):
        value = snapshot()
        value["auction"]["summary"]["last_received_at"] = "2026-09-18T09:23:00+08:00"
        self.assertEqual(check_map(readiness(value))["freshness"]["status"], "warn")

    def test_demo_does_not_check_live_credentials(self):
        value = {"mode": "demo", "configured": False}
        result = readiness(value)
        self.assertEqual(result["status"], "info")
        self.assertEqual(len(result["checks"]), 1)

    def test_unknown_mode_does_not_default_to_live_calendar_checks(self):
        for mode in (None, "unknown", "LIVE", 1):
            with self.subTest(mode=mode):
                value = snapshot()
                value["mode"] = mode
                checks = check_map(readiness(value))
                self.assertEqual(checks["mode"]["status"], "error")
                self.assertNotIn("calendar", checks)
                self.assertNotIn("freshness", checks)

    def test_watchlist_only_does_not_require_trend_pool(self):
        value = snapshot()
        value["config"] = {"universe": "watchlist"}
        value["stocks"]["trend_pool"] = {}
        result = readiness(value)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(check_map(result)["trend_pool"]["status"], "info")
        value["config"]["universe"] = "focus"
        self.assertEqual(check_map(readiness(value))["trend_pool"]["status"], "warn")

    def test_timezone_is_converted_without_using_machine_clock(self):
        value = snapshot()
        value["now"] = "2026-09-18T01:22:00+00:00"
        self.assertEqual(readiness(value)["status"], "ok")
        value["now"] = "2026-09-18T09:22:00"
        self.assertEqual(check_map(readiness(value))["clock"]["status"], "error")

    def test_post_close_new_trend_pool_can_target_next_session(self):
        value = snapshot(clock="16:00:00")
        value["stocks"]["trend_pool"]["date"] = "2026-09-18"
        self.assertEqual(check_map(readiness(value))["trend_pool"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
