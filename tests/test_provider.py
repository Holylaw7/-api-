import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from app.provider import APIError, HiThinkProvider, _SameOriginRedirect, date_ms, iso_date


class Response:
    status = 200

    def __init__(self, envelope):
        self.envelope = envelope

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return json.dumps(self.envelope).encode()


def success(data):
    return Response({"code": 0, "message": "success", "request_id": "fixture", "data": data})


class ProviderTests(unittest.TestCase):
    def setUp(self):
        HiThinkProvider._next_request.clear()
        self.provider = HiThinkProvider("fixture-secret", min_interval=0)

    @patch("app.provider.urlopen")
    def test_200_business_error_is_not_success_or_raw_message(self, open_mock):
        open_mock.return_value = Response({"code": 2003, "message": "fixture-secret detail", "data": None,
                                          "request_id": "fixture-secret"})
        with self.assertRaises(APIError) as captured:
            self.provider.get("/api/test")
        self.assertEqual(captured.exception.code, 2003)
        self.assertNotIn("fixture-secret", str(captured.exception))
        self.assertEqual(open_mock.call_count, 1)
        self.assertNotIn("fixture-secret", repr(self.provider))

    @patch("app.provider.urlopen")
    def test_unready_no_retry_and_null_not_empty(self, open_mock):
        open_mock.return_value = Response({"code": 3002, "message": "not ready", "data": None})
        with self.assertRaises(APIError) as captured:
            self.provider.get("/api/test")
        self.assertEqual(captured.exception.code, 3002)
        self.assertEqual(open_mock.call_count, 1)

    @patch("app.provider.time.sleep")
    @patch("app.provider.urlopen")
    def test_business_rate_limit_bounded_retry(self, open_mock, sleep_mock):
        error = Response({"code": 4001, "message": "slow", "data": None})
        open_mock.side_effect = [error, error, error, success({"item": []})]
        self.assertEqual(self.provider.get("/api/test"), {"item": []})
        self.assertEqual(open_mock.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep_mock.call_args_list], [.5, 1, 2])

    @patch("app.provider.time.sleep")
    @patch("app.provider.urlopen")
    def test_http429_retry_after_is_respected_and_bounded(self, open_mock, sleep_mock):
        open_mock.side_effect = [HTTPError("https://example.invalid", 429, "busy", {"Retry-After": "99999"}, io.BytesIO()),
                                 success({"item": []})]
        self.provider.get("/api/test")
        sleep_mock.assert_called_once_with(15.0)

    @patch("app.provider.time.sleep")
    @patch("app.provider.urlopen")
    def test_network_failure_bounded_and_sanitized(self, open_mock, sleep_mock):
        open_mock.side_effect = URLError("fixture-secret")
        with self.assertRaises(APIError) as captured:
            self.provider.get("/api/test")
        self.assertEqual(open_mock.call_count, 4)
        self.assertNotIn("fixture-secret", str(captured.exception))

    @patch("app.provider.urlopen")
    def test_auction_is_single_attempt(self, open_mock):
        open_mock.side_effect = TimeoutError("fixture-secret")
        with self.assertRaises(APIError):
            self.provider.auction(["600000.SH"], "live")
        self.assertEqual(open_mock.call_count, 1)

    @patch("app.provider.urlopen")
    def test_auction_100_raw_token_limit(self, open_mock):
        with self.assertRaises(APIError):
            self.provider.auction(["600000.SH"] * 101, "live")
        self.assertEqual(open_mock.call_count, 0)

    @patch("app.provider.urlopen")
    def test_credential_only_header_and_auction_dedup(self, open_mock):
        open_mock.return_value = success({"item": []})
        self.provider.auction(["600000.SH", "600000.SH"], "final")
        request = open_mock.call_args.args[0]
        self.assertEqual(request.get_header("X-api-key"), "fixture-secret")
        self.assertNotIn("fixture-secret", request.full_url)
        params = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(params, {"thscodes": ["600000.SH"], "stage": ["final"]})

    def test_cross_origin_redirect_does_not_forward_key(self):
        handler = _SameOriginRedirect()
        request = Request("https://fuyao.aicubes.cn/api/test", headers={"X-api-key": "fixture-secret"})
        with self.assertRaises(APIError):
            handler.redirect_request(request, None, 302, "redirect", {}, "https://other.example/api")
        with self.assertRaises(APIError):
            handler.redirect_request(request, None, 302, "redirect", {}, "http://fuyao.aicubes.cn/api")

    @patch("app.provider.urlopen")
    def test_success_null_data_is_invalid(self, open_mock):
        open_mock.return_value = success(None)
        with self.assertRaises(APIError):
            self.provider.get("/api/test")

    @patch("app.provider.urlopen")
    def test_code_boolean_not_zero(self, open_mock):
        open_mock.return_value = Response({"code": False, "data": {}})
        with self.assertRaises(APIError):
            self.provider.get("/api/test")

    @patch.object(HiThinkProvider, "get")
    def test_calendar_normalizes_and_sorts(self, get_mock):
        get_mock.return_value = {"item": [{"date": "20260918"}, {"date": "20260917"}]}
        self.assertEqual(self.provider.calendar(), ["2026-09-17", "2026-09-18"])
        self.assertEqual(iso_date(date_ms("2026-09-18")), "2026-09-18")

    @patch.object(HiThinkProvider, "get")
    def test_complete_pool_pagination_over200(self, get_mock):
        first = [{"thscode": f"{index:06}.SH"} for index in range(200)]
        get_mock.side_effect = [{"pagination": {"total": 201, "pages": 2}, "item": first},
                                {"pagination": {"total": 201, "pages": 2}, "item": [{"thscode": "600000.SH"}]}]
        self.assertEqual(len(self.provider.pool("limit-up", "2026-09-18")), 201)
        self.assertEqual(get_mock.call_args_list[1].args[1]["page"], 2)
        self.assertEqual(get_mock.call_args_list[0].args[1]["date_ms"], date_ms("2026-09-18"))

    @patch.object(HiThinkProvider, "get")
    def test_pool_incomplete_raises_instead_of_silent_truncation(self, get_mock):
        get_mock.return_value = {"pagination": {"total": 2, "pages": 1}, "item": [{"thscode": "600000.SH"}]}
        with self.assertRaises(APIError):
            self.provider.pool("limit-up", "2026-09-18")

    @patch.object(HiThinkProvider, "get")
    def test_market_empty_middle_page_does_not_stop(self, get_mock):
        get_mock.side_effect = [{"total": 201, "timestamp": date_ms("2026-09-18"), "item": [{"thscode": "600000.SH"}]},
                                {"total": 201, "timestamp": None, "item": []},
                                {"total": 201, "timestamp": date_ms("2026-09-18"), "item": [{"thscode": "600001.SH"}]}]
        data = self.provider.market()
        self.assertEqual(len(data["item"]), 2)
        self.assertEqual(data["total"], 201)
        self.assertEqual([call.args[1]["offset"] for call in get_mock.call_args_list], [0, 100, 200])

    @patch.object(HiThinkProvider, "get")
    def test_ticker_pagination(self, get_mock):
        get_mock.side_effect = [{"item": [{"thscode": f"{index:06}.SH"} for index in range(1000)]},
                                {"item": [{"thscode": "600000.SH"}]}]
        self.assertEqual(len(self.provider.tickers()), 1001)
        self.assertEqual(get_mock.call_args.args[1]["offset"], 1000)

    @patch.object(HiThinkProvider, "get")
    def test_historical_stock_unadjusted_index_no_adjust(self, get_mock):
        get_mock.return_value = {"item": []}
        self.provider.historical("600000.SH", "2026-09-17", "2026-09-18")
        self.assertEqual(get_mock.call_args.args[1]["adjust"], "none")
        self.provider.historical("600000.SH", "2026-09-17", "2026-09-18", adjust="forward")
        self.assertEqual(get_mock.call_args.args[1]["adjust"], "forward")
        self.provider.index_historical("881001.TI", "2026-09-17", "2026-09-18")
        self.assertNotIn("adjust", get_mock.call_args.args[1])
        self.assertTrue(get_mock.call_args.args[0].startswith("/api/a-share-index/"))

    @patch("app.provider.time.sleep")
    @patch("app.provider.time.monotonic", return_value=100.0)
    def test_limiter_shared_across_same_credential_instances(self, clock_mock, sleep_mock):
        first = HiThinkProvider("shared-fixture", min_interval=.5)
        second = HiThinkProvider("shared-fixture", min_interval=.5)
        first._throttle()
        second._throttle()
        sleep_mock.assert_called_once_with(.5)


if __name__ == "__main__":
    unittest.main()
