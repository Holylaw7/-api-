"""Pure, evidence-aware comparisons and local collection readiness checks.

No provider calls, disk access, system clock, or credential inspection belong
here. Report comparisons describe two observations, never a future forecast.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))
_STOCK = re.compile(r"\d{6}\.(?:SH|SZ|BJ)\Z")
_SECTOR = re.compile(r"[A-Z0-9]+\.[A-Z]+\Z")


def _dict(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _count(value):
    value = _number(value)
    return int(value) if value is not None and value >= 0 and value == int(value) else None


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _datetime(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(SHANGHAI) if parsed.tzinfo else None
    except ValueError:
        return None


def _delta(current, previous):
    return round(current - previous, 4) if current is not None and previous is not None else None


def _aggregate_status(statuses):
    statuses = list(statuses)
    if not statuses or all(value == "unavailable" for value in statuses):
        return "unavailable"
    return "ready" if all(value == "ready" for value in statuses) else "partial"


def _alignment(value, day):
    value = _dict(value)
    effective = value.get("effective_trade_date") or value.get("snapshot_date")
    if not day or effective != day or value.get("data_status") == "unavailable":
        return "unavailable"
    return "ready" if value.get("date_verified") is True else "provisional"


def _complete_pool(report):
    pool = _dict(report.get("limit_up"))
    rows = pool.get("rows")
    declared = _count(pool.get("count"))
    mapping = {}
    invalid = not isinstance(rows, list)
    for row in rows if isinstance(rows, list) else []:
        code = _dict(row).get("thscode")
        if not isinstance(code, str) or not _STOCK.fullmatch(code) or code in mapping:
            invalid = True
            continue
        mapping[code] = row
    complete = (not invalid and pool.get("status") == "ready" and declared is not None
                and declared == len(rows) == len(mapping))
    return mapping, complete, declared


def _stock_summary(value):
    if value is None:
        return None
    days = _count(value.get("consecutive_days"))
    return {"score": _number(value.get("score")),
            "consecutive_days": days if days and days >= 1 else None,
            "consecutive_lower_bound": value.get("consecutive_lower_bound") is not False}


def _limit_comparison(current, previous):
    current_map, current_complete, current_count = _complete_pool(current)
    previous_map, previous_complete, previous_count = _complete_pool(previous)
    result = {"status": "unavailable", "current_count": current_count,
              "previous_count": previous_count, "retained_count": None, "new_count": None,
              "exited_count": None, "retained": [], "new": [], "exited": [],
              "coverage": {"current_complete": current_complete, "previous_complete": previous_complete,
                           "current_valid_rows": len(current_map), "previous_valid_rows": len(previous_map)},
              "definition": "共同两期涨停、当期新出现、从对比池退出；不表示期间每日连续涨停或已证实断板。"}
    if not current_complete or not previous_complete:
        return result
    for key, codes in (("retained", current_map.keys() & previous_map.keys()),
                       ("new", current_map.keys() - previous_map.keys()),
                       ("exited", previous_map.keys() - current_map.keys())):
        result[key] = [{"thscode": code, "name": (current_map.get(code) or previous_map[code]).get("name"),
                        "current": _stock_summary(current_map.get(code)),
                        "previous": _stock_summary(previous_map.get(code))}
                       for code in sorted(codes)]
        result[key + "_count"] = len(result[key])
    result["status"] = "ready"
    return result


def _metric_value(report, identifier):
    market = _dict(report.get("market"))
    if identifier in ("consecutive_count", "max_consecutive"):
        mapping, complete, _ = _complete_pool(report)
        values = [_stock_summary(row) for row in mapping.values()]
        if not complete or any(row["consecutive_days"] is None for row in values):
            return None, "unavailable"
        value = (sum(row["consecutive_days"] >= 2 for row in values) if identifier == "consecutive_count"
                 else max((row["consecutive_days"] for row in values), default=0))
        return value, "provisional" if any(row["consecutive_lower_bound"] for row in values) else "ready"
    if identifier == "limit_up_count":
        _, complete, count = _complete_pool(report)
        return count, "ready" if complete else "unavailable"
    value = _number(market.get(identifier))
    if value is None or (identifier != "seal_rate_pct" and value < 0):
        return None, "unavailable"
    if identifier in ("limit_down_count", "limit_break_count", "seal_rate_pct"):
        if identifier == "seal_rate_pct" and not 0 <= value <= 100:
            return None, "unavailable"
        return value, "ready"
    aligned = _alignment(market, report.get("date"))
    if aligned == "ready" and market.get("status") != "ready":
        aligned = "provisional"
    return value, aligned


def _market_comparison(current, previous):
    metrics = (("advancing", "上涨家数", "家"), ("declining", "下跌家数", "家"),
               ("unchanged", "平盘家数", "家"), ("total_turnover", "市场成交额", "元"),
               ("limit_up_count", "涨停家数", "家"), ("limit_down_count", "跌停家数", "家"),
               ("limit_break_count", "炸板家数", "家"), ("seal_rate_pct", "封板比例", "百分点"),
               ("consecutive_count", "严格连板家数", "家"), ("max_consecutive", "最高严格连板数", "板"))
    rows = []
    for identifier, label, unit in metrics:
        now, now_status = _metric_value(current, identifier)
        before, before_status = _metric_value(previous, identifier)
        status = ("unavailable" if "unavailable" in (now_status, before_status)
                  else "provisional" if "provisional" in (now_status, before_status) else "ready")
        rows.append({"id": identifier, "label": label, "unit": unit, "current": now,
                     "previous": before, "delta": _delta(now, before) if status != "unavailable" else None,
                     "status": status})
    return {"status": _aggregate_status(row["status"] for row in rows), "rows": rows}


def _sector_map(report):
    rows = _dict(report.get("sectors")).get("rows")
    result, ambiguous = {}, set()
    for row in rows if isinstance(rows, list) else []:
        code = _dict(row).get("thscode")
        if not isinstance(code, str) or not _SECTOR.fullmatch(code):
            continue
        if code in result:
            ambiguous.add(code)
        result[code] = row
    for code in ambiguous:
        result.pop(code, None)
    return result


def _sector_comparison(current, previous):
    now_map, before_map = _sector_map(current), _sector_map(previous)
    rows, comparable = [], 0
    common = now_map.keys() & before_map.keys()
    for code in sorted(common):
        now, before = now_map[code], before_map[code]
        statuses = (_alignment(now, current.get("date")), _alignment(before, previous.get("date")))
        status = ("unavailable" if "unavailable" in statuses
                  else "provisional" if "provisional" in statuses else "ready")
        now_change, before_change = _number(now.get("price_change_ratio_pct")), _number(before.get("price_change_ratio_pct"))
        now_turnover, before_turnover = _number(now.get("turnover")), _number(before.get("turnover"))
        now_turnover = now_turnover if now_turnover is not None and now_turnover >= 0 else None
        before_turnover = before_turnover if before_turnover is not None and before_turnover >= 0 else None
        now_rank, before_rank = _count(now.get("rank")), _count(before.get("rank"))
        now_rank, before_rank = now_rank or None, before_rank or None
        aligned = status != "unavailable"
        change_delta = _delta(now_change, before_change) if aligned else None
        turnover_delta = (round((now_turnover / before_turnover - 1) * 100, 4)
                          if aligned and now_turnover is not None and before_turnover else None)
        rank_delta = _delta(before_rank, now_rank) if aligned else None
        if aligned and all(value is None for value in (change_delta, turnover_delta, rank_delta)):
            status = "unavailable"
        if status != "unavailable":
            comparable += 1
        rows.append({"thscode": code, "name": now.get("name") or before.get("name"),
                     "current_rank": now_rank, "previous_rank": before_rank, "rank_change": rank_delta,
                     "current_change_pct": now_change, "previous_change_pct": before_change,
                     "change_delta_pp": change_delta, "current_turnover": now_turnover,
                     "previous_turnover": before_turnover, "turnover_change_pct": turnover_delta,
                     "status": status})
    rows.sort(key=lambda row: (row["status"] == "unavailable", row["change_delta_pp"] is None,
                               -abs(row["change_delta_pp"] or 0), row["thscode"]))
    return {"status": _aggregate_status(row["status"] for row in rows), "rows": rows[:30],
            "coverage": {"current_count": len(now_map), "previous_count": len(before_map),
                         "common_count": len(common), "comparable_count": comparable,
                         "returned_count": min(len(rows), 30)},
            "warnings": ["先在两期完整可得板块表按代码匹配，再展示涨幅变化绝对值前30项；未列出不等于缺失。",
                         "名次变化仅为各期样本内的相对排名，样本或覆盖不同会影响比较；成交额与排名不代表净资金流。",
                         "日期推断值标为 provisional；缺少对应报告日行情的板块不计算变化。"]}


def compare_reports(current, previous):
    """Compare complete pools and date-aligned metrics without mutating input.

    Only explicit matching ``mode`` and ordered ISO report dates are accepted.
    Different dates alone cannot establish adjacent trading sessions.
    """
    current, previous = _dict(current), _dict(previous)
    current_date, previous_date = _date(current.get("date")), _date(previous.get("date"))
    mode = current.get("mode")
    result = {"status": "unavailable", "current_date": current_date, "previous_date": previous_date,
              "mode": mode if mode in ("live", "demo") else None, "adjacent_sessions": None,
              "warnings": [], "market": {"status": "unavailable", "rows": []},
              "limit_up": {"status": "unavailable", "current_count": None, "previous_count": None,
                           "retained_count": None, "new_count": None, "exited_count": None,
                           "retained": [], "new": [], "exited": [], "coverage": {}},
              "sectors": {"status": "unavailable", "rows": [], "coverage": {}, "warnings": []}}
    if not current_date or not previous_date or current_date <= previous_date:
        result["warnings"].append("对比需要有效日期，且当期日期必须晚于基准日期。")
        return result
    if mode not in ("live", "demo") or previous.get("mode") != mode:
        result["warnings"].append("两期必须明确属于同一数据模式，不能混用演示与实盘。")
        return result
    calendar = _dict(current.get("raw")).get("calendar")
    if isinstance(calendar, list) and calendar and all(_date(day) for day in calendar) and current_date in calendar:
        earlier = sorted(set(day for day in calendar if day < current_date))
        if earlier:
            result["adjacent_sessions"] = earlier[-1] == previous_date
    if result["adjacent_sessions"] is not True:
        result["warnings"].append("未证实为相邻交易日；共同两期涨停不代表期间持续连板，新出现或退出不等于首板或断板。")
    result["market"] = _market_comparison(current, previous)
    result["limit_up"] = _limit_comparison(current, previous)
    result["sectors"] = _sector_comparison(current, previous)
    if result["limit_up"]["status"] != "ready":
        result["warnings"].append("至少一期涨停池缺失、代码无效、重复或数量不完整，不推断新增与退出。")
    if any(row["status"] == "provisional" for row in result["market"]["rows"]):
        result["warnings"].append("部分盘面日期为推断或连板数为下限，相关变化只能作暂定比较。")
    result["status"] = _aggregate_status(result[key]["status"] for key in ("market", "limit_up", "sectors"))
    return result


def readiness(snapshot, internal=None):
    """Return local readiness diagnostics using the supplied Shanghai timestamp.

    ``internal`` may provide prepared_date and calendar_loaded_date. This is a
    check of known state, never proof of API authorization or future readiness.
    """
    snapshot, internal = _dict(snapshot), _dict(internal)
    current = _datetime(snapshot.get("now"))
    checks = []

    def add(identifier, label, status, message):
        checks.append({"id": identifier, "label": label, "status": status, "message": message})

    if snapshot.get("mode") == "demo":
        add("mode", "数据模式", "info", "当前为演示；这些数据不用于判断实盘采集是否就绪。")
        return {"status": "info", "summary": "演示模式", "checked_at": snapshot.get("now"), "checks": checks}
    if snapshot.get("mode") != "live":
        add("mode", "数据模式", "error", "数据模式未知，无法确认这是否为实盘状态；不继续判断交易日与采集就绪。")
        return {"status": "error", "summary": "无法确认数据模式", "checked_at": snapshot.get("now"), "checks": checks}
    add("credentials", "数据接入", "ok" if snapshot.get("configured") else "error",
        "已配置数据凭据；不代表本次远程鉴权已验证。" if snapshot.get("configured") else "未配置数据 API Key，请在策略与接入中保存。")
    if current is None:
        add("clock", "本机时点", "error", "缺少有效且含时区的当前时间，无法核对采集时段。")
    calendar = _dict(snapshot.get("calendar"))
    today = current.date().isoformat() if current else None
    checked_on = internal.get("calendar_loaded_date") or calendar.get("checked_on")
    dates = calendar.get("dates")
    calendar_valid = (today is not None and checked_on == today and isinstance(dates, list) and bool(dates)
                      and all(_date(day) and day <= today for day in dates)
                      and isinstance(calendar.get("today_is_trading"), bool)
                      and calendar.get("today_is_trading") == (today in dates))
    if not calendar_valid:
        add("calendar", "交易日历", "warn", "今日官方交易日历尚未核验；不能仅按星期判断是否开市。")
        trading = None
    else:
        trading = calendar["today_is_trading"]
        add("calendar", "交易日历", "ok" if trading else "info",
            "今日已核验为交易日。" if trading else "今日已核验为休市日，无需竞价终态。")
    running = snapshot.get("running") is True
    clock = current.strftime("%H:%M:%S") if current else ""
    active = trading is True and "09:15:00" <= clock <= "09:26:00"
    before = trading is True and clock < "09:15:00"
    after = trading is True and clock > "09:26:00"
    add("monitor", "自动监测", "ok" if running else "error" if active else "warn" if before else "info",
        "监测已启动。" if running else "监测未启动；请启动后保持电脑联网且不休眠。")
    auction = _dict(snapshot.get("auction"))
    summary = _dict(auction.get("summary"))
    universe = _count(auction.get("universe_count"))
    prepared = internal.get("prepared_date")
    session_matches = bool(today and auction.get("date") == today)
    if trading is True:
        pool_ready = bool(universe and session_matches and (prepared == today if prepared is not None else True))
        add("universe", "当日关注池", "ok" if pool_ready else "error" if active else "warn",
            f"本会话关注 {universe} 只股票。" if pool_ready else "当日关注池未准备好或为空，请检查准备任务。")
        if active:
            receipt = _datetime(summary.get("last_received_at") or auction.get("last_receipt"))
            age = (current - receipt).total_seconds() if receipt else None
            if not session_matches or age is None or age < 0:
                add("freshness", "本地接收时效", "warn", "尚无本会话有效接收时间，不能确认正在获得新观察。")
            else:
                add("freshness", "本地接收时效", "warn" if age > 30 else "ok",
                    f"距最近一批本地接收 {round(age, 1)} 秒；这是接收时效，不是交易所行情延迟。")
            coverage = _number(auction.get("coverage"))
            valid_coverage = coverage is not None and 0 <= coverage <= 1 and session_matches
            add("coverage", "采集覆盖", "ok" if valid_coverage and coverage == 1 else "warn",
                f"本会话已处理关注池的 {coverage * 100:.1f}%；不代表十分钟完整覆盖。" if valid_coverage else "本会话采集覆盖尚不可确认。")
        if active and clock >= "09:25:00" or after:
            final_count = _count(auction.get("final_count"))
            final_ready = bool(session_matches and universe and final_count == universe)
            add("final", "竞价终态", "ok" if final_ready else "info" if active else "warn",
                f"{final_count}/{universe} 只已取得终态。" if final_ready else "部分或全部股票缺少本日终态；只能参考有日期和质量标记的观察。")
        if _dict(snapshot.get("config")).get("universe") == "watchlist":
            add("trend_pool", "趋势候选池", "info", "当前仅自选，不要求准备趋势候选池。")
        else:
            trend_pool = _dict(_dict(snapshot.get("stocks")).get("trend_pool"))
            previous = max((day for day in dates if day < today), default=None) if calendar_valid else None
            pool_date = trend_pool.get("date")
            # After today's close the refreshed pool may already target tomorrow.
            expected = today if clock >= "15:00:00" and pool_date == today else previous
            usable = expected is not None and pool_date == expected and trend_pool.get("status") in ("ready", "partial")
            add("trend_pool", "趋势候选池", "ok" if usable and trend_pool.get("status") == "ready" else "warn",
                f"候选池截止 {pool_date}，状态 {trend_pool.get('status')}；属于有限样本筛选。" if usable else "缺少与会话匹配的已收盘日趋势池；昨日涨停与手动关注仍可独立使用。")
    observations = _count(summary.get("observation_count", summary.get("observations")))
    if after and (not session_matches or not observations):
        add("observations", "本日竞价留存", "warn", "本机未留存本日竞价观察；盘后启动不能补采此前十分钟。")
    errors = sum(row["status"] == "error" for row in checks)
    warns = sum(row["status"] == "warn" for row in checks)
    status = "error" if errors else "warn" if warns else "ok" if trading is True else "info"
    return {"status": status, "summary": f"{errors} 项需处理，{warns} 项提醒" if errors or warns
            else "今日采集检查通过" if trading is True else "本机状态已检查",
            "checked_at": snapshot.get("now"), "checks": checks}
