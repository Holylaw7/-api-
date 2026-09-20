import copy
import unittest
from datetime import datetime, timedelta

from app.provider import APIError, SHANGHAI, date_ms
from app.sector_research import build_sector_research, load_catalog, search_catalog, validate_sector_codes


DATE = "2026-09-18"
NOW = datetime(2026, 9, 20, 16, tzinfo=SHANGHAI)
CATALOG = [{"thscode": "885738.TI", "name": "PCB概念", "category": "cn_concept"},
           {"thscode": "881100.TI", "name": "电子元件", "category": "industry"},
           {"thscode": "885999.TI", "name": "测试概念", "category": "cn_concept"}]


def calendar():
    day, rows = datetime(2026, 7, 20), []
    while day.date().isoformat() <= DATE:
        if day.weekday() < 5:
            rows.append(day.date().isoformat())
        day += timedelta(days=1)
    return rows


def stock(index, **values):
    return {"thscode": f"600{index:03}.SH", "name": f"成员{index}", **values}


def market(rows, day=DATE):
    return {"timestamp": date_ms(day) + 15 * 3600_000, "item": rows, "total": len(rows)}


def report(date=DATE, rows=None, quotes=None):
    pool = rows if rows is not None else [stock(1, seal_money=10, max_seal_money=20, continue_day_text="5天4板")]
    return {"date": date, "mode": "live", "generated_at": date + "T16:00:00+08:00",
            "raw": {"calendar": calendar(), "pools_by_date": {date: pool}, "market": market(quotes or [], date)},
            "limit_up": {"status": "ready", "count": len(pool),
                         "rows": [{**row, "consecutive_days": 2, "consecutive_lower_bound": True} for row in pool]}}


class Provider:
    def __init__(self):
        self.calls = []
        self.members_by_code = {row["thscode"]: [stock(1), stock(2)] for row in CATALOG}
        self.failures = set()
        self.quote_date = DATE
        self.quote_rows = None
        self.histories = {}

    def call(self, name, *args):
        self.calls.append((name, *args))
        if name in self.failures:
            raise APIError("sensitive third-party error must not appear")

    def calendar(self):
        self.call("calendar")
        return calendar()

    def catalog(self, category):
        self.call("catalog", category)
        return [{"thscode": row["thscode"], "name": row["name"]} for row in CATALOG if row["category"] == category]

    def members(self, code):
        self.call("members", code)
        return copy.deepcopy(self.members_by_code[code])

    def index_historical(self, code, start, end):
        self.call("index_historical", code, start, end)
        return {"adjust": None, "item": [{"date_ms": date_ms(day), "close_price": 100 + i, "volume": 1000}
                                         for i, day in enumerate(calendar()) if start <= day <= end]}

    def stock_quote(self, codes):
        self.call("stock_quote", list(codes))
        return market(copy.deepcopy(self.quote_rows) if self.quote_rows is not None else
                      [{"thscode": code, "last_price": 20, "price_change_ratio_pct": 2,
                        "turnover": 100, "volume": 10} for code in codes], self.quote_date)

    def historical(self, code, start, end, *, adjust):
        self.call("historical", code, start, end, adjust)
        return self.histories.get(code, {"adjust": adjust, "item": [
            {"date_ms": date_ms(start), "close_price": 10, "volume": 10, "turnover": 100},
            {"date_ms": date_ms(end), "close_price": 11, "volume": 10, "turnover": 110}]})


class SectorResearchTests(unittest.TestCase):
    def build(self, provider=None, source=None, codes=None, date=DATE, **kwargs):
        return build_sector_research(provider or Provider(), codes or ["885738.TI"], date,
                                     report=source if source is not None else report(date),
                                     catalog=CATALOG, now=NOW, **kwargs)

    def test_catalog_two_full_categories_and_search_no_invented_mapping(self):
        provider = Provider()
        rows = load_catalog(provider)
        self.assertEqual(len(rows), 3)
        self.assertEqual(provider.calls, [("catalog", "industry"), ("catalog", "cn_concept")])
        self.assertEqual(search_catalog(rows, "pcb")["matches"][0]["thscode"], "885738.TI")
        self.assertEqual(search_catalog(rows, "885738.ti")["exact"], ["885738.TI"])
        missing = search_catalog(rows, "MLCC")
        self.assertEqual(missing["matches"], [])
        self.assertTrue(missing["unmatched_note"])

    def test_validation_full_official_codes_only(self):
        for codes in ([], ["885738"], "885738.TI", ["885738.TI"] * 2, ["885738.TI"] * 4, [True]):
            with self.subTest(codes=codes), self.assertRaises(ValueError):
                validate_sector_codes(codes)
        self.assertEqual(validate_sector_codes(["885738.ti"]), ["885738.TI"])
        with self.assertRaises(ValueError):
            self.build(codes=["000300.SH"])

    def test_complete_peers_and_strict_consecutive_not_raw_days_boards(self):
        provider = Provider()
        result = self.build(provider)
        board = result["boards"][0]
        self.assertEqual(result["status"], "ready")
        self.assertEqual(board["statistics"]["limit_up_count"], 1)
        self.assertEqual(board["statistics"]["consecutive_count"], 1)
        self.assertEqual(board["limit_up_members"][0]["consecutive_days"], 2)
        self.assertEqual(board["limit_up_members"][0]["seal_retention_pct"], 50)
        self.assertEqual(board["membership_basis"], "current_members_view")
        self.assertTrue(board["members_as_of"].startswith("2026-09-20"))
        self.assertEqual(board["price_trend"]["adjust"], "not_applicable_index")
        self.assertEqual(board["members"][0]["price"], 20)
        self.assertIsNone(board["net_flow"])
        self.assertEqual(result["coverage"]["business_calls"], len(provider.calls))

    def test_saved_snapshot_used_without_another_finance_quote(self):
        provider = Provider()
        source = report(quotes=[stock(1, price=13, price_change_ratio_pct=0, turnover=0),
                                stock(2, price_change_ratio_pct=None, turnover=None)])
        result = self.build(provider, source)
        board = result["boards"][0]
        self.assertFalse(any(call[0] == "stock_quote" for call in provider.calls))
        self.assertEqual(board["statistics"]["valid_change_count"], 1)
        self.assertEqual(board["statistics"]["unchanged"], 1)
        self.assertEqual(board["statistics"]["turnover_sum"], 0)
        self.assertEqual(board["members"][0]["source"], "saved_report_snapshot")
        self.assertEqual(result["status"], "partial")

    def test_old_target_uses_bounded_history_and_never_current_snapshot(self):
        provider = Provider()
        provider.members_by_code["885738.TI"] = [stock(i) for i in range(1, 31)]
        result = self.build(provider, date="2026-09-17")
        board = result["boards"][0]
        self.assertFalse(any(call[0] == "stock_quote" for call in provider.calls))
        histories = [call for call in provider.calls if call[0] == "historical"]
        self.assertEqual(len(histories), 20)
        self.assertTrue(all(call[2:5] == ("2026-09-16", "2026-09-17", "forward") for call in histories))
        self.assertEqual(board["coverage"]["quoted_count"], 20)
        self.assertEqual(board["statistics"]["mean_change_pct"], 10)
        self.assertEqual(board["membership_basis"], "current_members_view")

    def test_three_boards_no_more_than_sixty_historical_requests(self):
        provider = Provider()
        for index, row in enumerate(CATALOG):
            provider.members_by_code[row["thscode"]] = [stock(i) for i in range(index * 100, index * 100 + 30)]
        result = self.build(provider, date="2026-09-17", codes=[row["thscode"] for row in CATALOG])
        self.assertEqual(result["coverage"]["stock_history_calls"], 60)
        self.assertFalse(any(call[0] == "stock_quote" for call in provider.calls))
        self.assertTrue(all(board["coverage"]["quoted_count"] == 20 for board in result["boards"]))

    def test_no_previous_session_bar_no_cross_day_return(self):
        provider = Provider()
        provider.histories["600001.SH"] = {"adjust": "forward", "item": [
            {"date_ms": date_ms("2026-09-15"), "close_price": 10},
            {"date_ms": date_ms("2026-09-17"), "close_price": 30}]}
        result = self.build(provider, date="2026-09-17")
        member = result["boards"][0]["members"][0]
        self.assertIsNone(member["price_change_ratio_pct"])
        self.assertEqual(member["price"], 30)

    def test_all_members_pool_intersection_independent_of_quote_sample_and_display(self):
        provider = Provider()
        provider.members_by_code["885738.TI"] = [stock(i) for i in range(400)]
        source = report(rows=[stock(i) for i in range(340, 380)])
        result = self.build(provider, source)
        board = result["boards"][0]
        self.assertEqual(result["coverage"]["quote_code_count"], 300)
        self.assertEqual(result["coverage"]["stock_quote_calls"], 3)
        self.assertEqual(board["coverage"]["quoted_count"], 300)
        self.assertEqual(board["coverage"]["shown_count"], 100)
        self.assertTrue(board["coverage"]["truncated"])
        self.assertEqual(board["statistics"]["limit_up_count"], 40)
        self.assertEqual(len(board["limit_up_members"]), 30)
        self.assertTrue(board["coverage"]["limit_up_truncated"])

    def test_missing_or_invalid_pool_unknown_never_zero(self):
        for kind in ("missing", "duplicate", "intraday", "count", "mode"):
            source = report()
            if kind == "missing":
                source["raw"]["pools_by_date"][DATE] = None
            elif kind == "duplicate":
                source["raw"]["pools_by_date"][DATE] *= 2
                source["limit_up"]["count"] = 2
            elif kind == "intraday":
                source["generated_at"] = DATE + "T15:05:00+08:00"
            elif kind == "count":
                source["limit_up"]["count"] = 999
            else:
                source["mode"] = "demo"
                with self.assertRaises(ValueError):
                    self.build(source=source)
                continue
            with self.subTest(kind=kind):
                board = self.build(source=source)["boards"][0]
                self.assertIsNone(board["statistics"]["limit_up_count"])
                self.assertTrue(all(row["limit_up"] is None for row in board["members"]))

    def test_empty_complete_pool_real_zero(self):
        board = self.build(source=report(rows=[]))["boards"][0]
        self.assertEqual(board["statistics"]["limit_up_count"], 0)
        self.assertTrue(all(row["limit_up"] is False for row in board["members"]))

    def test_unknown_strict_consecutive_not_counted_as_zero(self):
        source = report()
        source["limit_up"]["rows"][0].pop("consecutive_days")
        board = self.build(source=source)["boards"][0]
        self.assertEqual(board["statistics"]["consecutive_known_count"], 0)
        self.assertIsNone(board["statistics"]["consecutive_count"])
        self.assertIsNone(board["limit_up_members"][0]["consecutive_days"])

    def test_cache_alignment_uses_original_report_clock_not_current_clock(self):
        provider = Provider()
        source = report(quotes=[stock(1, price_change_ratio_pct=6), stock(2, price_change_ratio_pct=6)])
        source["generated_at"] = "2026-09-19T16:00:00+08:00"
        source["raw"]["market"]["timestamp"] = date_ms("2026-09-19") + 15 * 3600_000
        result = self.build(provider, source)
        board = result["boards"][0]
        self.assertFalse(any(call[0] == "stock_quote" for call in provider.calls))
        self.assertEqual(board["members"][0]["data_status"], "provisional")
        self.assertFalse(board["members"][0]["date_verified"])
        self.assertEqual(board["statistics"]["mean_change_pct"], 6)

    def test_mixed_or_future_snapshot_not_accepted(self):
        provider = Provider()
        provider.quote_date = "2026-09-21"
        source = report(quotes=[stock(1, price_change_ratio_pct=999)])
        source["raw"]["market"]["page_timestamps"] = [date_ms(DATE), date_ms("2026-09-17")]
        result = self.build(provider, source)
        board = result["boards"][0]
        self.assertEqual(board["members"], [])
        self.assertIsNone(board["statistics"]["mean_change_pct"])
        self.assertTrue(result["warnings"])

    def test_bad_members_and_quote_duplicates_not_counted_complete(self):
        provider = Provider()
        provider.members_by_code["885738.TI"] += [stock(2), {"thscode": "600003"}]
        provider.quote_rows = [stock(1, price_change_ratio_pct=3), stock(1, price_change_ratio_pct=4)]
        board = self.build(provider)["boards"][0]
        self.assertFalse(board["coverage"]["membership_complete"])
        self.assertEqual(board["member_count"], 1)
        self.assertEqual(board["members"], [])

    def test_failure_per_item_and_no_exception_text_leak(self):
        provider = Provider()
        provider.failures = {"index_historical"}
        result = self.build(provider)
        self.assertEqual(result["boards"][0]["coverage"]["quoted_count"], 2)
        self.assertEqual(result["boards"][0]["price_trend"]["status"], "unavailable")
        self.assertNotIn("sensitive", str(result))

    def test_all_failed_unavailable_but_valid_index_keeps_partial(self):
        provider = Provider()
        provider.failures = {"members", "index_historical", "stock_quote"}
        result = self.build(provider)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["boards"][0]["status"], "unavailable")
        provider.failures.remove("index_historical")
        result = self.build(provider)
        self.assertEqual(result["status"], "partial")
        self.assertIsNotNone(result["boards"][0]["price_trend"]["close"])

    def test_membership_alone_not_price_evidence_but_complete_empty_pool_is(self):
        provider = Provider()
        provider.failures = {"index_historical", "stock_quote"}
        source = report()
        source["raw"]["pools_by_date"][DATE] = None
        result = self.build(provider, source)
        self.assertEqual(result["status"], "unavailable")
        result = self.build(provider, report(rows=[]))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["boards"][0]["statistics"]["limit_up_count"], 0)

    def test_cancellation_checked_before_and_after_request(self):
        provider = Provider()
        with self.assertRaisesRegex(ValueError, "取消"):
            self.build(provider, should_stop=lambda: True)
        self.assertEqual(provider.calls, [])
        with self.assertRaisesRegex(ValueError, "取消"):
            self.build(provider, should_stop=lambda: len(provider.calls) >= 1)
        self.assertEqual(provider.calls, [("calendar",)])

    def test_nontrading_future_and_before_close_rejected(self):
        provider = Provider()
        with self.assertRaises(ValueError):
            self.build(provider, date="2026-09-19")
        with self.assertRaises(ValueError):
            self.build(provider, date="2026-09-21")
        with self.assertRaises(ValueError):
            build_sector_research(provider, ["885738.TI"], DATE, report=report(), catalog=CATALOG,
                                  now=datetime(2026, 9, 18, 15, 9, tzinfo=SHANGHAI))

    def test_unknown_upstream_fields_not_copied(self):
        source = report(rows=[stock(1, secret="not allowed", instruction="execute something")])
        provider = Provider()
        provider.quote_rows = [stock(1, secret="not allowed", instruction="execute something", price_change_ratio_pct=True)]
        result = self.build(provider, source)
        self.assertNotIn("not allowed", str(result))
        self.assertNotIn("execute something", str(result))
        self.assertIsNone(result["boards"][0]["members"][0]["price_change_ratio_pct"])

    def test_source_report_not_mutated(self):
        source = report()
        original = copy.deepcopy(source)
        self.build(source=source)
        self.assertEqual(source, original)


if __name__ == "__main__":
    unittest.main()
