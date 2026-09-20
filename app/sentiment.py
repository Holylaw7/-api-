"""Pure, bounded descriptions of retained limit-up evidence.

No remote requests, clocks, file writes or predictions.  The caller supplies the
report's explicit mode and date; a partial pool never becomes a zero-stock day.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import date as date_type
from statistics import median

BUCKETS = ("1", "2", "3", "4", "5+", "unknown")
WINDOW_DAYS = 10
REASON_LIMIT = 12
CODE_LIMIT = 10


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        date_type.fromisoformat(value)
        return value
    except ValueError:
        return None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def _pct(numerator, denominator):
    return round(numerator / denominator * 100, 2) if denominator else None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _pool(value):
    """De-duplicate for coverage only; a damaged pool cannot prove absence."""
    rows, duplicate, invalid, ambiguous = {}, 0, 0, set()
    if not isinstance(value, list):
        return {"rows": {}, "complete": False, "observed_count": None,
                "duplicate_count": 0, "invalid_count": 0, "ambiguous_count": 0, "available": False}
    for row in value:
        code = row.get("thscode") if isinstance(row, dict) else None
        if not isinstance(code, str) or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
            invalid += 1
        elif code in rows:
            duplicate += 1
            if row != rows[code]:
                ambiguous.add(code)
        else:
            rows[code] = row
    observed = len(rows)
    for code in ambiguous:
        del rows[code]
    return {"rows": rows, "complete": not duplicate and not invalid,
            "observed_count": observed, "duplicate_count": duplicate,
            "invalid_count": invalid, "ambiguous_count": len(ambiguous), "available": True}


def _explicit_height(row):
    # continue_day_cnt and precomputed floats cannot prove consecutive sessions.
    text = row.get("continue_day_text")
    text = text.strip() if isinstance(text, str) else ""
    if text == "首板":
        return 1, False
    match = re.fullmatch(r"([1-9]\d{0,4})连板", text)
    if match:
        return int(match[1]), False
    match = re.fullmatch(r"([1-9]\d{0,4})天([1-9]\d{0,4})板", text)
    if match:
        same = match[1] == match[2]
        return int(match[1]) if same else None, not same
    return None, False


def _height(code, row, index, days, pools, calendar_verified):
    explicit, nonconsecutive_label = _explicit_height(row)
    if not calendar_verified:
        return explicit, False, nonconsecutive_label, False
    run, bounded = 0, False
    for day in reversed(days[:index + 1]):
        pool = pools[day]
        if not pool["complete"]:
            break
        if code not in pool["rows"]:
            bounded = True
            break
        run += 1
    conflict = explicit is not None and ((bounded and explicit != run) or run > explicit)
    if explicit is not None and not conflict:
        return explicit, False, nonconsecutive_label, False
    return run or None, bool(run and not bounded), nonconsecutive_label, conflict


def _matrix(report, target):
    raw = _dict(report.get("raw"))
    source = _dict(raw.get("pools_by_date"))
    calendar = raw.get("calendar")
    calendar_valid = isinstance(calendar, list) and bool(calendar) and all(_date(day) for day in calendar)
    calendar_verified = bool(calendar_valid and target in calendar)
    warnings = []
    if calendar_verified:
        days = sorted({day for day in calendar if day <= target})[-WINDOW_DAYS:]
    else:
        days = sorted({day for day in source if _date(day) and day <= target})[-WINDOW_DAYS:]
        if target not in days:
            days = sorted([*days, target])[-WINDOW_DAYS:]
        warnings.append("缺少可核验的目标交易日日历，日期仅为报告中的池日期；不跨日期推断连续交易日")
    excluded = sum(not _date(day) or day > target or (calendar_verified and day not in calendar) for day in source)
    if excluded:
        warnings.append(f"排除{excluded}个无效、未来或不在已核验交易日历中的池日期")
    pools = {day: _pool(source.get(day)) for day in days}
    rows = []
    for index, day in enumerate(days):
        pool = pools[day]
        coverage = {key: pool[key] for key in ("observed_count", "duplicate_count", "invalid_count")}
        coverage["complete"] = pool["complete"]
        item = {"date": day, "status": "unavailable", "limit_up_count": None,
                "buckets": dict.fromkeys(BUCKETS), "lower_bound_count": None,
                "lower_bound_by_bucket": dict.fromkeys(BUCKETS), "max_consecutive": None,
                "coverage": coverage, "warnings": []}
        if not pool["complete"]:
            item["warnings"].append("完整涨停池缺失或含重复/无效代码，数量与梯队不补零")
            rows.append(item)
            continue
        buckets, lower_buckets = dict.fromkeys(BUCKETS, 0), dict.fromkeys(BUCKETS, 0)
        heights, lower_count, nonconsecutive, conflicts = [], 0, 0, 0
        for code, row in sorted(pool["rows"].items()):
            height, lower, non_label, conflict = _height(code, row, index, days, pools, calendar_verified)
            key = "unknown" if height is None else (str(height) if height < 5 else "5+")
            buckets[key] += 1
            lower_buckets[key] += int(lower)
            lower_count += int(lower)
            nonconsecutive += int(non_label)
            conflicts += int(conflict)
            if height is not None:
                heights.append(height)
        if lower_count:
            item["warnings"].append(f"{lower_count}只达到窗口边界或缺失日，梯队高度为可证实下限")
        if nonconsecutive:
            item["warnings"].append(f"{nonconsecutive}只含N天M板非连续标签，未把M直接当连板数")
        if conflicts:
            item["warnings"].append(f"{conflicts}只明确连板标签与连续完整池记录冲突，采用已观察记录")
        if buckets["unknown"]:
            item["warnings"].append(f"{buckets['unknown']}只严格连板高度未知，单列未知梯队")
        item.update(status="partial" if item["warnings"] or not calendar_verified else "ready",
                    limit_up_count=len(pool["rows"]), buckets=buckets, lower_bound_count=lower_count,
                    lower_bound_by_bucket=lower_buckets,
                    max_consecutive=max(heights) if heights else (0 if not pool["rows"] else None))
        rows.append(item)
    complete = sum(row["coverage"]["complete"] for row in rows)
    status = "ready" if rows and all(row["status"] == "ready" for row in rows) else ("partial" if complete else "unavailable")
    return {"status": status, "rows": rows,
            "coverage": {"expected_days": len(days), "complete_days": complete,
                         "coverage_pct": _pct(complete, len(days)), "calendar_verified": calendar_verified,
                         "requested_window_days": WINDOW_DAYS},
            "definition": "最近至多10个已核验交易日的完整涨停池。1/2/3/4/5+及未知各档互斥，合计等于当日涨停数；下限另列，1的下限不能称已确认首板。明确连板标签与逐日完整池交叉核验，不使用有限天梯样本或原始continue_day_cnt作全市场计数。",
            "warnings": warnings}


def _current(report):
    limit = _dict(report.get("limit_up"))
    current = _pool(limit.get("rows"))
    count = limit.get("count")
    declared_valid = isinstance(count, int) and not isinstance(count, bool) and count >= 0
    current["complete"] = bool(current["complete"] and limit.get("status") == "ready"
                               and declared_valid and count == current["observed_count"])
    if limit.get("date") not in (None, report.get("date")):
        return {**_pool(None), "date_mismatch": True}
    raw_pools = _dict(_dict(report.get("raw")).get("pools_by_date"))
    if report.get("date") in raw_pools:
        source = _pool(raw_pools[report["date"]])
        if not source["complete"] or source["rows"].keys() != current["rows"].keys():
            current["complete"] = False
    return {**current, "date_mismatch": False}


def _retention(current):
    values, missing, invalid, above = [], 0, 0, 0
    for row in current["rows"].values():
        raw_seal, raw_peak = row.get("seal_money"), row.get("max_seal_money")
        if raw_seal is None or raw_peak is None:
            missing += 1
            continue
        seal, peak = _number(raw_seal), _number(raw_peak)
        if seal is None or peak is None or seal < 0 or peak <= 0:
            invalid += 1
            continue
        if seal > peak:
            above += 1
        else:
            values.append(seal / peak * 100)
    total = current["observed_count"] if current["complete"] else None
    below = sum(value < 50 for value in values)
    warnings = []
    if not current["complete"]:
        warnings.append("当期完整涨停池未通过核验，统计仅限有效代码的可见样本；全池总数及覆盖率未知")
    if current["ambiguous_count"]:
        warnings.append(f"{current['ambiguous_count']}个重复代码的记录互相冲突，整只股票从样本中剔除")
    if missing:
        warnings.append(f"{missing}只缺少当前或峰值封单额")
    if invalid:
        warnings.append(f"{invalid}只封单额为负值、非有限数值或峰值不大于零，已剔除")
    if above:
        warnings.append(f"{above}只当前封单额大于峰值、留存超过100%，按异常剔除而非截为100%")
    status = "ready" if current["complete"] and not warnings else ("partial" if values or total == 0 else "unavailable")
    return {"status": status, "total_count": total, "observed_count": current["observed_count"],
            "valid_count": len(values) if current["available"] else None,
            "missing_count": missing if current["available"] else None,
            "invalid_count": invalid if current["available"] else None,
            "above_100_count": above if current["available"] else None,
            "excluded_conflict_count": current["ambiguous_count"] if current["available"] else None,
            "median_pct": round(median(values), 4) if values else None,
            "below_50_count": below if current["available"] else None,
            "below_50_pct": _pct(below, len(values)), "coverage_pct": _pct(len(values), total),
            "definition": "封单留存=seal_money/max_seal_money×100%。有效样本要求两者为有限非负数、峰值>0且留存≤100%；低于50%占比以有效样本为分母。描述池快照封单结构，不是成交概率、净流入或当前竞价金额留存。",
            "warnings": warnings}


def _reasons(current):
    groups, missing, invalid = defaultdict(list), 0, 0
    for code, row in sorted(current["rows"].items()):
        reason = row.get("limit_up_reason")
        if not isinstance(reason, str) or not reason.strip():
            missing += 1
        elif len(reason) > 4000:
            invalid += 1
        else:
            # Keep the official full text; do not split a multi-theme phrase.
            groups[reason].append(code)
    known = sum(len(codes) for codes in groups.values())
    total = current["observed_count"] if current["complete"] else None
    rows = [{"reason": reason, "count": len(codes), "share_pct": _pct(len(codes), known),
             "codes": codes[:CODE_LIMIT], "displayed_code_count": min(len(codes), CODE_LIMIT),
             "other_code_count": max(0, len(codes) - CODE_LIMIT)}
            for reason, codes in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))[:REASON_LIMIT]]
    warnings = []
    if not current["complete"]:
        warnings.append("当期完整涨停池未通过核验，原因分布仅为可见样本")
    if current["ambiguous_count"]:
        warnings.append(f"{current['ambiguous_count']}个重复代码的记录互相冲突，整只股票从原因分组剔除")
    if current["available"] and not known and current["observed_count"]:
        warnings.append("没有可用limit_up_reason官方文本，不从名称、行业或其他标签猜测涨停原因")
    if missing:
        warnings.append(f"{missing}只缺少有效官方涨停原因")
    if invalid:
        warnings.append(f"{invalid}条涨停原因超过文本保护上限，未参与分组")
    if len(groups) > REASON_LIMIT:
        warnings.append(f"展示前{REASON_LIMIT}组，另有{len(groups) - REASON_LIMIT}组未展开")
    status = "ready" if current["complete"] and not missing and not invalid else ("partial" if known else "unavailable")
    return {"status": status, "source_field": "limit_up_reason", "total_count": total,
            "observed_count": current["observed_count"], "known_count": known if current["available"] else None,
            "missing_count": missing if current["available"] else None,
            "invalid_count": invalid if current["available"] else None,
            "excluded_conflict_count": current["ambiguous_count"] if current["available"] else None,
            "group_count": len(groups) if current["available"] else None, "displayed_count": len(rows),
            "coverage_pct": _pct(known, total), "rows": rows,
            "definition": "按官方limit_up_reason完整原文精确分组，同一股票仅入一组；占比以有有效原因文本的股票数为分母。按数量降序、原文升序展示前12组，每组至多10个代码。官方文本是来源描述，不是行业/概念归属、资金流或经独立验证的因果证明。",
            "warnings": warnings}


def build_sentiment(report):
    """Describe the report's evidence without mutating it or adding new data."""
    report = _dict(report)
    target, mode = _date(report.get("date")), report.get("mode")
    warnings = []
    valid = target is not None and mode in ("live", "demo")
    if target is None:
        warnings.append("报告日期缺失或无效，不能建立情绪结构")
    if mode not in ("live", "demo"):
        warnings.append("报告模式缺失或未知，未将数据默认为实盘")
    if mode == "demo":
        warnings.append("DEMO：演示数据，仅用于功能验证，不能作为实盘市场情绪")
    current = _current(report) if valid else _pool(None)
    if current.get("date_mismatch"):
        warnings.append("涨停池日期与报告日期不符，不计算当期封单与原因分布")
    matrix = _matrix(report, target) if valid else {
        "status": "unavailable", "rows": [],
        "coverage": {"expected_days": None, "complete_days": None, "coverage_pct": None,
                     "calendar_verified": False, "requested_window_days": WINDOW_DAYS},
        "definition": "需有明确日期与模式的完整涨停池证据", "warnings": []}
    retention, reasons = _retention(current), _reasons(current)
    components = (matrix, retention, reasons)
    status = ("ready" if all(item["status"] == "ready" for item in components)
              else "unavailable" if all(item["status"] == "unavailable" for item in components) else "partial")
    for item in components:
        warnings.extend(item["warnings"])
    return {"version": "1.0", "date": target, "mode": mode if mode in ("live", "demo") else "unknown",
            "status": status, "definition": "基于已留存报告的涨停情绪结构；没有额外行情请求，不生成情绪买卖分、未来预测或仓位指令。",
            "matrix": matrix, "retention": retention, "reasons": reasons,
            "warnings": list(dict.fromkeys(warnings))}
