"""Bounded official-sector evidence for an explicitly selected closed session.

Current constituent lists describe today's membership, never historical index
composition. No model, persistence, credentials, or capital-flow inference lives
in this module. Callers bind the result to the immutable source report version.
"""
from __future__ import annotations

import re
import statistics
import time
from datetime import datetime, timedelta

from .provider import SHANGHAI, iso_date
from .review import _price_trend, _snapshot_alignment, number, ratio

INDEX_CODE = re.compile(r"[0-9]{6}\.(?:TI|SH|SZ|BJ)")
STOCK_CODE = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)")
MAX_BOARDS = 3
MAX_QUOTES = 300
HISTORY_PER_BOARD = 20
MEMBER_DISPLAY = 100
LIMIT_DISPLAY = 30


def _stop(should_stop):
    if should_stop and should_stop():
        raise ValueError("板块取数已取消或进入竞价保护时段")


def _text(value, limit=300):
    return value[:limit] if isinstance(value, str) else None


def _catalog(rows):
    if not isinstance(rows, list):
        raise ValueError("官方板块目录格式无效")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("官方板块目录含无效记录")
        code, name, category = row.get("thscode"), row.get("name"), row.get("category")
        if (not isinstance(code, str) or not INDEX_CODE.fullmatch(code)
                or not isinstance(name, str) or not name.strip()
                or category not in ("industry", "cn_concept")):
            raise ValueError("官方板块目录代码、名称或类别无效")
        item = {"thscode": code, "name": name.strip()[:100], "category": category}
        if code in result and result[code] != item:
            raise ValueError("官方板块目录同一代码存在冲突")
        result[code] = item
    return sorted(result.values(), key=lambda row: row["thscode"])


def load_catalog(provider, should_stop=None):
    """Read both complete official directories; failures do not look empty."""
    rows = []
    for category in ("industry", "cn_concept"):
        _stop(should_stop)
        items = provider.catalog(category)
        _stop(should_stop)
        if not isinstance(items, list):
            raise ValueError("官方板块目录格式无效")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("官方板块目录含无效记录")
            rows.append({"thscode": item.get("thscode"), "name": item.get("name"), "category": category})
    return _catalog(rows)


def search_catalog(rows, query):
    if not isinstance(query, str) or not query.strip() or len(query) > 80:
        raise ValueError("请输入 1—80 字的板块名称或完整指数代码")
    query = query.strip()
    needle = query.casefold()
    normalized = _catalog(rows)
    matches = [row for row in normalized if (row["thscode"].casefold() == needle
               if INDEX_CODE.fullmatch(query.upper()) else needle in row["name"].casefold())]
    exact = [row["thscode"] for row in matches if needle in (row["name"].casefold(), row["thscode"].casefold())]
    return {"query": query, "matches": matches, "exact": exact,
            "unmatched_note": (None if matches else "官方行业和概念目录未找到匹配；未将输入词映射为其他板块，请改用目录中的名称或完整代码。")}


def validate_sector_codes(values):
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_BOARDS:
        raise ValueError("每次请选择 1—3 个官方板块代码")
    result = []
    for value in values:
        if not isinstance(value, str) or not INDEX_CODE.fullmatch(value.strip().upper()):
            raise ValueError("板块查询须使用官方目录中的完整指数代码")
        code = value.strip().upper()
        if code in result:
            raise ValueError("板块代码不能重复")
        result.append(code)
    return result


def _aware(value):
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        return parsed.astimezone(SHANGHAI) if isinstance(parsed, datetime) and parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _closed(day, moment):
    return day < moment.date().isoformat() or (day == moment.date().isoformat() and moment.strftime("%H:%M") >= "15:10")


def _rows_by_code(rows):
    """Preserve usable unique rows, but never call a malformed list complete."""
    if not isinstance(rows, list):
        return {}, False
    result, duplicate, valid = {}, set(), True
    for row in rows:
        code = row.get("thscode") if isinstance(row, dict) else None
        if not isinstance(code, str) or not STOCK_CODE.fullmatch(code):
            valid = False
            continue
        if code in result or code in duplicate:
            duplicate.add(code)
            result.pop(code, None)
            valid = False
            continue
        result[code] = row
    return result, valid


def _snapshot(data, date, calendar, observed_at):
    if not isinstance(data, dict) or observed_at is None:
        return {}, {"accepted": False}, False
    timestamp = data.get("timestamp")
    pages = data.get("page_timestamps", [timestamp])
    # The shared helper allows missing exact-date page stamps for older reports.
    # Targeted evidence is stricter: every contributing page must be dated.
    if (not isinstance(pages, list) or not pages or any(number(value) is None or iso_date(value) is None
            or value > observed_at.timestamp() * 1000 for value in [timestamp, *pages])):
        return {}, {"accepted": False}, False
    alignment = _snapshot_alignment(timestamp, date, calendar=calendar, now=observed_at, page_timestamps=pages)
    if not alignment["accepted"]:
        return {}, alignment, False
    rows, valid = _rows_by_code(data.get("item"))
    return rows, alignment, valid


def _daily_quote(data, code, date, previous):
    if not isinstance(data, dict) or data.get("adjust") not in (None, "forward"):
        return None
    items = data.get("item")
    if not isinstance(items, list):
        return None
    bars, duplicate = {}, set()
    for bar in items:
        day = iso_date(bar.get("date_ms")) if isinstance(bar, dict) else None
        if day not in (date, previous):
            continue
        if day in bars:
            duplicate.add(day)
        bars[day] = bar
    if date not in bars or date in duplicate:
        return None
    current = bars[date]
    close = number(current.get("close_price"))
    prior = number(bars.get(previous, {}).get("close_price")) if previous not in duplicate else None
    change = round((close / prior - 1) * 100, 4) if close is not None and close > 0 and prior is not None and prior > 0 else None
    return {"thscode": code, "price": close if close is not None and close > 0 else None,
            "price_change_ratio_pct": change, "turnover": current.get("turnover"), "volume": current.get("volume")}


def _positive(value):
    parsed = number(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _consecutive(row):
    value = number(row.get("consecutive_days")) if isinstance(row, dict) else None
    return int(value) if value is not None and value >= 1 and value.is_integer() else None


def build_sector_research(provider, codes, date, *, report, catalog=None, now=None, should_stop=None, progress=None):
    """Read at most 3 sectors, 300 stock snapshots or 60 short stock histories.

    Quote statistics use only the retrieved sample. Limit-up attribution instead
    intersects the entire valid current-member list with a complete dated pool.
    """
    codes = validate_sector_codes(codes)
    if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("板块分析日期格式无效")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise ValueError("板块分析日期无效") from None
    started = _aware(now) if now is not None else datetime.now(SHANGHAI)
    if started is None:
        raise ValueError("板块分析时钟必须包含时区")
    clock_start = time.monotonic()

    def received_now():
        return started + timedelta(seconds=max(0, time.monotonic() - clock_start))

    if not _closed(date, started):
        raise ValueError("仅支持已收盘交易日，当日请在 15:10 后读取")
    if not isinstance(report, dict) or report.get("mode") != "live" or report.get("date") != date:
        raise ValueError("板块分析须绑定同日真实复盘报告")
    counts = {"business_calls": 0, "catalog_calls": 0, "calendar_calls": 0, "member_calls": 0,
              "index_history_calls": 0, "stock_quote_calls": 0, "stock_history_calls": 0,
              "quote_code_count": 0, "historical_code_count": 0}
    warnings = []

    def fetch(kind, label, operation, target_warnings):
        _stop(should_stop)
        if progress:
            progress(label)
        counts["business_calls"] += 1
        counts[kind] += 1
        try:
            value = operation()
        except Exception:
            # Upstream text can contain credentials or misleading instructions.
            _stop(should_stop)
            target_warnings.append(label + "失败，相关数据保留缺失")
            return None
        _stop(should_stop)
        return value

    calendar = fetch("calendar_calls", "核验官方交易日历", provider.calendar, warnings)
    if (not isinstance(calendar, list) or any(not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
                                            for day in calendar)):
        raise ValueError("官方交易日历读取失败，不能确认板块分析日期")
    try:
        for day in calendar:
            datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise ValueError("官方交易日历格式无效") from None
    calendar = sorted(set(calendar))
    if date not in calendar:
        raise ValueError("所选日期不在官方交易日历内")
    latest_closed = max((day for day in calendar if _closed(day, started)), default=None)
    previous = max((day for day in calendar if day < date), default=None)
    if catalog is None:
        catalog_rows = []
        for category in ("industry", "cn_concept"):
            items = fetch("catalog_calls", "读取官方板块目录", lambda category=category: provider.catalog(category), warnings)
            if not isinstance(items, list):
                raise ValueError("官方板块目录读取失败，不能确认板块代码")
            catalog_rows.extend({"thscode": item.get("thscode"), "name": item.get("name"), "category": category}
                                for item in items if isinstance(item, dict))
            if any(not isinstance(item, dict) for item in items):
                raise ValueError("官方板块目录含无效记录")
        catalog = catalog_rows
    catalog_map = {row["thscode"]: row for row in _catalog(catalog)}
    if any(code not in catalog_map for code in codes):
        raise ValueError("所选代码不在官方行业或概念目录中")
    raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
    report_at = _aware(report.get("generated_at"))
    report_closed = report_at is not None and report_at <= received_now() and _closed(date, report_at)
    report_calendar = raw.get("calendar")
    report_calendar_valid = isinstance(report_calendar, list) and date in report_calendar
    pool_rows = raw.get("pools_by_date", {}).get(date) if isinstance(raw.get("pools_by_date"), dict) else None
    pool, pool_valid = _rows_by_code(pool_rows)
    limit_section = report.get("limit_up") if isinstance(report.get("limit_up"), dict) else {}
    declared = limit_section.get("count")
    pool_valid = bool(pool_valid and report_closed and report_calendar_valid
                      and type(declared) is int and declared == len(pool_rows)
                      and limit_section.get("status") == "ready")
    if not pool_valid:
        pool = {}
        warnings.append("目标日完整收盘涨停池未通过核验；涨停标签与数量未知，不能视为零")
    ranked, _ = _rows_by_code(limit_section.get("rows"))
    cache, cache_alignment, cache_valid = ({}, {"accepted": False}, False)
    if report_closed and report_calendar_valid:
        cache, cache_alignment, cache_valid = _snapshot(raw.get("market"), date, report_calendar, report_at)
    if cache_alignment.get("accepted") and not cache_valid:
        warnings.append("已保存行情含无效或重复代码，仅采用唯一有效记录")
    boards, all_members = [], set()
    for code in codes:
        board = {**catalog_map[code], "status": "partial", "membership_basis": "current_members_view",
                 "members_as_of": None, "member_count": None,
                 "quote_scope": {"source": "target_date_evidence", "selection": "code_order",
                                 "max_stock_quotes": MAX_QUOTES, "max_historical_per_board": HISTORY_PER_BOARD},
                 "coverage": {}, "price_trend": None, "statistics": {}, "members": [], "limit_up_members": [],
                 "warnings": ["成分为当前接口组成；历史目标日仅观察当前成员当时表现，不代表当日历史板块组成。",
                              "板块成交额与涨跌分布不能解释为资金净流入；官方资金流接口未公开。"],
                 "net_flow": None, "net_flow_status": "unavailable"}
        members = fetch("member_calls", f"读取{board['name']}当前成分", lambda code=code: provider.members(code), board["warnings"])
        board["members_as_of"] = received_now().isoformat() if members is not None else None
        mapping, complete = _rows_by_code(members)
        board["_member_map"] = mapping
        board["member_count"] = len(mapping) if members is not None else None
        board["coverage"]["membership_complete"] = complete
        if not complete:
            board["warnings"].append("当前成分读取不完整或代码异常，成员计数及涨停归属仅覆盖有效记录")
        all_members.update(mapping)
        start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=60)).date().isoformat()
        history = fetch("index_history_calls", f"读取{board['name']}指数日线", lambda code=code: provider.index_historical(code, start, date), board["warnings"])
        trend = _price_trend(history, date, start, calendar)
        trend["adjust"] = "not_applicable_index"
        quote = _daily_quote(history, code, date, previous)
        trend["change_pct"] = quote.get("price_change_ratio_pct") if quote else None
        board["price_trend"] = trend
        boards.append(board)

    # Fixed code order, independent of target-day returns or pool membership.
    selected = sorted(all_members)[:MAX_QUOTES]
    quotes, origins = {}, {}
    for code in selected:
        if code in cache:
            quotes[code] = cache[code]
            origins[code] = {"source": "saved_report_snapshot", **cache_alignment}
    missing = [code for code in selected if code not in quotes]
    if date == latest_closed:
        counts["quote_code_count"] = len(missing)
        for offset in range(0, len(missing), 100):
            batch = missing[offset:offset + 100]
            data = fetch("stock_quote_calls", "读取指定成分股行情", lambda batch=batch: provider.stock_quote(batch), warnings)
            rows, alignment, valid = _snapshot(data, date, calendar, received_now())
            if data is not None and not alignment.get("accepted"):
                warnings.append("最新行情日期不能对应目标日，未用于历史填补")
            if alignment.get("accepted") and not valid:
                warnings.append("成分行情存在异常代码，仅保留唯一有效且本次请求的记录")
            for code in batch:
                if code in rows:
                    quotes[code] = rows[code]
                    origins[code] = {"source": "official_stock_snapshot", **alignment}
    else:
        historical_codes = sorted({code for board in boards for code in sorted(board["_member_map"])[:HISTORY_PER_BOARD]
                                   if code in selected and code not in quotes})
        counts["historical_code_count"] = len(historical_codes)
        if previous is None:
            warnings.append("交易日历缺少前一交易日，无法计算历史成分日涨跌幅")
        else:
            for code in historical_codes:
                data = fetch("stock_history_calls", "读取指定成分股历史日线", lambda code=code: provider.historical(code, previous, date, adjust="forward"), warnings)
                quote = _daily_quote(data, code, date, previous)
                if quote is not None:
                    quotes[code] = quote
                    origins[code] = {"source": "official_stock_history", "date_basis": "dated_daily_bars",
                                     "date_verified": True, "data_status": "ready"}
            if historical_codes:
                warnings.append("历史同行涨跌幅按目标日与官方前一交易日前复权收盘价计算；复权数据可能修订，缺日不跨日补算。")

    for board in boards:
        mapping = board.pop("_member_map")
        rows = []
        for code in sorted(mapping):
            if code not in quotes:
                continue
            quote, origin = quotes[code], origins[code]
            known = ranked.get(code, {})
            price = number(quote.get("price", quote.get("last_price")))
            rows.append({"thscode": code, "name": _text(mapping[code].get("name"), 100),
                         "price": price if price is not None and price > 0 else None,
                         "price_change_ratio_pct": number(quote.get("price_change_ratio_pct")),
                         "turnover": _positive(quote.get("turnover")), "volume": _positive(quote.get("volume")),
                         "source": origin["source"], "date_basis": origin.get("date_basis"),
                         "date_verified": origin.get("date_verified", False), "data_status": origin.get("data_status"),
                         "limit_up": code in pool if pool_valid else None,
                         "consecutive_days": _consecutive(known) if pool_valid and code in pool else None,
                         "consecutive_lower_bound": known.get("consecutive_lower_bound") if pool_valid and code in pool and type(known.get("consecutive_lower_bound")) is bool else None})
        changes = [row["price_change_ratio_pct"] for row in rows if row["price_change_ratio_pct"] is not None]
        amounts = [row["turnover"] for row in rows if row["turnover"] is not None]
        up_codes = sorted(set(mapping) & set(pool)) if pool_valid else []
        limits = []
        for code in up_codes:
            item, known = pool[code], ranked.get(code, {})
            seal, peak = _positive(item.get("seal_money")), _positive(item.get("max_seal_money"))
            retention = round(seal / peak * 100, 4) if seal is not None and peak is not None and 0 <= seal <= peak and peak > 0 else None
            limits.append({"thscode": code, "name": _text(item.get("name") or mapping[code].get("name"), 100),
                           "seal_money": seal, "max_seal_money": peak, "seal_retention_pct": retention,
                           "limit_up_time": _text(item.get("limit_up_time"), 10),
                           "limit_up_reason": _text(item.get("limit_up_reason"), 1000),
                           "consecutive_days": _consecutive(known),
                           "consecutive_lower_bound": known.get("consecutive_lower_bound") if type(known.get("consecutive_lower_bound")) is bool else None})
        board["statistics"] = {"total_members": board["member_count"], "quoted_count": len(rows),
                               "valid_change_count": len(changes),
                               "advancing": sum(value > 0 for value in changes) if changes else None,
                               "declining": sum(value < 0 for value in changes) if changes else None,
                               "unchanged": sum(value == 0 for value in changes) if changes else None,
                               "mean_change_pct": round(statistics.mean(changes), 4) if changes else None,
                               "median_change_pct": round(statistics.median(changes), 4) if changes else None,
                               "turnover_sum": sum(amounts) if amounts else None, "turnover_known_count": len(amounts),
                               "limit_up_count": len(up_codes) if pool_valid and board["member_count"] is not None else None,
                               "consecutive_known_count": sum(_consecutive(ranked.get(code)) is not None for code in up_codes) if pool_valid and board["member_count"] is not None else None,
                               "consecutive_count": (sum((_consecutive(ranked.get(code)) or 0) >= 2 for code in up_codes)
                                                      if pool_valid and board["member_count"] is not None and
                                                      (not up_codes or any(_consecutive(ranked.get(code)) is not None for code in up_codes)) else None)}
        if pool_valid and up_codes and board["statistics"]["consecutive_known_count"] < len(up_codes):
            board["warnings"].append("部分涨停成员缺少可核实的严格连板数，连板数量只统计已知记录；全部未知时为空")
        board["members"] = rows[:MEMBER_DISPLAY]
        board["limit_up_members"] = limits[:LIMIT_DISPLAY]
        board["coverage"].update({"requested_count": len(set(mapping) & set(selected)), "quoted_count": len(rows),
                                  "quote_coverage_pct": ratio(len(rows), len(mapping)), "shown_count": len(board["members"]),
                                  "truncated": len(rows) > MEMBER_DISPLAY, "limit_pool_complete": pool_valid,
                                  "limit_up_shown_count": len(board["limit_up_members"]), "limit_up_truncated": len(limits) > LIMIT_DISPLAY})
        if len(rows) < len(mapping):
            board["warnings"].append("同行行情仅覆盖已取得样本；未取得行情不作为下跌、不涨停或零成交额")
        if any(row["data_status"] == "provisional" for row in rows):
            board["warnings"].append("部分行情日期为官方日历支持的休市日推断，保留 provisional 标记")
        ready = (bool(mapping) and board["coverage"]["membership_complete"] and pool_valid and len(changes) == len(mapping)
                 and board["price_trend"]["status"] == "ready" and all(row["data_status"] == "ready" for row in rows))
        has_quote = any(any(row[key] is not None for key in ("price", "price_change_ratio_pct", "turnover", "volume")) for row in rows)
        has_index = board["price_trend"].get("as_of") == date and board["price_trend"].get("close") is not None
        has_pool_evidence = bool(mapping) and pool_valid
        board["status"] = ("ready" if ready else "partial" if has_quote or has_index or has_pool_evidence else "unavailable")
        if board["status"] == "unavailable":
            board["warnings"].append("未取得可供当日强弱比较的行情、指数收盘或成员涨停交集证据")
    _stop(should_stop)
    status = ("unavailable" if all(board["status"] == "unavailable" for board in boards)
              else "ready" if all(board["status"] == "ready" for board in boards) else "partial")
    return {"date": date, "mode": "live", "status": status,
            "generated_at": received_now().isoformat(), "boards": boards, "coverage": counts,
            "warnings": list(dict.fromkeys(warnings)),
            "definition": "指定官方行业/概念的当前成分，在所选已收盘日的行情样本与完整涨停池交集；不受原报告板块前30名截断。统计不是历史成分回测，成交额不是资金净流入。业务调用数不含适配器内部重试。"}
