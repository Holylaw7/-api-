"""Offline official-context contracts; never access credentials or real data."""
import copy
import json
import unittest
from datetime import datetime, timedelta

from app.official_context import BENCHMARK_PATH, DRAGON_TIGER_PATH, build_official_context
from app.provider import APIError, SHANGHAI, date_ms


DATE = "2026-01-05"


def stock(code="000001.SZ", days=1, **values):
    row = {"thscode": code, "ticker": code[:6], "name": "测试证券",
           "range_days": days, "change": .025, "net_rate": -.12,
           "net_value": 100, "org_net_value": 20, "hot_money_item_net_value": -30,
           "hot_money_net_value": -50, "buy_value": 1000, "sell_value": 900,
           "concept_list": [{"name": "测试概念"}], "limit_reason": "测试原因"}
    row.update(values)
    return row


def payloads():
    result = {"benchmark": {"date": DATE, "date_ms": date_ms(DATE), "timestamp": 123,
                            "item": [{"thscode": "000001.SZ", "name": "测试证券", "auction_pct": .35,
                                      "tags": ["高开"]}]}}
    for board in ("all", "org", "hot_money"):
        result[board] = {"trade_date": DATE, "board_type": board, "timestamp": date_ms(DATE),
                         "count": 1, "stock_count": 1, "stock_items": [stock()] if board != "hot_money" else [],
                         "hot_money_items": [{"name": "测试游资", "buying": -30,
                                              "rows": [stock()]}] if board == "hot_money" else []}
    return result


class FakeProvider:
    def __init__(self, data=None):
        self.data = payloads() if data is None else data
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, copy.deepcopy(params)))
        section = "benchmark" if path == BENCHMARK_PATH else params["board_type"]
        item = self.data[section]
        if isinstance(item, Exception):
            raise item
        return copy.deepcopy(item)


class OfficialContextTests(unittest.TestCase):
    def test_exactly_four_dated_calls_no_calendar_member_or_funds_scan(self):
        provider = FakeProvider()
        result = build_official_context(provider, DATE)
        self.assertEqual("ready", result["status"])
        self.assertEqual({"attempted": 4, "max": 4}, result["requests"])
        self.assertEqual([(BENCHMARK_PATH, {"date": DATE})] +
                         [(DRAGON_TIGER_PATH, {"date": DATE, "board_type": board})
                          for board in ("all", "org", "hot_money")], provider.calls)
        self.assertEqual(DATE, result["date"])
        self.assertEqual("live", result["mode"])

    def test_benchmark_percent_already_percent_and_zeros_count(self):
        data = payloads()
        data["benchmark"]["item"] = [
            {"thscode": "000001.SZ", "auction_pct": 2, "tags": []},
            {"thscode": "600000.SH", "auction_pct": 0, "tags": []},
            {"thscode": "600001.SH", "auction_pct": -1, "tags": []},
            {"thscode": "600002.SH", "auction_pct": None, "tags": []}]
        result = build_official_context(FakeProvider(data), DATE)["benchmark"]
        self.assertEqual(4, result["sample_count"])
        self.assertEqual(3, result["valid_auction_count"])
        self.assertAlmostEqual(1 / 3, result["mean_auction_pct"])
        self.assertEqual(0, result["median_auction_pct"])
        self.assertAlmostEqual(100 / 3, result["positive_ratio_pct"])
        self.assertEqual([2, 0, -1, None], [row["auction_pct"] for row in result["rows"]])

    def test_benchmark_empty_is_valid_but_statistics_unknown(self):
        data = payloads()
        data["benchmark"]["item"] = []
        result = build_official_context(FakeProvider(data), DATE)["benchmark"]
        self.assertEqual(0, result["sample_count"])
        self.assertEqual(0, result["valid_auction_count"])
        self.assertIsNone(result["mean_auction_pct"])
        self.assertIsNone(result["median_auction_pct"])
        self.assertIsNone(result["positive_ratio_pct"])

    def test_date_and_midnight_ms_must_both_match(self):
        for field, value in [("date", "2026-01-06"), ("date_ms", date_ms(DATE) + 1),
                             ("date_ms", None), ("date_ms", str(date_ms(DATE))), ("date_ms", True)]:
            with self.subTest(field=field, value=value):
                data = payloads()
                data["benchmark"][field] = value
                result = build_official_context(FakeProvider(data), DATE)
                self.assertIsNone(result["benchmark"])
                self.assertEqual("partial", result["status"])
                self.assertEqual("invalid_data", result["errors"][0]["kind"])
                self.assertEqual(value, result["raw"]["benchmark"][field])

    def test_timestamp_alone_never_proves_date(self):
        data = payloads()
        data["benchmark"].pop("date")
        data["benchmark"]["timestamp"] = date_ms(DATE)
        data["all"].pop("trade_date")
        data["all"]["timestamp"] = date_ms(DATE)
        result = build_official_context(FakeProvider(data), DATE)
        self.assertIsNone(result["benchmark"])
        self.assertIsNone(result["dragon_tiger"]["all"])
        self.assertEqual(2, len(result["errors"]))

    def test_response_timestamp_can_be_another_day_without_relabeling(self):
        result = build_official_context(FakeProvider(), DATE)
        self.assertEqual(123, result["benchmark"]["response_timestamp"])
        self.assertEqual(DATE, result["benchmark"]["date"])

    def test_dragon_tiger_timestamp_conflict_is_exposed_without_rewriting(self):
        data = payloads()
        data["all"]["timestamp"] = date_ms(DATE) + 86_400_000
        result = build_official_context(FakeProvider(data), DATE)
        board = result["dragon_tiger"]["all"]
        self.assertEqual("partial", result["status"])
        self.assertEqual("partial", board["status"])
        self.assertEqual(DATE, board["trade_date"])
        self.assertEqual("explicit_trade_date", board["date_basis"])
        self.assertFalse(board["timestamp_matches_date"])
        self.assertEqual(date_ms(DATE) + 86_400_000, board["timestamp"])
        self.assertTrue(board["warnings"])
        self.assertTrue(result["dragon_tiger"]["org"]["timestamp_matches_date"])

    def test_dragon_tiger_missing_timestamp_is_unknown_not_matching(self):
        data = payloads()
        data["all"].pop("timestamp")
        board = build_official_context(FakeProvider(data), DATE)["dragon_tiger"]["all"]
        self.assertEqual("partial", board["status"])
        self.assertIsNone(board["timestamp_matches_date"])
        self.assertEqual(DATE, board["trade_date"])

    def test_no_implicit_date_fallback_or_future_request(self):
        future = (datetime.now(SHANGHAI).date() + timedelta(days=1)).isoformat()
        for date in (None, "", "2026-1-05", "2026-02-30", "../2026-01-05", future):
            with self.subTest(date=date):
                provider = FakeProvider()
                with self.assertRaises(ValueError):
                    build_official_context(provider, date)
                self.assertFalse(provider.calls)

    def test_board_type_and_date_are_verified(self):
        for field, value in [("trade_date", "2026-01-06"), ("board_type", "all")]:
            data = payloads()
            data["org"][field] = value
            result = build_official_context(FakeProvider(data), DATE)
            self.assertIsNone(result["dragon_tiger"]["org"])
            self.assertIsNotNone(result["dragon_tiger"]["all"])
            self.assertEqual("partial", result["status"])

    def test_not_ready_and_api_errors_remain_distinct_from_zero(self):
        data = payloads()
        data["benchmark"] = APIError("fixture private body", code=3002)
        data["org"] = APIError("fixture unauthorized body", code=2001)
        result = build_official_context(FakeProvider(data), DATE)
        self.assertEqual("partial", result["status"])
        self.assertEqual(["not_ready", "api_error"], [error["kind"] for error in result["errors"]])
        self.assertEqual([3002, 2001], [error["code"] for error in result["errors"]])
        self.assertIsNone(result["benchmark"])
        self.assertNotIn("private body", json.dumps(result))
        self.assertNotIn("unauthorized body", json.dumps(result))

    def test_total_failure_does_not_make_successful_empty_boards(self):
        data = {key: RuntimeError("sensitive arbitrary exception") for key in payloads()}
        result = build_official_context(FakeProvider(data), DATE)
        self.assertEqual("unavailable", result["status"])
        self.assertEqual(4, len(result["errors"]))
        self.assertTrue(all(value is None for value in result["dragon_tiger"].values()))
        self.assertNotIn("sensitive", json.dumps(result))

    def test_malformed_lists_fail_only_own_section_and_preserve_raw(self):
        for value in (None, {}, ["wrong"], [{"thscode": "000001"}]):
            with self.subTest(value=value):
                data = payloads()
                data["benchmark"]["item"] = value
                result = build_official_context(FakeProvider(data), DATE)
                self.assertIsNone(result["benchmark"])
                self.assertEqual(value, result["raw"]["benchmark"]["item"])
                self.assertIsNotNone(result["dragon_tiger"]["all"])
        data = payloads()
        data["hot_money"]["hot_money_items"][0]["rows"] = [1]
        self.assertIsNone(build_official_context(FakeProvider(data), DATE)["dragon_tiger"]["hot_money"])

    def test_null_boolean_nan_and_nonnumber_are_not_zero(self):
        data = payloads()
        values = [None, False, float("nan"), float("inf"), "2", 0]
        data["benchmark"]["item"] = [{"thscode": f"{i:06}.SZ", "auction_pct": v, "tags": []}
                                     for i, v in enumerate(values)]
        result = build_official_context(FakeProvider(data), DATE)["benchmark"]
        self.assertEqual(1, result["valid_auction_count"])
        self.assertEqual(0, result["mean_auction_pct"])
        self.assertEqual(0, result["positive_ratio_pct"])
        self.assertEqual(5, sum(row["auction_pct"] is None for row in result["rows"]))

    def test_board_amounts_in_yuan_ratios_converted_separately(self):
        result = build_official_context(FakeProvider(), DATE)
        row = result["dragon_tiger"]["all"]["groups"]["one_day"][0]
        self.assertEqual(100, row["net_value"])
        self.assertEqual(.025, row["change"])
        self.assertEqual(2.5, row["change_pct"])
        self.assertEqual(-.12, row["net_rate"])
        self.assertEqual(-12, row["net_rate_pct"])

    def test_daily_and_three_day_not_merged_and_duplicates_not_summed(self):
        data = payloads()
        data["all"].update(count=5, stock_count=2, stock_items=[
            stock(net_value=100), stock(days=3, net_value=300),
            stock(net_value=50, limit_reason="另一涨跌停原因"),
            stock("600000.SH", days=None), stock("600000.SH", days=0)])
        board = build_official_context(FakeProvider(data), DATE)["dragon_tiger"]["all"]
        self.assertEqual({"one_day": 2, "three_day": 1, "other": 2}, board["group_counts"])
        self.assertEqual([100, 50], [row["net_value"] for row in board["groups"]["one_day"]])
        self.assertTrue(all(row["duplicate_record"] for row in board["groups"]["one_day"]))
        self.assertFalse(board["groups"]["three_day"][0]["duplicate_record"])
        self.assertEqual(2, board["duplicate_record_count"])
        self.assertEqual(5, board["reported_count"])
        self.assertEqual(2, board["reported_stock_count"])
        self.assertEqual(2, board["observed_stock_count"])
        self.assertNotIn("total_net_value", board)

    def test_hot_money_overlap_is_preserved_without_double_aggregation(self):
        data = payloads()
        data["hot_money"]["hot_money_items"] += [{"name": "第二游资", "buying": 70,
                                                   "rows": [stock(hot_money_item_net_value=70)]}]
        board = build_official_context(FakeProvider(data), DATE)["dragon_tiger"]["hot_money"]
        self.assertEqual(2, board["actor_count"])
        self.assertEqual(1, board["observed_stock_count"])
        self.assertEqual(0, board["duplicate_record_count"])
        rows = board["groups"]["one_day"]
        self.assertEqual([70, -30], [row["hot_money_item_net_value"] for row in rows])
        self.assertEqual([70, -30], [row["reported_hot_money_net_value"] for row in rows])
        self.assertNotIn("total_net_value", board)

    def test_stock_boards_declared_nonempty_but_empty_rows_are_partial(self):
        for kind in ("all", "org"):
            with self.subTest(board=kind):
                data = payloads()
                data[kind].update(count=80, stock_count=75, stock_items=[])
                result = build_official_context(FakeProvider(data), DATE)
                board = result["dragon_tiger"][kind]
                self.assertEqual("partial", result["status"])
                self.assertEqual("partial", board["status"])
                self.assertEqual("inconsistent", board["coverage"]["status"])
                self.assertFalse(board["coverage"]["record_count_matches"])
                self.assertFalse(board["coverage"]["stock_count_matches"])
                self.assertEqual(80, board["reported_count"])
                self.assertEqual(0, board["received_rows"])
                self.assertTrue(board["warnings"])

    def test_record_and_unique_counts_checked_independently(self):
        for count, stocks, record_match, stock_match in [(2, 1, False, True), (1, 2, True, False)]:
            data = payloads()
            data["all"].update(count=count, stock_count=stocks)
            board = build_official_context(FakeProvider(data), DATE)["dragon_tiger"]["all"]
            self.assertEqual("partial", board["status"])
            self.assertEqual(record_match, board["coverage"]["record_count_matches"])
            self.assertEqual(stock_match, board["coverage"]["stock_count_matches"])
        data = payloads()
        data["all"].update(count=2, stock_count=1, stock_items=[stock(), stock(days=3)])
        board = build_official_context(FakeProvider(data), DATE)["dragon_tiger"]["all"]
        self.assertEqual("verified", board["coverage"]["status"])
        self.assertEqual("ready", board["status"])

    def test_hot_money_upstream_counts_are_not_associated_row_denominator(self):
        data = payloads()
        data["hot_money"].update(count=52, stock_count=47, hot_money_items=[
            {"name": f"测试游资{actor}", "buying": -90,
             "rows": [stock(f"{(actor * 3 + offset) % 30 + 1:06}.SZ") for offset in range(3)]}
            for actor in range(14)])
        board = build_official_context(FakeProvider(data), DATE)["dragon_tiger"]["hot_money"]
        self.assertEqual("ready", board["status"])
        self.assertEqual(52, board["reported_count"])
        self.assertEqual(47, board["reported_stock_count"])
        self.assertEqual(42, board["received_rows"])
        self.assertEqual(30, board["observed_stock_count"])
        self.assertEqual("unknown", board["coverage"]["status"])
        self.assertIsNone(board["coverage"]["record_count_matches"])
        self.assertIsNone(board["coverage"]["stock_count_matches"])
        self.assertTrue(board["coverage"]["definition"])

    def test_numeric_zero_remains_valid_count_and_negative_net_retained(self):
        data = payloads()
        data["all"].update(count=0, stock_count=0, stock_items=[])
        result = build_official_context(FakeProvider(data), DATE)
        self.assertEqual(0, result["dragon_tiger"]["all"]["reported_count"])
        self.assertEqual(0, result["dragon_tiger"]["all"]["reported_stock_count"])
        data["all"].update(count=True, stock_count=None, stock_items=[stock(net_value=0, org_net_value=None,
                                                                          buy_value=-5, sell_value=0)])
        board = build_official_context(FakeProvider(data), DATE)["dragon_tiger"]["all"]
        self.assertIsNone(board["reported_count"])
        self.assertIsNone(board["reported_stock_count"])
        row = board["groups"]["one_day"][0]
        self.assertEqual(0, row["net_value"])
        self.assertIsNone(row["org_net_value"])
        self.assertIsNone(row["buy_value"])
        self.assertEqual(0, row["sell_value"])

    def test_rows_capped_after_statistics_raw_complete_and_sort_stable(self):
        data = payloads()
        data["benchmark"]["item"] = [{"thscode": f"{i:06}.SZ", "auction_pct": 1, "tags": []}
                                     for i in range(40, 0, -1)]
        data["all"].update(count=80, stock_count=40,
                           stock_items=[stock(f"{i:06}.SZ", days=day) for day in (1, 3) for i in range(40, 0, -1)])
        result = build_official_context(FakeProvider(data), DATE)
        benchmark = result["benchmark"]
        self.assertEqual(40, benchmark["valid_auction_count"])
        self.assertEqual(30, len(benchmark["rows"]))
        self.assertEqual("000001.SZ", benchmark["rows"][0]["thscode"])
        self.assertTrue(benchmark["truncated"])
        board = result["dragon_tiger"]["all"]
        self.assertEqual({"one_day": 40, "three_day": 40, "other": 0}, board["group_counts"])
        self.assertEqual(60, board["shown_count"])
        self.assertEqual("000001.SZ", board["groups"]["one_day"][0]["thscode"])
        self.assertEqual(80, len(result["raw"]["all"]["stock_items"]))

    def test_unknown_fields_stay_raw_only(self):
        data = payloads()
        data["benchmark"]["item"][0]["unexpected_blob"] = "opaque"
        data["all"]["stock_items"][0]["unexpected_blob"] = "opaque"
        data["all"]["stock_items"][0]["concept_list"] = [{"name": "概念", "unexpected_blob": "opaque"}]
        result = build_official_context(FakeProvider(data), DATE)
        public = {key: value for key, value in result.items() if key != "raw"}
        self.assertNotIn("unexpected_blob", json.dumps(public))
        self.assertIn("unexpected_blob", json.dumps(result["raw"]))

    def test_duplicate_benchmark_does_not_skew_statistics(self):
        data = payloads()
        data["benchmark"]["item"] *= 2
        result = build_official_context(FakeProvider(data), DATE)
        self.assertIsNone(result["benchmark"])
        self.assertEqual("invalid_data", result["errors"][0]["kind"])

    def test_cancel_before_first_and_between_requests(self):
        provider = FakeProvider()
        result = build_official_context(provider, DATE, should_stop=lambda: True)
        self.assertEqual("cancelled", result["status"])
        self.assertFalse(provider.calls)
        provider = FakeProvider()
        result = build_official_context(provider, DATE, should_stop=lambda: len(provider.calls) >= 2)
        self.assertEqual(2, len(provider.calls))
        self.assertTrue(result["cancelled"])
        self.assertIsNotNone(result["benchmark"])
        self.assertIsNotNone(result["dragon_tiger"]["all"])
        self.assertIsNone(result["dragon_tiger"]["org"])
        self.assertIsNone(result["dragon_tiger"]["hot_money"])


if __name__ == "__main__":
    unittest.main()
