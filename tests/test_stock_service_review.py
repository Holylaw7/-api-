"""Independent integration regression cases for review findings."""
import copy
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from app.config import DEFAULT
from app.provider import APIError
from app.service import Service, SH


class StockIntegrationReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch("app.service.load_config", return_value=copy.deepcopy(DEFAULT)),
                        patch("app.service.load_llm", return_value={}),
                        patch("app.service.finance_key", return_value="review-fixture-only"),
                        patch("app.service.credential_status", return_value={}),
                        patch("app.service.now_sh", return_value=datetime(2026, 9, 18, 8, 50, tzinfo=SH))]
        for item in self.patches:
            item.start()
        self.service = Service(Path(self.tmp.name))
        self.service.shutdown.set()
        self.service.thread.join(2)
        self.service.shutdown.clear()
        self.service.provider = Mock()

    def tearDown(self):
        self.service.close()
        self.service.store.close()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_full_code_cache_does_not_prove_bare_code_is_globally_unambiguous(self):
        self.service.metadata = {"000001.SZ": {
            "thscode": "000001.SZ", "ticker": "000001", "name": "已确认标的",
            "asset_type": "a-share", "resolution": {"source": "official_ticker_search", "input": "000001.SZ"}}}
        self.service.provider.resolve_stock.side_effect = APIError("六位代码存在多个官方精确匹配")
        with self.assertRaises(APIError):
            self.service._resolve_stock("000001")
        self.service.provider.resolve_stock.assert_called_once_with("000001")

    def test_remove_in_demo_cannot_rebuild_synthetic_universe_from_live_sources(self):
        self.service.mode = "demo"
        self.service.config["watchlist"] = ["300750.SZ"]
        self.service.codes = ["000001.SZ", "000002.SZ"]
        self.service.context = {"600519.SH": {"context_date": "2026-09-17"}}
        with self.assertRaises(ValueError):
            self.service.remove_watchlist("300750.SZ")
        self.assertEqual(self.service.config["watchlist"], ["300750.SZ"])
        self.assertEqual(self.service.codes, ["000001.SZ", "000002.SZ"])

    def test_demo_llm_completion_cannot_replace_live_mode_narrative(self):
        service = self.service
        service.mode = "demo"
        entered, release = threading.Event(), threading.Event()
        def delayed_analysis(*args):
            entered.set()
            release.wait(2)
            return "根据演示股票编写的合成结论"
        demo_state = {"mode": "demo", "auction": {"rows": [{}]}, "review": None,
                      "stocks": {"analysis": None}}
        with patch.object(service, "snapshot", return_value=demo_state), patch("app.llm.analyze", side_effect=delayed_analysis):
            service.run_llm()
            self.assertTrue(entered.wait(1))
            try:
                service.start()
            finally:
                release.set()
            deadline = time.monotonic() + 2
            while service.jobs["llm"]["status"] == "running" and time.monotonic() < deadline:
                time.sleep(.005)
        self.assertEqual(service.mode, "live")
        self.assertNotEqual(service.jobs["llm"]["status"], "running")
        self.assertTrue(service.llm_result is None or service.llm_result.get("mode") == "live")

    def test_protected_interruption_allows_trend_refresh_to_resume_later(self):
        service = self.service
        service.days = ["2026-09-16", "2026-09-17", "2026-09-18"]
        service.calendar_loaded_date = "2026-09-18"
        service.prepared_date = "2026-09-18"
        service.last_trend_attempt = "2026-09-17"
        interrupted = {"date": "2026-09-17", "status": "partial", "rows": [],
                       "coverage": {"cancelled_or_protected": True}}
        with patch("app.service.build_trend_pool", return_value=interrupted):
            service.refresh_trends("2026-09-17")
            deadline = time.monotonic() + 2
            while service.jobs["trends"]["status"] == "running" and time.monotonic() < deadline:
                time.sleep(.005)
        self.assertNotEqual(service.jobs["trends"]["status"], "running")
        self.assertNotEqual(service.last_trend_attempt, "2026-09-17")


if __name__ == "__main__":
    unittest.main()
