import copy
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from app.provider import APIError, HiThinkProvider, SHANGHAI, date_ms
from app.stocks import analyze_stock, validate_code


METADATA = {"thscode": "000001.SZ", "ticker": "000001", "name": "平安银行",
            "asset_type": "a-share", "exchange": "SZ", "currency": "CNY"}
CALENDAR = [(datetime(2026, 6, 1) + timedelta(days=offset)).date().isoformat()
            for offset in range(100) if (datetime(2026, 6, 1) + timedelta(days=offset)).weekday() < 5]
DATE = CALENDAR[-1]
NOW = datetime(2026, 9, 19, 17, tzinfo=SHANGHAI)


def fixture_bars():
    return {"timestamp": date_ms(DATE), "item": [
        {"date_ms": date_ms(day), "open_price": 10 + i / 10, "high_price": 10.2 + i / 10,
         "low_price": 9.8 + i / 10, "close_price": 10 + i / 10, "volume": 1000 + i * 10,
         "turnover": (10 + i / 10) * (1000 + i * 10)} for i, day in enumerate(CALENDAR[-40:])]}


class ResolveStockTests(unittest.TestCase):
    def setUp(self):
        self.provider = HiThinkProvider("fixture-stock-secret", min_interval=0)

    def test_format_validation_preserves_leading_zero_and_normalizes_case(self):
        self.assertEqual(validate_code(" 000001.sz "), "000001.SZ")
        self.assertEqual(validate_code("000001"), "000001")
        for value in [1, None, True, "平安银行", "00001", "000001.SZ,600000.SH", "sh600000", "０００００１", "000001.HK", "000 001"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_code(value)

    @patch.object(HiThinkProvider, "get")
    def test_six_digit_resolved_from_exact_official_match_not_prefix_guess(self, get_mock):
        other = dict(METADATA, thscode="000010.SZ", ticker="000010")
        get_mock.return_value = {"timestamp": 123, "item": [other, METADATA]}
        result = self.provider.resolve_stock("000001")
        self.assertEqual(result["thscode"], "000001.SZ")
        self.assertEqual(result["name"], "平安银行")
        self.assertEqual(result["resolution"]["timestamp"], 123)
        get_mock.assert_called_once_with("/api/meta/tickers/search", {"q":"000001", "asset_type":"a-share", "limit":50})

    @patch.object(HiThinkProvider, "get")
    def test_explicit_suffix_must_match_and_never_corrects_silently(self, get_mock):
        get_mock.return_value = {"item": [METADATA]}
        with self.assertRaises(APIError):
            self.provider.resolve_stock("000001.SH")

    @patch.object(HiThinkProvider, "get")
    def test_ambiguous_six_digit_code_rejected_but_exact_suffix_resolves(self, get_mock):
        other = dict(METADATA, thscode="000001.SH", exchange="SH", name="另一官方标的")
        get_mock.return_value = {"item": [METADATA, other]}
        with self.assertRaises(APIError):
            self.provider.resolve_stock("000001")
        result = self.provider.resolve_stock("000001.sz")
        self.assertEqual(result["thscode"], "000001.SZ")

    @patch.object(HiThinkProvider, "get")
    def test_nonstock_asset_and_inconsistent_metadata_rejected(self, get_mock):
        for bad in [dict(METADATA, asset_type="a-share-index"), dict(METADATA, ticker="000002"),
                    dict(METADATA, exchange="SH"), dict(METADATA, name=None)]:
            with self.subTest(bad=bad):
                get_mock.return_value = {"item": [bad]}
                with self.assertRaises(APIError):
                    self.provider.resolve_stock("000001")

    @patch.object(HiThinkProvider, "get")
    def test_conflicting_same_code_rejected_and_identical_duplicate_deduplicated(self, get_mock):
        get_mock.return_value = {"item": [METADATA, dict(METADATA, name="冲突名称")]}
        with self.assertRaises(APIError):
            self.provider.resolve_stock("000001")
        get_mock.return_value = {"item": [METADATA, copy.deepcopy(METADATA)]}
        self.assertEqual(self.provider.resolve_stock("000001")["name"], METADATA["name"])

    @patch.object(HiThinkProvider, "get")
    def test_invalid_input_makes_no_network_call(self, get_mock):
        with self.assertRaises(APIError):
            self.provider.resolve_stock("银行")
        get_mock.assert_not_called()

    @patch.object(HiThinkProvider, "get")
    def test_stock_quote_uses_explicit_codes_and_preserves_timestamp(self, get_mock):
        response = {"timestamp": 12345, "total": 2, "item": [{"thscode":"000001.SZ", "last_price":12}]}
        get_mock.return_value = response
        self.assertIs(self.provider.stock_quote(["000001.sz", "000001.SZ"]), response)
        get_mock.assert_called_once_with("/api/a-share/prices/snapshot", {"thscodes":"000001.SZ"})
        with self.assertRaises(APIError):
            self.provider.stock_quote(["881001.TI"])


class SingleStockAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.provider = Mock()
        self.provider.historical.return_value = fixture_bars()
        self.clock = patch("app.stocks.now_sh", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def analyze(self, **changes):
        params = dict(provider=self.provider, metadata=METADATA, date=DATE, calendar=CALENDAR)
        params.update(changes)
        return analyze_stock(**params)

    def test_one_bounded_forward_history_and_no_snapshot_or_auction(self):
        result = self.analyze()
        self.provider.historical.assert_called_once_with("000001.SZ", CALENDAR[-40], DATE, adjust="forward")
        self.provider.stock_quote.assert_not_called()
        self.provider.auction.assert_not_called()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["trend"]["status"], "ready")
        self.assertIsNotNone(result["trend_score"])
        self.assertEqual(result["trend_coverage"], 1)
        self.assertEqual(len(result["history"]), 40)
        self.assertEqual(result["history"][-1]["date"], DATE)
        self.assertEqual(result["source"]["request_count"], 1)
        self.assertIsNone(result["quote"])

    def test_score_contributions_and_explicit_formula_are_reproducible(self):
        result = self.analyze()
        factors = result["trend_factors"]
        self.assertAlmostEqual(sum(factor["contribution"] for factor in factors.values()), result["trend_score"], places=3)
        self.assertEqual(factors["ma_position"]["score"], 100)
        self.assertEqual(factors["drawdown_control"]["score"], 100)
        self.assertTrue(all(factor["formula"] for factor in factors.values()))
        self.assertAlmostEqual(factors["momentum_5d"]["score"], 50 + 2.5 * result["trend"]["return_5d_pct"], places=3)

    def test_future_or_nontrading_date_rejected_before_request(self):
        for date in ["2099-01-01", "2026-09-19", "2026-02-30"]:
            with self.subTest(date=date), self.assertRaises(ValueError):
                self.analyze(date=date)
        self.provider.historical.assert_not_called()

    def test_today_before_close_rejected(self):
        with patch("app.stocks.now_sh", return_value=datetime.fromisoformat(DATE + "T14:59:59").replace(tzinfo=SHANGHAI)):
            with self.assertRaises(ValueError):
                self.analyze()
        self.provider.historical.assert_not_called()

    def test_metadata_must_be_resolved_full_stock_code(self):
        with self.assertRaises(ValueError):
            self.analyze(metadata=dict(METADATA, thscode="000001"))
        with self.assertRaises(ValueError):
            self.analyze(metadata=dict(METADATA, asset_type="fund-etf"))
        self.provider.historical.assert_not_called()

    def test_future_bar_does_not_change_score_or_appear_on_chart(self):
        baseline = self.analyze()
        data = fixture_bars()
        future = copy.deepcopy(data["item"][-1])
        future.update(date_ms=date_ms("2026-09-19"), close_price=9999, volume=1e20)
        data["item"].append(future)
        self.provider.historical.return_value = data
        result = self.analyze()
        self.assertEqual(result["trend_score"], baseline["trend_score"])
        self.assertEqual(len(result["history"]), 40)
        self.assertTrue(any("未来数据" in warning for warning in result["warnings"]))
        self.assertEqual(result["status"], "partial")

    def test_missing_target_close_is_not_replaced_by_previous_or_next_day(self):
        data = fixture_bars()
        data["item"].pop()
        self.provider.historical.return_value = data
        result = self.analyze()
        self.assertIsNone(result["trend_score"])
        self.assertEqual(result["trend"]["status"], "date_mismatch")
        self.assertEqual(result["status"], "unavailable")

    def test_duplicate_target_day_quarantined(self):
        data = fixture_bars()
        data["item"].append(copy.deepcopy(data["item"][-1]))
        self.provider.historical.return_value = data
        result = self.analyze()
        self.assertIsNone(result["trend_score"])
        self.assertNotIn(DATE, [row["date"] for row in result["history"]])
        self.assertTrue(any("重复" in warning for warning in result["warnings"]))

    def test_null_volume_remains_unavailable_instead_of_zero(self):
        data = fixture_bars()
        data["item"][-1]["volume"] = None
        self.provider.historical.return_value = data
        result = self.analyze()
        factor = result["trend_factors"]["volume_confirmation"]
        self.assertIsNone(factor["value"])
        self.assertIsNone(factor["score"])
        self.assertEqual(result["trend_coverage"], .85)
        self.assertIsNotNone(result["trend_score"])

    def test_fewer_than_twenty_bars_do_not_fabricate_moving_averages(self):
        data = fixture_bars()
        data["item"] = data["item"][-6:]
        self.provider.historical.return_value = data
        result = self.analyze()
        self.assertIsNone(result["trend"]["ma20"])
        self.assertIsNone(result["trend_factors"]["ma_position"]["score"])
        self.assertIsNone(result["trend_score"])
        self.assertEqual(result["trend_coverage"], .5)

    def test_high_volume_falling_day_is_not_rewarded_as_bullish(self):
        data = fixture_bars()
        data["item"][-1]["close_price"] = data["item"][-2]["close_price"] * .95
        data["item"][-1]["volume"] = data["item"][-2]["volume"] * 5
        self.provider.historical.return_value = data
        result = self.analyze()
        self.assertEqual(result["trend_factors"]["volume_confirmation"]["score"], 0)
        self.assertLess(result["trend_factors"]["volume_confirmation"]["day_return_pct"], 0)

    def test_history_error_returns_explicit_unavailable_and_sanitizes_unexpected_error(self):
        self.provider.historical.side_effect = RuntimeError("fixture-secret")
        result = self.analyze()
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["trend_score"])
        self.assertEqual(result["history"], [])
        self.assertNotIn("fixture-secret", str(result))

    def test_context_after_target_date_does_not_leak_into_analysis(self):
        result = self.analyze(context={"000001.SZ":{"context_date":"2099-01-01", "continue_day_cnt":10}})
        self.assertIsNone(result["context"])
        self.assertTrue(any("晚于分析日" in warning for warning in result["warnings"]))

    def test_wrong_adjustment_is_unavailable(self):
        self.provider.historical.return_value = dict(fixture_bars(), adjust="none")
        result = self.analyze()
        self.assertIsNone(result["trend_score"])
        self.assertEqual(result["trend"]["status"], "unavailable")
        self.assertEqual(result["history"], [])
        self.assertEqual(result["source"]["response_adjust"], "none")


if __name__ == "__main__":
    unittest.main()
