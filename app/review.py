"""Deterministic, date-aware post-close analysis with traceable raw evidence.

This module makes no trading decisions and never treats turnover as net flow.
The caller runs build_review in a background worker and persists its result.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Protocol

from .provider import APIError, SHANGHAI, iso_date

TREND_DAYS = 10
SECTOR_MEMBER_LIMIT = 30
HISTORICAL_SECTOR_LIMIT = 30
PRICE_LEADER_LIMIT = 20


class CapitalFlowSource(Protocol):
    """Optional future adapter; explicitly separate from participation scores.

    Return date, currency='CNY', definition, source, timestamp and rows with
    thscode, inflow, outflow, net_flow. Missing amounts must remain None.
    Only install an adapter whose provider authorizes this capability.
    """

    def sector_flows(self, date: str, sector_codes: list[str]) -> dict: ...


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def ratio(numerator, denominator):
    return round(numerator / denominator * 100, 2) if numerator is not None and denominator else None


def _percentiles(values):
    """Equal values receive equal midpoint ranks; missing values stay missing."""
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return {}
    if len(clean) == 1:
        return {clean[0]: 50.0}
    result = {}
    for value in set(clean):
        lo = clean.index(value)
        hi = len(clean) - 1 - clean[::-1].index(value)
        result[value] = (lo + hi) / 2 / (len(clean) - 1) * 100
    return result


def _weighted(factors, weights):
    available = [(value, weights[key]) for key, value in factors.items() if value is not None]
    denominator = sum(weight for _, weight in available)
    return round(sum(value * weight for value, weight in available) / denominator, 2) if denominator else None


def _early_seal(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        return None
    hour, minute = map(int, value.split(":"))
    if hour > 23 or minute > 59:
        return None
    # Trading minutes only: 09:25 auction seal is earliest, lunch is excluded.
    minutes = hour * 60 + minute
    if minutes < 565 or minutes > 900:
        return None
    elapsed = max(0, min(minutes, 690) - 570) + max(0, minutes - 780)
    return round(max(0, 1 - elapsed / 240) * 100, 2)


def _pool_maps(pools):
    return {day: ({row["thscode"]: row for row in rows if row.get("thscode")}
                  if rows is not None else None) for day, rows in pools.items()}


def consecutive_info(row, day, days, pool_maps):
    """Never equate an N-days/M-limits label with M consecutive boards."""
    code = row.get("thscode")
    available_days = [value for value in days if value <= day]
    run, bounded, gap = 0, False, False
    for value in reversed(available_days):
        mapping = pool_maps.get(value)
        if mapping is None:
            gap = True
            break
        if code not in mapping:
            bounded = True
            break
        run += 1
    text = str(row.get("continue_day_text") or "").strip()
    match = re.fullmatch(r"(\d+)连板", text)
    equal_days = re.fullmatch(r"(\d+)天(\d+)板", text)
    explicit = int(match.group(1)) if match else (1 if text == "首板" else None)
    if equal_days and equal_days.group(1) == equal_days.group(2):
        explicit = int(equal_days.group(1))
    quality = []
    if explicit is not None and explicit >= 1:
        if (bounded and explicit != run) or run > explicit:
            quality.append("连板标签与连续交易日池记录冲突，采用已观察记录")
        else:
            return {"days": explicit, "source": "明确连板标签", "lower_bound": False, "quality": quality}
    if equal_days and equal_days.group(1) != equal_days.group(2):
        quality.append(f"{text}不等于{equal_days.group(2)}连板")
    if run:
        if not bounded:
            quality.append("连续记录已到窗口边界或缺失日，连板数为可证实下限")
        return {"days": run, "source": "逐日完整涨停池验证", "lower_bound": not bounded,
                "quality": quality}
    return {"days": None, "source": "证据不足", "lower_bound": gap or not bounded,
            "quality": quality + ["未能验证严格连板数"]}


def _promotion(previous_rows, current_rows, previous_day, days, maps):
    if previous_rows is None or current_rows is None:
        return {"previous_count": None if previous_rows is None else len(previous_rows),
                "promoted_count": None, "rate_pct": None, "by_height": [], "status": "unavailable"}
    today_codes = {row.get("thscode") for row in current_rows}
    previous_codes = {row.get("thscode") for row in previous_rows}
    promoted = previous_codes & today_codes
    groups = {}
    for row in previous_rows:
        info = consecutive_info(row, previous_day, days, maps)
        height = info["days"]
        group = groups.setdefault(height, {"height": height, "previous_count": 0, "promoted_count": 0,
                                           "lower_bound_count": 0})
        group["previous_count"] += 1
        group["promoted_count"] += row.get("thscode") in promoted
        group["lower_bound_count"] += bool(info["lower_bound"])
    for group in groups.values():
        group["rate_pct"] = ratio(group["promoted_count"], group["previous_count"])
    return {"previous_count": len(previous_codes), "promoted_count": len(promoted),
            "rate_pct": ratio(len(promoted), len(previous_codes)),
            "by_height": sorted(groups.values(), key=lambda value: value["height"] or 0),
            "status": "ready", "definition": "上一交易日完整涨停池中本日再次涨停的比例，含首板晋级"}


def _snapshot_alignment(timestamp, date, *, calendar=None, now=None, page_timestamps=None):
    """Retain source time; only infer a last-session date on a verified rest day.

    The nontrading exception is an explicit, provisional inference responding to
    observed API behavior. It never rewrites source timestamps or asserts that
    the true per-security quote date has been independently verified.
    """
    raw_date = iso_date(timestamp)
    alignment = {"accepted": False, "snapshot_date": raw_date, "snapshot_timestamp": timestamp,
                 "effective_trade_date": None, "date_basis": "unavailable", "date_verified": False,
                 "data_status": "unavailable"}
    times = page_timestamps if page_timestamps is not None else [timestamp]
    if not isinstance(times, list) or not times:
        return alignment
    dates = {iso_date(value) for value in times}
    if raw_date == date and not (dates - {date, None}):
        return {**alignment, "accepted": True, "effective_trade_date": date,
                "date_basis": "snapshot_timestamp", "date_verified": True, "data_status": "ready"}
    if now is None or not calendar:
        return alignment
    today = now.date().isoformat()
    # Exact dates may use small test calendars; inference needs a plausibly full,
    # freshly fetched official calendar. Absence from a tiny/stale list is not
    # evidence that today is a nontrading day.
    try:
        parsed = [datetime.strptime(day, "%Y-%m-%d").date() for day in calendar]
    except (ValueError, TypeError):
        return alignment
    if (len(set(parsed)) < 20 or min(parsed) > now.date() - timedelta(days=30)
            or max(parsed) > now.date() or today in calendar):
        return alignment
    latest = max(parsed)
    if not 1 <= (now.date() - latest).days <= 14 or date != latest.isoformat():
        return alignment
    if raw_date != today or None in dates or dates - {date, today}:
        return alignment
    # Inference may not excuse undated pages or future timestamps. Checking all
    # page times matters because market() exposes the maximum in timestamp.
    if any(number(value) is None or value > now.timestamp() * 1000 for value in [timestamp, *times]):
        return alignment
    return {**alignment, "accepted": True, "effective_trade_date": date,
            "date_basis": "inferred_latest_closed_session", "date_verified": False,
            "data_status": "provisional"}


def _market_summary(data, date, *, calendar=None, now=None):
    empty = {"status": "unavailable", "snapshot_date": None, "total": None, "valid_count": None,
             "advancing": None, "declining": None, "unchanged": None, "missing_change": None,
             "total_turnover": None, "turnover_known_count": None, "turnover_coverage_pct": None,
             "snapshot_timestamp": None, "effective_trade_date": None, "date_basis": "unavailable",
             "date_verified": False, "data_status": "unavailable"}
    if data is None:
        return empty
    alignment = _snapshot_alignment(data.get("timestamp"), date, calendar=calendar, now=now,
                                    page_timestamps=data.get("page_timestamps", [data.get("timestamp")]))
    accepted = alignment.pop("accepted")
    empty.update(alignment)
    if not accepted:
        empty["status"] = "date_mismatch"
        return empty
    rows = data.get("item", [])
    changes = [number(row.get("price_change_ratio_pct")) for row in rows]
    valid = [value for value in changes if value is not None]
    turnover = [number(row.get("turnover")) for row in rows]
    known_turnover = [value for value in turnover if value is not None and value >= 0]
    declared = data.get("total", len(rows))
    return {**alignment, "status": "ready" if len(valid) == declared and alignment["data_status"] == "ready" else "partial",
            "timestamp": data.get("timestamp"), "total": declared, "returned_count": len(rows),
            "valid_count": len(valid), "advancing": sum(value > 0 for value in valid),
            "declining": sum(value < 0 for value in valid), "unchanged": sum(value == 0 for value in valid),
            "missing_change": max(0, declared - len(valid)),
            "total_turnover": sum(known_turnover) if known_turnover else None,
            "turnover_known_count": len(known_turnover), "turnover_coverage_pct": ratio(len(known_turnover), declared),
            "turnover_note": "仅有效行情成交额之和，成交额不等于资金净流入；停牌及缺值不补零"}


def _limit_ranking(rows, date, days, maps):
    if rows is None:
        return None
    seal_rank = _percentiles([number(row.get("seal_money")) for row in rows])
    result = []
    for row in rows:
        info = consecutive_info(row, date, days, maps)
        seal, peak = number(row.get("seal_money")), number(row.get("max_seal_money"))
        retention = min(100.0, max(0.0, seal / peak * 100)) if seal is not None and peak and peak > 0 else None
        factors = {"early_seal": _early_seal(row.get("limit_up_time")), "seal_retention": retention,
                   "seal_size": seal_rank.get(seal), "continuity": min(100.0, info["days"] / 7 * 100) if info["days"] else None}
        code = row.get("thscode")
        observed = [value for value in days if maps.get(value) is not None]
        appearances = sum(code in maps[value] for value in observed)
        last_five = days[-5:]
        missing = len(days) - len(observed)
        quality = info["quality"] + [f"缺失因子：{key}" for key, value in factors.items() if value is None]
        if missing:
            quality.append(f"趋势窗口缺失{missing}个交易日，出现次数只计有效日")
        result.append({**row, "consecutive_days": info["days"], "consecutive_source": info["source"],
                       "consecutive_lower_bound": info["lower_bound"], "factors": factors,
                       "score": _weighted(factors, {"early_seal": .25, "seal_retention": .30, "seal_size": .25, "continuity": .20}),
                       "factor_coverage_pct": ratio(sum(value is not None for value in factors.values()), 4),
                       "quality": quality,
                       "trend": {"appearances": appearances, "window_days": len(days), "observed_days": len(observed),
                                 "last_5_appearances": sum(code in maps[value] for value in last_five if maps.get(value) is not None),
                                 "last_5_observed_days": sum(maps.get(value) is not None for value in last_five),
                                 "recent_run": info["days"], "run_lower_bound": info["lower_bound"]}})
    result.sort(key=lambda row: (row["score"] is not None, row["score"] or 0), reverse=True)
    for rank, row in enumerate(result, 1):
        row["rank"] = rank
    return result


def _price_trend(data, date, start, calendar):
    """Use only requested-window daily bars; never fill missing sessions.

    A 5-session return uses t and t-5; volume compares t with the five PRIOR
    sessions. Maximum drawdown uses the last 20 closes, not intraday extremes.
    """
    result = {"status": "unavailable", "adjust": "forward", "as_of": None, "bar_count": 0,
              "close": None, "ma5": None, "ma10": None, "ma20": None, "return_5d_pct": None,
              "volume_ratio_5d": None, "above_ma5": None, "above_ma10": None, "above_ma20": None,
              "max_drawdown_20d_pct": None, "warnings": []}
    if data is None:
        result["warnings"].append("日线读取失败")
        return result
    if not isinstance(data, dict) or not isinstance(data.get("item"), list):
        result["warnings"].append("日线响应格式不完整")
        return result
    if data.get("adjust") not in (None, "forward"):
        result["warnings"].append("上游复权口径与请求前复权不一致")
        return result
    dates, duplicate_dates = {}, set()
    outside = 0
    for bar in data.get("item", []):
        if not isinstance(bar, dict):
            outside += 1
            continue
        day = iso_date(bar.get("date_ms"))
        if day is None or not start <= day <= date:
            outside += 1
            continue
        if day in dates:
            duplicate_dates.add(day)
        dates[day] = bar
    if outside:
        result["warnings"].append(f"忽略{outside}条请求窗口外或日期无效的日线，未来日线不参与计算")
    for day in duplicate_dates:
        del dates[day]
    if duplicate_dates:
        result["warnings"].append("存在重复日期日线，该日按缺失处理")
    result["bar_count"] = len(dates)
    result["as_of"] = max(dates, default=None)
    if date not in dates:
        result["status"] = "date_mismatch"
        result["warnings"].append("没有所选交易日收盘K线，不用较早或较晚收盘价代替")
        return result
    sessions = sorted(day for day in calendar or [] if start <= day <= date)
    if not sessions or sessions[-1] != date:
        sessions = sorted(dates)
        result["warnings"].append("交易日历不完整，周期仅按已返回日线计算，不能验证缺失交易日")
    closes = [number(dates.get(day, {}).get("close_price")) for day in sessions]
    closes = [value if value is not None and value > 0 else None for value in closes]
    result["close"] = closes[-1] if closes else None
    for period in (5, 10, 20):
        window = closes[-period:]
        if len(window) == period and all(value is not None for value in window):
            average = sum(window) / period
            result[f"ma{period}"] = round(average, 6)
            result[f"above_ma{period}"] = result["close"] > average
    if len(closes) >= 6 and closes[-1] is not None and closes[-6] is not None:
        result["return_5d_pct"] = round((closes[-1] / closes[-6] - 1) * 100, 4)
    if len(sessions) >= 6:
        volumes = [number(dates.get(day, {}).get("volume")) for day in sessions[-6:]]
        if all(value is not None and value >= 0 for value in volumes) and sum(volumes[:-1]) > 0:
            result["volume_ratio_5d"] = round(volumes[-1] / (sum(volumes[:-1]) / 5), 4)
    if len(closes) >= 20 and all(value is not None for value in closes[-20:]):
        peak, drawdown = closes[-20], 0.0
        for close in closes[-20:]:
            peak = max(peak, close)
            drawdown = max(drawdown, (peak - close) / peak * 100)
        result["max_drawdown_20d_pct"] = round(drawdown, 4)
    incomplete = any(result[key] is None for key in ("close", "ma5", "ma10", "ma20", "return_5d_pct", "volume_ratio_5d", "max_drawdown_20d_pct"))
    if incomplete:
        result["warnings"].append("交易日线不足或存在缺值，相关周期指标保留为空")
    result["status"] = "partial" if incomplete or result["warnings"] else "ready"
    return result


def build_review(provider, date: str, previous_date: str | None, progress=None) -> dict:
    """Build an auditable report; partial failures remain explicit/null.

    All sector catalogs are included. Attribution uses the strongest 30 sectors
    by snapshot participation proxy, with exact coverage advertised. Historical
    sector prices use a fixed first-30-industry sample, never a future winner set.
    """
    datetime.strptime(date, "%Y-%m-%d")  # Do not silently coerce invalid dates.
    now = datetime.now(SHANGHAI)
    report = {"date": date, "generated_at": now.isoformat(), "source": "HiThink Financial-API",
              "status": "ready", "warnings": [], "raw": {"pools_by_date": {}, "sector_members": {},
                                                            "sector_snapshots": [], "sector_histories": {}, "stock_histories": {}}}
    warnings = report["warnings"]
    raw = report["raw"]

    def notify(message):
        if progress:
            progress(message)

    def fetch(label, operation):
        try:
            return operation()
        except APIError as error:
            warnings.append(f"{label}：{error}")
        except Exception:
            # Do not expose third-party exception text, which might contain keys.
            warnings.append(f"{label}：数据读取或格式处理失败")
        report["status"] = "partial"
        return None

    notify("读取交易日历与近十日完整涨停池")
    calendar = fetch("交易日历", provider.calendar)
    raw["calendar"] = calendar
    if calendar is not None and date not in calendar:
        warnings.append("所选日期不在接口返回的近一年交易日历中，可能非交易日或超出窗口；不自动替换日期")
        report["status"] = "partial"
    days = [day for day in calendar or [] if day <= date][-TREND_DAYS:]
    if date not in days:
        days.append(date)
    # A supplied previous_date is accepted only if it is the calendar predecessor.
    calendar_previous = next((day for day in reversed(calendar or []) if day < date), None)
    if calendar_previous:
        if previous_date and previous_date != calendar_previous:
            warnings.append("传入上一交易日与交易日历不符，已采用日历确定的前一交易日")
        previous_date = calendar_previous
    elif previous_date and previous_date < date:
        if previous_date not in days:
            days.insert(0, previous_date)
    else:
        previous_date = None
    days = sorted(set(days))[-TREND_DAYS:]
    pools = {}
    for index, day in enumerate(days):
        notify(f"读取涨停历史 {index + 1}/{len(days)}：{day}")
        pools[day] = fetch(f"{day}涨停池", lambda day=day: provider.pool("limit-up", day))
    raw["pools_by_date"] = pools
    current = pools.get(date)
    previous = pools.get(previous_date) if previous_date else None
    down = fetch("跌停池", lambda: provider.pool("limit-down", date))
    broken = fetch("炸板池", lambda: provider.pool("limit-break", date))
    raw["limit_down_pool"], raw["limit_break_pool"] = down, broken
    maps = _pool_maps(pools)
    limit_rows = _limit_ranking(current, date, days, maps)
    promotion = _promotion(previous, current, previous_date, days, maps)
    heights = [row["consecutive_days"] for row in limit_rows or [] if row["consecutive_days"] is not None]
    report["limit_up"] = {"rows": limit_rows, "count": len(current) if current is not None else None,
                          "status": "ready" if current is not None else "unavailable",
                          "consecutive_count": sum(height >= 2 for height in heights) if current is not None else None,
                          "max_consecutive": max(heights, default=0) if current is not None else None,
                          "promotion": promotion,
                          "method": "早封25% + 封单留存30% + 封单额分位25% + 严格连板20%；缺值按可用权重重归一化，比较时结合覆盖率"}
    ladder = fetch("连板天梯", provider.ladder)
    raw["ladder"] = ladder
    distribution = Counter(heights)
    ladder_rows = [{"height": height, "count": count,
                    "codes": [row["thscode"] for row in limit_rows or [] if row["consecutive_days"] == height]}
                   for height, count in sorted(distribution.items(), reverse=True)]
    report["ladder"] = {"rows": ladder_rows if current is not None else None,
                        "source": "本日完整涨停池与逐日记录", "raw": ladder,
                        "limitation": "官方天梯每个板位最多4只，仅作旁证；梯队统计及晋级率使用完整涨停池。N天M板不直接算M连板。"}

    notify("读取全市场行情并核对行情日期")
    market = fetch("全市场行情", provider.market)
    raw["market"] = market
    market_received_at = datetime.now(SHANGHAI)
    summary = _market_summary(market, date, calendar=calendar if market_received_at.date() == now.date() else None,
                              now=market_received_at)
    up_count = len(current) if current is not None else None
    break_count = len(broken) if broken is not None else None
    touched = ({row.get("thscode") for row in current} | {row.get("thscode") for row in broken}) if current is not None and broken is not None else None
    summary.update({"limit_up_count": up_count, "limit_down_count": len(down) if down is not None else None,
                    "limit_break_count": break_count, "seal_rate_pct": ratio(up_count, len(touched)) if touched is not None else None,
                    "seal_rate_definition": "涨停池数量 / 涨停池与炸板池代码并集数量（非逐笔封板成功概率）"})
    report["market"] = summary
    if summary["status"] != "ready":
        report["status"] = "partial"
        if summary["data_status"] == "provisional":
            warnings.append("全市场快照时间为今日非交易日；仅按本次有效日历推断归属最近收盘交易日，保留原始时间，行情日期未独立核实，指标为暂定值")
        else:
            warnings.append("全市场快照日期不匹配或行情覆盖不完整；历史日不使用当前行情冒充，缺失盘面指标保留为空")
    if date == now.date().isoformat() and now.hour < 15:
        warnings.append("当前交易日尚未收盘，这份报告属于盘中快照，不能视为最终收盘结论")
        report["status"] = "partial"

    trend_rows = []
    for index, day in enumerate(days):
        items = pools.get(day)
        day_heights = [consecutive_info(row, day, days, maps)["days"] for row in items or []]
        preceding = pools.get(days[index - 1]) if index else None
        promoted = _promotion(preceding, items, days[index - 1] if index else None, days, maps)
        trend_rows.append({"date": day, "limit_up_count": len(items) if items is not None else None,
                           "consecutive_count": sum(value is not None and value >= 2 for value in day_heights) if items is not None else None,
                           "max_consecutive": max((value for value in day_heights if value is not None), default=0) if items is not None else None,
                           "promoted_count": promoted["promoted_count"], "promotion_rate_pct": promoted["rate_pct"],
                           "status": "ready" if items is not None else "unavailable"})
    report["trend"] = {"rows": trend_rows, "date_count": len(days),
                       "status": "partial" if any(value is None for value in pools.values()) else "ready",
                       "leaders": [{"thscode": row["thscode"], "name": row.get("name"), "consecutive_days": row["consecutive_days"],
                                    **row["trend"]} for row in sorted(limit_rows or [], key=lambda row: (row["consecutive_days"] or 0, row["trend"]["appearances"]), reverse=True)],
                       "method": "近10个交易日完整涨停池、严格连板、逐日晋级率；只描述已发生趋势，不输出未来涨停概率"}

    price_start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=59)).date().isoformat()
    price_leaders = []
    for row in limit_rows or []:
        row["price_trend"] = None
    for index, row in enumerate((limit_rows or [])[:PRICE_LEADER_LIMIT]):
        notify(f"读取涨停龙头日线 {index + 1}/{min(len(limit_rows), PRICE_LEADER_LIMIT)}：{row.get('name') or row['thscode']}")
        bars = fetch("涨停股日线", lambda row=row: provider.historical(row["thscode"], price_start, date, adjust="forward"))
        raw["stock_histories"][row["thscode"]] = bars
        price = _price_trend(bars, date, price_start, calendar)
        row["price_trend"] = price
        price_leaders.append({"thscode": row["thscode"], "name": row.get("name"), **price})
    report["trend"]["price_leaders"] = price_leaders
    report["trend"]["price_coverage"] = {"requested": len(price_leaders),
                                           "available": sum(row["close"] is not None for row in price_leaders),
                                           "fully_computed": sum(row["status"] == "ready" for row in price_leaders),
                                           "limit": PRICE_LEADER_LIMIT, "adjust": "forward", "start": price_start, "end": date,
                                           "selection": "目标日涨停强度榜前20名", "window_calendar_days": 60}
    report["trend"]["price_method"] = "前复权日线MA5/10/20；5交易日收益；当日成交量/此前5日均量；最近20个收盘价峰谷最大回撤。无目标日K线或周期缺值不补算。"

    notify("读取行业与概念全目录及行情")
    catalogs = {}
    sectors = []
    for tag in ("industry", "cn_concept"):
        catalog = fetch(f"{tag}板块目录", lambda tag=tag: provider.catalog(tag))
        catalogs[tag] = catalog
        for row in catalog or []:
            sectors.append({"thscode": row["thscode"], "name": row.get("name"), "category": tag,
                            "score": None, "price_change_ratio_pct": None, "turnover": None,
                            "limit_up_count": None, "consecutive_count": None, "member_count": None,
                            "limit_up_ratio_pct": None, "net_flow": None, "net_flow_status": "unavailable",
                            "quality": [], "timestamp": None, "snapshot_date": None, "snapshot_timestamp": None,
                            "effective_trade_date": None, "date_basis": "unavailable", "date_verified": False,
                            "data_status": "unavailable"})
    raw["sector_catalogs"] = catalogs
    latest_trade_day = max((day for day in calendar or [] if day <= now.date().isoformat()), default=None)
    historical = latest_trade_day is not None and date < latest_trade_day
    if not historical:
        for offset in range(0, len(sectors), 100):
            batch = sectors[offset:offset + 100]
            notify(f"读取全板块行情 {min(offset + 100, len(sectors))}/{len(sectors)}")
            snapshot = fetch("板块行情", lambda batch=batch: provider.indices([row["thscode"] for row in batch]))
            raw["sector_snapshots"].append(snapshot)
            if snapshot is None:
                for row in batch:
                    row["quality"].append("板块行情请求失败")
                continue
            snapshot_received_at = datetime.now(SHANGHAI)
            alignment = _snapshot_alignment(snapshot.get("timestamp"), date, now=snapshot_received_at,
                                            calendar=calendar if snapshot_received_at.date() == now.date() else None)
            accepted = alignment.pop("accepted")
            for row in batch:
                row.update(alignment)
            if not accepted:
                warnings.append("部分板块快照日期与所选日期不一致，相关指标不参与排名")
                report["status"] = "partial"
                for row in batch:
                    row["quality"].append("行情日期不匹配")
                continue
            if alignment["data_status"] == "provisional":
                warnings.append("板块快照时间为今日非交易日；仅按本次有效日历推断归属最近收盘交易日，保留原始时间，行情日期未独立核实，板块排名为暂定结果")
                report["status"] = "partial"
                for row in batch:
                    row["quality"].append("休市日最新快照推断归属最近交易日，未经逐板块历史日线核实")
            lookup = {row.get("thscode"): row for row in snapshot.get("item", [])}
            for row in batch:
                quote = lookup.get(row["thscode"], {})
                row.update({"price_change_ratio_pct": number(quote.get("price_change_ratio_pct")),
                            "turnover": number(quote.get("turnover")), "timestamp": snapshot.get("timestamp")})
                if not quote:
                    row["quality"].append("上游未返回本板块行情")
    else:
        # Fixed code-ordered industry sample avoids selecting historical winners
        # using today's performance; all unsampled catalog rows remain explicit.
        sample = sorted((row for row in sectors if row["category"] == "industry"),
                        key=lambda row: row["thscode"])[:HISTORICAL_SECTOR_LIMIT]
        start = days[max(0, len(days) - 2)] if len(days) >= 2 else (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=14)).date().isoformat()
        for index, row in enumerate(sample):
            notify(f"读取历史行业样本 {index + 1}/{len(sample)}：{row.get('name') or row['thscode']}")
            bars = fetch("历史板块行情", lambda row=row: provider.index_historical(row["thscode"], start, date))
            raw["sector_histories"][row["thscode"]] = bars
            valid = sorted((bar for bar in (bars or {}).get("item", []) if iso_date(bar.get("date_ms")) and iso_date(bar["date_ms"]) <= date),
                           key=lambda bar: bar["date_ms"])
            if valid and iso_date(valid[-1]["date_ms"]) == date:
                today_bar = valid[-1]
                close, previous_close = number(today_bar.get("close_price")), number(valid[-2].get("close_price")) if len(valid) > 1 else None
                row.update({"price_change_ratio_pct": round((close / previous_close - 1) * 100, 6) if close is not None and previous_close else None,
                            "turnover": number(today_bar.get("turnover")), "timestamp": today_bar.get("date_ms"),
                            "snapshot_date": date, "snapshot_timestamp": today_bar.get("date_ms"),
                            "effective_trade_date": date, "date_basis": "historical_bar_date", "date_verified": True,
                            "data_status": "ready"})
            row["quality"].append("历史行业固定样本；历史成分归属不可得")
        sampled = {row["thscode"] for row in sample}
        for row in sectors:
            if row["thscode"] not in sampled:
                row["quality"].append("历史行情未采集：固定行业样本最多30个，当前行情不替代历史")
        warnings.append("历史板块行情只取代码排序前30个行业样本；全目录其余板块保留为空，历史成分归属不可得，不宣称全板块历史排名")
        report["status"] = "partial"

    # Rank ALL valid snapshots by a uniform factor set before member attribution.
    # The sample's extra attribution is descriptive and cannot boost its score.
    change_rank = _percentiles([row["price_change_ratio_pct"] for row in sectors])
    turnover_rank = _percentiles([row["turnover"] for row in sectors])
    for row in sectors:
        factors = {"price_strength": change_rank.get(row["price_change_ratio_pct"]),
                   "turnover_participation": turnover_rank.get(row["turnover"])}
        row["score"] = _weighted(factors, {"price_strength": .6, "turnover_participation": .4})
        row["factors"] = factors
        row["factor_coverage_pct"] = ratio(sum(value is not None for value in factors.values()), 2)
    sectors.sort(key=lambda row: (row["score"] is not None, row["score"] or 0), reverse=True)
    attribution = [row for row in sectors if row["score"] is not None][:SECTOR_MEMBER_LIMIT] if not historical else []
    current_codes = {row.get("thscode") for row in current or []}
    consecutive_codes = {row["thscode"] for row in limit_rows or [] if (row["consecutive_days"] or 0) >= 2}
    completed_attribution = 0
    for index, row in enumerate(attribution):
        notify(f"读取强势板块成分 {index + 1}/{len(attribution)}：{row.get('name') or row['thscode']}")
        members = fetch("板块成分", lambda row=row: provider.members(row["thscode"]))
        raw["sector_members"][row["thscode"]] = members
        if members is None:
            row["quality"].append("成分获取失败，归属统计为空")
            continue
        member_codes = {member.get("thscode") for member in members}
        row["member_count"] = len(member_codes)
        if current is not None:
            row["limit_up_count"] = len(member_codes & current_codes)
            row["consecutive_count"] = len(member_codes & consecutive_codes)
            row["limit_up_ratio_pct"] = ratio(row["limit_up_count"], len(member_codes))
        row["membership_basis"] = "接口当前成分；概念归属可重叠，不跨板块加总"
        completed_attribution += 1
    for rank, row in enumerate(sectors, 1):
        row["rank"] = rank if row["score"] is not None else None
        if row["member_count"] is None and not row["quality"]:
            row["quality"].append("仅全板块行情排名；成分归属只采集排名前30个板块")
    valid_sectors = sum(row["score"] is not None for row in sectors)
    inferred_sectors = sum(row["data_status"] == "provisional" for row in sectors)
    report["sectors"] = {"rows": sectors, "count": len(sectors),
                         "status": "ready" if valid_sectors == len(sectors) and not inferred_sectors and all(value is not None for value in catalogs.values()) else "partial",
                         "data_status": "provisional" if inferred_sectors else ("ready" if valid_sectors else "unavailable"),
                         "date_basis": "inferred_latest_closed_session" if inferred_sectors else ("historical_bar_date" if historical else "snapshot_timestamp"),
                         "date_verified": bool(valid_sectors) and valid_sectors == len(sectors) and not inferred_sectors,
                         "effective_trade_date": date if valid_sectors else None,
                         "snapshot_dates": sorted({row["snapshot_date"] for row in sectors if row["snapshot_date"]}),
                         "net_flow_status": "unavailable", "net_flow": None,
                         "coverage": {"catalog_count": len(sectors), "ranked_count": valid_sectors,
                                      "member_attribution_count": completed_attribution,
                                      "member_attribution_limit": SECTOR_MEMBER_LIMIT,
                                      "inferred_date_count": inferred_sectors,
                                      "industry_catalog_available": catalogs.get("industry") is not None,
                                      "concept_catalog_available": catalogs.get("cn_concept") is not None,
                                      "historical_sample": historical},
                         "method": "全目录有效行情：涨幅分位60% + 成交额分位40%，衡量价格强度与成交参与度；前30名另附涨停/连板归属，不改变统一榜分数",
                         "net_flow_note": "真实资金净流入不可用：官方资金流接口标注端内专用、待上线、当前不可调用。成交额与强弱评分均不是主力净流入。"}
    warnings.append(report["sectors"]["net_flow_note"])
    if report["sectors"]["status"] != "ready":
        report["status"] = "partial"
    report["warnings"] = list(dict.fromkeys(warnings))
    report["completed_at"] = datetime.now(SHANGHAI).isoformat()
    notify("复盘计算完成，已保留原始来源与缺失标记")
    return report
