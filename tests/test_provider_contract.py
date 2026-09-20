"""Official pagination and nonblocking shared-cooldown contract regressions."""
import io
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.error import HTTPError

from app.provider import APIError, HiThinkProvider
from tests.test_provider import Response, success


def pool_page(total=1, pages=1, page=1, size=200, codes=None):
    return {"pagination": {"total": total, "pages": pages, "page": page, "size": size},
            "item": [{"thscode": code} for code in (codes if codes is not None else ["600000.SH"])]}


class ProviderContractTests(unittest.TestCase):
    def setUp(self):
        HiThinkProvider._next_request.clear()
        HiThinkProvider._cooldown_until.clear()
        self.provider = HiThinkProvider("contract-fixture", min_interval=0)

    def tearDown(self):
        HiThinkProvider._next_request.clear()
        HiThinkProvider._cooldown_until.clear()

    def test_pool_rejects_wrong_page_size_and_boolean_pagination(self):
        invalid = [pool_page(page=9), pool_page(size=1)]
        for key in ("total", "pages", "page", "size"):
            payload = pool_page()
            payload["pagination"][key] = True
            invalid.append(payload)
        for payload in invalid:
            with self.subTest(paging=payload["pagination"]), patch.object(self.provider, "get", return_value=payload):
                with self.assertRaises(APIError):
                    self.provider.pool("limit-up", "2026-09-18")

    def test_pool_rejects_missing_echo_or_inconsistent_page_count(self):
        invalid = [pool_page(total=201, pages=1), pool_page(total=1, pages=2)]
        missing = pool_page()
        del missing["pagination"]["page"]
        invalid.append(missing)
        for payload in invalid:
            with self.subTest(payload=payload), patch.object(self.provider, "get", return_value=payload):
                with self.assertRaises(APIError):
                    self.provider.pool("limit-up", "2026-09-18")

    def test_pool_rejects_totals_changing_across_pages(self):
        first = pool_page(total=201, pages=2, codes=[f"{i:06}.SH" for i in range(200)])
        second = pool_page(total=202, pages=2, page=2, codes=["600000.SH", "600001.SH"])
        with patch.object(self.provider, "get", side_effect=[first, second]):
            with self.assertRaises(APIError):
                self.provider.pool("limit-up", "2026-09-18")

    def test_pool_rejects_duplicate_malformed_or_missing_codes(self):
        for codes in (["600000.SH", "600000.SH"], ["600000"], [None], [[]]):
            with self.subTest(codes=codes), patch.object(self.provider, "get", return_value=pool_page(total=len(codes), codes=codes)):
                with self.assertRaises(APIError):
                    self.provider.pool("limit-up", "2026-09-18")

    def test_pool_does_not_treat_short_middle_page_as_complete(self):
        with patch.object(self.provider, "get", return_value=pool_page(total=201, pages=2)) as get_mock:
            with self.assertRaises(APIError):
                self.provider.pool("limit-up", "2026-09-18")
            self.assertEqual(get_mock.call_count, 1)

    def test_empty_pool_accepts_zero_or_one_pages_with_valid_echo(self):
        for pages in (0, 1):
            with patch.object(self.provider, "get", return_value=pool_page(total=0, pages=pages, codes=[])):
                self.assertEqual(self.provider.pool("limit-down", "2026-09-18"), [])

    def test_market_keeps_empty_last_page_and_full_declared_coverage(self):
        with patch.object(self.provider, "get", side_effect=[
                {"total": 101, "item": [{"thscode": "600000.SH"}]},
                {"total": 101, "item": []}]) as get_mock:
            data = self.provider.market()
            self.assertEqual(data["total"], 101)
            self.assertEqual(len(data["item"]), 1)
            self.assertEqual(get_mock.call_count, 2)

    def test_market_rejects_boolean_total_or_more_rows_than_code_slots(self):
        for data in ({"total": False, "item": []},
                     {"total": 0, "item": [{"thscode": "600000.SH"}]},
                     {"total": 1, "item": [{"thscode": "600000.SH"}, {"thscode": "600001.SH"}]}):
            with patch.object(self.provider, "get", return_value=data):
                with self.assertRaises(APIError):
                    self.provider.market()

    def test_market_rejects_total_drift_and_duplicate_code(self):
        for second in ({"total": 100, "item": []},
                       {"total": 101, "item": [{"thscode": "600000.SH"}]}):
            with patch.object(self.provider, "get", side_effect=[
                    {"total": 101, "item": [{"thscode": "600000.SH"}]}, second]):
                with self.assertRaises(APIError):
                    self.provider.market()

    def test_tickers_rejects_duplicate_page_and_oversized_page(self):
        first = {"item": [{"thscode": f"{i:06}.SH"} for i in range(1000)]}
        with patch.object(self.provider, "get", side_effect=[first, {"item": [first["item"][0]]}]):
            with self.assertRaises(APIError):
                self.provider.tickers()
        with patch.object(self.provider, "get", return_value={"item": first["item"] + [{"thscode": "600000.SH"}]}):
            with self.assertRaises(APIError):
                self.provider.tickers()

    @patch("app.provider.time.sleep")
    @patch("app.provider.time.monotonic", return_value=100)
    @patch("app.provider.urlopen")
    def test_auction_429_preserves_delay_and_shares_nonblocking_cooldown(self, open_mock, clock_mock, sleep_mock):
        open_mock.side_effect = HTTPError("https://example.invalid", 429, "secret fixture", {"Retry-After": "42"}, io.BytesIO())
        with self.assertRaises(APIError) as captured:
            self.provider.auction(["600000.SH"])
        self.assertEqual(captured.exception.retry_after_seconds, 42)
        self.assertNotIn("secret fixture", str(captured.exception))
        peer = HiThinkProvider("contract-fixture", min_interval=0)
        with self.assertRaises(APIError) as peer_error:
            peer.get("/api/test")
        self.assertEqual(peer_error.exception.code, 4001)
        self.assertEqual(peer_error.exception.retry_after_seconds, 42)
        self.assertEqual(peer.rate_limit_status(), {"rate_limited": True, "cooldown_seconds": 42})
        self.assertEqual(open_mock.call_count, 1)
        sleep_mock.assert_not_called()
        clock_mock.return_value = 143
        open_mock.side_effect = None
        open_mock.return_value = success({"item": []})
        self.assertEqual(peer.get("/api/test"), {"item": []})
        self.assertEqual(peer.rate_limit_status(), {"rate_limited": False, "cooldown_seconds": 0})

    @patch("app.provider.time.sleep")
    @patch("app.provider.time.monotonic", return_value=100)
    @patch("app.provider.urlopen")
    def test_business_rate_limit_header_and_default_delay_are_preserved(self, open_mock, clock_mock, sleep_mock):
        for header, expected in (("37", 37), (None, .5)):
            HiThinkProvider._cooldown_until.clear()
            response = Response({"code": 4001, "data": None})
            response.headers = {"Retry-After": header} if header else {}
            open_mock.return_value = response
            with self.assertRaises(APIError) as captured:
                self.provider.auction(["600000.SH"])
            self.assertEqual(captured.exception.retry_after_seconds, expected)
            self.assertEqual(self.provider.rate_limit_status()["cooldown_seconds"], expected)
        sleep_mock.assert_not_called()

    @patch("app.provider.time.monotonic")
    @patch("app.provider.time.sleep")
    @patch("app.provider.urlopen")
    def test_short_retry_after_waits_fully_before_regular_retry(self, open_mock, sleep_mock, clock_mock):
        clock = [100.0]
        clock_mock.side_effect = lambda: clock[0]
        sleep_mock.side_effect = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        open_mock.side_effect = [HTTPError("https://example.invalid", 429, "busy", {"Retry-After": "8"}, io.BytesIO()),
                                 success({"item": []})]
        self.assertEqual(self.provider.get("/api/test"), {"item": []})
        sleep_mock.assert_called_once_with(8)
        self.assertEqual(clock[0], 108)

    @patch("app.provider.time.monotonic", return_value=100)
    @patch("app.provider.time.sleep")
    @patch("app.provider.urlopen")
    def test_waiting_slot_rechecks_cooldown_before_sending(self, open_mock, sleep_mock, clock_mock):
        self.provider.min_interval = 1
        self.provider._next_request[self.provider._limiter_id] = 101
        sleep_mock.side_effect = lambda _: self.provider._note_rate_limit(20)
        with self.assertRaises(APIError) as captured:
            self.provider.get("/api/test")
        self.assertEqual(captured.exception.retry_after_seconds, 20)
        open_mock.assert_not_called()

    @patch("app.provider.datetime")
    def test_retry_after_date_and_invalid_header(self, datetime_mock):
        datetime_mock.now.return_value = datetime(2026, 9, 20, 0, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(self.provider._retry_delay(0, "Sun, 20 Sep 2026 00:01:00 GMT"), 60)
        for value in ("nan", "inf", "-1", "secret fixture", ""):
            self.assertEqual(self.provider._retry_delay(0, value), .5)
        for value in (True, float("nan"), float("inf"), -1, "23"):
            self.assertIsNone(APIError(retry_after_seconds=value).retry_after_seconds)

    @patch("app.provider.time.monotonic", return_value=100)
    def test_cooldown_is_isolated_by_credential_and_never_shortened(self, clock_mock):
        self.provider._note_rate_limit(20)
        self.provider._note_rate_limit(2)
        self.assertEqual(self.provider.rate_limit_status()["cooldown_seconds"], 20)
        different = HiThinkProvider("another-fixture", min_interval=0)
        self.assertEqual(different.rate_limit_status(), {"cooldown_seconds": 0, "rate_limited": False})


if __name__ == "__main__":
    unittest.main()
