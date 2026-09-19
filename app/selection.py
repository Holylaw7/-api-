"""Bounded, explainable selection of stronger-trend auction candidates.

No files are written and no orders are submitted. The caller caches this result
by its completed trading date and combines it with watchlists/yesterday's pool.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from .provider import APIError, SHANGHAI
from .review import _price_trend, _snapshot_alignment, number

CANDIDATE_LIMIT = 60
SELECTION_LIMIT = 30
ACTIVE_MARKET_LIMIT = 40
SCORE_WEIGHTS = {"momentum": .30, "ma_alignment": .30, "drawdown_resilience": .30,
                 "volume_confirmation": .10}
THRESHOLDS = {"close_above_ma5_above_ma10_above_ma20": True,
              "return_5d_min_exclusive_pct": 0, "return_5d_max_pct": 30,
              "max_drawdown_20d_pct": 15}


def _valid_code(value):
    return bool(isinstance(value, str) and re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value))


def _excluded(row, name):
    if not _valid_code(row.get("thscode")) or not isinstance(name, str) or not name.strip():
        return True
    upper = name.upper().replace(" ", "")
    return bool(row.get("is_st") or row.get("is_delisted") or "ST" in upper or "退" in name)


def _trend_score(trend):
    """Fixed reference scales, so a smaller candidate set does not inflate ranks."""
    return5 = number(trend.get("return_5d_pct"))
    ma5, ma20 = number(trend.get("ma5")), number(trend.get("ma20"))
    drawdown, volume = number(trend.get("max_drawdown_20d_pct")), number(trend.get("volume_ratio_5d"))
    clamp = lambda value: max(0.0, min(100.0, value))
    factors = {"momentum": clamp(return5 / 15 * 100) if return5 is not None else None,
               "ma_alignment": clamp((ma5 / ma20 - 1) / .10 * 100) if ma5 is not None and ma20 else None,
               "drawdown_resilience": clamp((1 - drawdown / 15) * 100) if drawdown is not None else None,
               "volume_confirmation": clamp(volume / 2 * 100) if volume is not None else None}
    denominator = sum(SCORE_WEIGHTS[key] for key, value in factors.items() if value is not None)
    score = sum(value * SCORE_WEIGHTS[key] for key, value in factors.items() if value is not None) / denominator if denominator else None
    return round(score, 2) if score is not None else None, {key: round(value, 2) if value is not None else None for key, value in factors.items()}


def _passes(trend):
    values = [number(trend.get(key)) for key in ("close", "ma5", "ma10", "ma20", "return_5d_pct", "max_drawdown_20d_pct")]
    if any(value is None for value in values):
        return False
    close, ma5, ma10, ma20, return5, drawdown = values
    return close > ma5 > ma10 > ma20 > 0 and 0 < return5 <= 30 and 0 <= drawdown <= 15


def build_trend_pool(provider, date, calendar, report=None, progress=None, should_stop=None):
    """Screen up to 60 observed candidates, returning at most 30 qualifying rows.

    Prefer up to 40 liquid names from an aligned report, reserving space for the
    last five complete limit-up pools. With no usable report use those pools.
    The time guard and cancellation guard run before every provider operation.
    """
    now = datetime.now(SHANGHAI)
    result = {"date": date, "status": "ready", "rows": [], "candidate_count": 0,
              "candidate_available_count": 0, "evaluated_count": 0, "valid_history_count": 0,
              "selected_count": 0, "matched_count": 0, "warnings": [], "generated_at": now.isoformat(),
              "source": "HiThink真实日线/有限候选", "candidate_sources": [], "thresholds": dict(THRESHOLDS),
              "coverage": {},
              "method": "有限候选筛选，非全市场扫描：报告活跃股优先40只，再补最近5日完整涨停池；候选最多60、入选最多30。前复权日线截至指定已收盘日，不使用未来K线。"}
    warnings = result["warnings"]
    counts = {"excluded_invalid_or_st": 0, "unknown_names": 0, "history_failures": 0,
              "insufficient_history": 0, "histories_with_warnings": 0,
              "pool_days_available": 0, "pool_days_requested": 0}
    interrupted = False

    def warn(message):
        warnings.append(message)
        result["status"] = "partial"

    def guarded():
        nonlocal interrupted
        if interrupted:
            return True
        try:
            requested_stop = bool(should_stop and should_stop())
        except Exception:
            requested_stop = True
        current = datetime.now(SHANGHAI)
        minute = current.hour * 60 + current.minute
        if requested_stop or 550 <= minute <= 566:
            interrupted = True
            warn("已停止启动新的取数请求：收到取消信号或进入09:10—09:26竞价保护时段；仅保留此前完成结果")
        return interrupted

    def notify(message):
        if progress:
            progress(message)

    def fetch(label, operation):
        if guarded():
            return None
        try:
            return operation()
        except APIError as error:
            warn(f"{label}：{error}")
        except Exception:
            warn(f"{label}：数据读取失败")
        return None

    def finish():
        result["rows"].sort(key=lambda row: (row["score"], row["thscode"]), reverse=True)
        result["matched_count"] = len(result["rows"])
        result["rows"] = result["rows"][:SELECTION_LIMIT]
        result["selected_count"] = len(result["rows"])
        for rank, row in enumerate(result["rows"], 1):
            row["rank"] = rank
        result["warnings"] = list(dict.fromkeys(warnings))
        result["coverage"] = {**counts, "candidate_limit": CANDIDATE_LIMIT, "selection_limit": SELECTION_LIMIT,
                              "candidate_available_count": result["candidate_available_count"],
                              "candidate_count": result["candidate_count"], "evaluated_count": result["evaluated_count"],
                              "valid_history_count": result["valid_history_count"],
                              "not_evaluated_count": result["candidate_count"] - result["evaluated_count"],
                              "limited_universe": True, "cancelled_or_protected": interrupted,
                              "adjust": "forward", "end": date}
        result["completed_at"] = datetime.now(SHANGHAI).isoformat()
        return result

    try:
        target = datetime.strptime(date, "%Y-%m-%d").date()
        days = sorted(set(calendar))
        if any(datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day for day in days):
            raise ValueError("invalid calendar")
    except (ValueError, TypeError):
        warn("截止日或交易日历无效，未启动趋势筛选")
        return finish()
    if (date not in days or target > now.date()
            or (target == now.date() and now.hour < 15)):
        warn("趋势池截止日必须是日历中已收盘的交易日，不使用未来或尚未收盘的数据")
        return finish()
    if guarded():
        return finish()
    recent = [day for day in days if day <= date][-5:]
    counts["pool_days_requested"] = len(recent)
    if len(recent) < 5:
        warn("交易日历不足5个历史交易日，候选来源覆盖不完整")
    same_report = (isinstance(report, dict) and report.get("date") == date
                   and report.get("source") != "demo" and report.get("mode") != "demo" and not report.get("demo"))
    raw = report.get("raw", {}) if same_report and isinstance(report.get("raw"), dict) else {}
    cached_pools = raw.get("pools_by_date", {})
    if not isinstance(cached_pools, dict):
        cached_pools = {}
    pool_candidates, names = [], {}
    for day in reversed(recent):
        if guarded():
            break
        cached = cached_pools.get(day)
        rows = cached if isinstance(cached, list) else fetch(f"{day}涨停候选池", lambda day=day: provider.pool("limit-up", day))
        if rows is None:
            continue
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            warn(f"{day}涨停候选池格式不完整")
            continue
        counts["pool_days_available"] += 1
        for row in sorted(rows, key=lambda row: number(row.get("seal_money")) or 0, reverse=True):
            if not _valid_code(row.get("thscode")):
                counts["excluded_invalid_or_st"] += 1
                continue
            name = row.get("name")
            if isinstance(name, str) and name.strip():
                names.setdefault(row.get("thscode"), {"name": name, "is_st": row.get("is_st", False)})
            pool_candidates.append({**row, "source": f"{day}完整涨停池"})
    if counts["pool_days_available"]:
        result["candidate_sources"].append("最近5个交易日完整涨停池")
    if interrupted:
        return finish()

    market_rows = []
    market = raw.get("market")
    if isinstance(market, dict) and isinstance(market.get("item"), list):
        source_now = now
        # Revalidate the explicit inference at the report's completion time, so
        # a saved same-date report remains usable on a subsequent nontrading day.
        market_meta = report.get("market", {})
        allowed_inferred = (market_meta.get("date_basis") == "inferred_latest_closed_session"
                            and market_meta.get("data_status") == "provisional"
                            and market_meta.get("date_verified") is False
                            and market_meta.get("effective_trade_date") == date)
        if allowed_inferred:
            try:
                source_now = datetime.fromisoformat(report["completed_at"])
                if source_now.tzinfo is None or source_now > now:
                    allowed_inferred = False
            except (KeyError, TypeError, ValueError):
                allowed_inferred = False
        alignment = _snapshot_alignment(market.get("timestamp"), date,
                                        calendar=days if allowed_inferred else None, now=source_now,
                                        page_timestamps=market.get("page_timestamps", [market.get("timestamp")]))
        if alignment["accepted"] and (alignment["date_basis"] == "snapshot_timestamp" or allowed_inferred):
            market_rows = [row for row in market["item"] if isinstance(row, dict)
                           and number(row.get("turnover")) is not None and row["turnover"] > 0]
            if alignment["data_status"] == "provisional":
                warn("活跃股候选来自休市日快照对最近收盘日的有限日期推断，原始快照日期未独立核实；入选趋势仍用明确目标日日线验证")
            result["candidate_sources"].append("同截止日报告的成交额活跃股" + ("（日期暂定推断）" if alignment["data_status"] == "provisional" else ""))
        else:
            warn("报告市场快照不满足目标日日期校验，已跳过活跃股来源，使用近期涨停池")
    if market_rows:
        missing_names = any(not row.get("name") and _valid_code(row.get("thscode")) and row.get("thscode") not in names for row in market_rows)
        if missing_names:
            metadata = fetch("候选名称代码表", provider.tickers) if hasattr(provider, "tickers") else None
            if isinstance(metadata, list):
                for item in metadata:
                    if isinstance(item, dict) and _valid_code(item.get("thscode")):
                        names[item["thscode"]] = item
            elif not interrupted:
                warn("部分活跃股缺少名称，无法确认ST或退市状态，未纳入这些代码")
        if interrupted:
            return finish()

    def normalize(rows):
        result_rows, seen = [], set()
        for row in rows:
            code = row.get("thscode")
            if not _valid_code(code):
                counts["excluded_invalid_or_st"] += 1
                continue
            if code in seen:
                continue
            seen.add(code)
            metadata = names.get(code, {})
            name = row.get("name") or metadata.get("name")
            combined = {**metadata, **row, "name": name}
            if not name:
                counts["unknown_names"] += 1
            if _excluded(combined, name):
                counts["excluded_invalid_or_st"] += 1
                continue
            # The current catalog can also explicitly mark an ended listing.
            if metadata.get("end_date") and str(metadata["end_date"]) <= now.date().isoformat():
                counts["excluded_invalid_or_st"] += 1
                continue
            result_rows.append(combined)
        return result_rows

    active = normalize([{**row, "source": "报告成交额活跃股"} for row in sorted(market_rows, key=lambda row: row["turnover"], reverse=True)])
    recent_pool = normalize(pool_candidates)
    ordered = active[:ACTIVE_MARKET_LIMIT] + recent_pool + active[ACTIVE_MARKET_LIMIT:]
    candidates, seen = [], set()
    for row in ordered:
        if row["thscode"] not in seen:
            candidates.append(row)
            seen.add(row["thscode"])
    result["candidate_available_count"] = len(candidates)
    candidates = candidates[:CANDIDATE_LIMIT]
    result["candidate_count"] = len(candidates)
    if not candidates:
        if counts["pool_days_available"] < counts["pool_days_requested"]:
            warn("没有可验证候选，来源存在缺失；不把此结果解释为市场没有强势股")
        return finish()
    start = (target - timedelta(days=59)).isoformat()
    for index, row in enumerate(candidates):
        if guarded():
            break
        notify(f"筛选趋势候选 {index + 1}/{len(candidates)}：{row['name']}")
        # Check once more after callbacks, which may allow cancellation to arrive.
        if guarded():
            break
        bars = fetch("候选前复权日线", lambda row=row: provider.historical(row["thscode"], start, date, adjust="forward"))
        if interrupted:
            break
        result["evaluated_count"] += 1
        if bars is None:
            counts["history_failures"] += 1
            continue
        trend = _price_trend(bars, date, start, days)
        if trend.get("warnings"):
            counts["histories_with_warnings"] += 1
        if any(number(trend.get(key)) is None for key in ("close", "ma5", "ma10", "ma20", "return_5d_pct", "max_drawdown_20d_pct")):
            counts["insufficient_history"] += 1
            continue
        result["valid_history_count"] += 1
        if not _passes(trend):
            continue
        score, factors = _trend_score(trend)
        trend = {**trend, "score_factors": factors, "score_weights": dict(SCORE_WEIGHTS),
                 "score_factor_coverage_pct": round(sum(SCORE_WEIGHTS[key] for key, value in factors.items() if value is not None)
                                                     / sum(SCORE_WEIGHTS.values()) * 100, 2)}
        reason = (f"收盘价 > MA5 > MA10 > MA20；5交易日涨幅{trend['return_5d_pct']:.2f}%；"
                  f"20日收盘最大回撤{trend['max_drawdown_20d_pct']:.2f}%")
        result["rows"].append({"thscode": row["thscode"], "name": row["name"], "score": score, "trend_score": score,
                               "date": date, "trend": trend, "reason": reason, "source": row["source"]})
    if counts["insufficient_history"]:
        warn(f"{counts['insufficient_history']}只候选日线缺失或不足完整周期，未入选；不补零、不拿未来或更早K线代替")
    if counts["histories_with_warnings"]:
        warn(f"{counts['histories_with_warnings']}只候选日线存在缺值、日期异常或覆盖提示，已保留明细质量标记")
    return finish()
