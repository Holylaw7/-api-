"""Bounded single-stock analysis using one forward-adjusted history request.

No auction requests, universes, price snapshots, or LLM calls are issued here.
Live auction observations are attached by the service from its existing engine.
The formulas below are transparent research heuristics, not backtested returns.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, time

from .provider import APIError, SHANGHAI, iso_date
from .review import _price_trend, number

TREND_WEIGHTS = {"momentum_5d": .35, "ma_position": .30, "drawdown_control": .20, "volume_confirmation": .15}
TREND_FORMULAS = {
    "momentum_5d": "clip(50 + 2.5 × 前复权5交易日涨幅百分点, 0, 100)",
    "ma_position": "100 × (收盘价>MA5、>MA10、>MA20的成立数量) / 3；三条均线均须可用",
    "drawdown_control": "clip(100 - 5 × 最近20交易日收盘最大回撤百分点, 0, 100)",
    "volume_confirmation": "clip(50 + 25 × sign(当日涨幅) × clip(当日量/前5交易日均量 - 1, 0, 2), 0, 100)",
}
TREND_LABELS = {"momentum_5d": "五日价格动量", "ma_position": "均线位置", "drawdown_control": "回撤控制", "volume_confirmation": "量价同向确认"}


def validate_code(code) -> str:
    if not isinstance(code, str):
        raise ValueError("请输入六位股票代码或完整代码，例如 600519 或 600519.SH")
    cleaned = code.strip().upper()
    if not re.fullmatch(r"[0-9]{6}(?:\.(?:SH|SZ|BJ))?", cleaned):
        raise ValueError("仅支持六位股票代码或六位加 .SH/.SZ/.BJ；不接受名称或模糊匹配")
    return cleaned


def now_sh():
    return datetime.now(SHANGHAI)


def _clip(value, lo=0, hi=100):
    return max(lo, min(hi, value))


def _validated_metadata(metadata):
    if not isinstance(metadata, dict):
        raise ValueError("请先通过官方代码表确认证券")
    code = validate_code(metadata.get("thscode"))
    if "." not in code or metadata.get("asset_type") != "a-share" or metadata.get("ticker") != code[:6]:
        raise ValueError("证券元信息不是已确认的完整 A 股代码")
    if not isinstance(metadata.get("name"), str) or not metadata["name"].strip():
        raise ValueError("证券元信息缺少官方名称")
    return code


def _calendar_window(date, calendar, now):
    if not isinstance(date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date):
        raise ValueError("分析日期须为 YYYY-MM-DD")
    try:
        requested = datetime.strptime(date, "%Y-%m-%d").date()
        if not isinstance(calendar, (list, tuple)):
            raise ValueError()
        days = sorted({datetime.strptime(value, "%Y-%m-%d").date().isoformat() for value in calendar})
    except (TypeError, ValueError):
        raise ValueError("分析日期或交易日历无效") from None
    if date not in days:
        raise ValueError("所选日期不在官方交易日历中")
    if requested > now.date() or (requested == now.date() and now.time() < time(15)):
        raise ValueError("趋势分析仅接受已收盘交易日，不能用盘中日线代替收盘")
    sessions = [day for day in days if day <= date][-40:]
    return sessions


def _clean_history(data, start, date, sessions, warnings):
    if not isinstance(data, dict) or not isinstance(data.get("item"), list):
        return None, []
    if data.get("adjust") not in (None, "forward"):
        # Keep the contradictory metadata for _price_trend's diagnostic, but do
        # not draw unadjusted prices under a forward-adjusted chart label.
        return {"item": [], "timestamp": data.get("timestamp"), "adjust": data.get("adjust")}, []
    mapping, duplicates = {}, set()
    bad_dates = 0
    for original in data["item"]:
        if not isinstance(original, dict):
            bad_dates += 1
            continue
        day = iso_date(original.get("date_ms"))
        if day is None or not start <= day <= date or day not in sessions:
            bad_dates += 1
            continue
        if day in mapping:
            duplicates.add(day)
        row = {"date": day, "date_ms": original["date_ms"]}
        for key in ("open_price", "high_price", "low_price", "close_price", "volume", "turnover"):
            value = number(original.get(key))
            if value is not None and (value < 0 or (key.endswith("price") and value == 0)):
                value = None
            row[key] = value
        mapping[day] = row
    for day in duplicates:
        del mapping[day]
    if bad_dates:
        warnings.append(f"已排除{bad_dates}条日期无效、非交易日或窗口外日线；未来数据不参与评分")
    if duplicates:
        warnings.append("日线日期重复，重复日按缺失处理，不选取任意一条替代")
    history = [mapping[day] for day in sorted(mapping)]
    return {"item": history, "timestamp": data.get("timestamp"), "adjust": data.get("adjust")}, history


def _trend_score(trend, history, sessions):
    values = {"momentum_5d": trend.get("return_5d_pct"),
              "ma_position": None,
              "drawdown_control": trend.get("max_drawdown_20d_pct"),
              "volume_confirmation": trend.get("volume_ratio_5d")}
    scores = {key: None for key in TREND_WEIGHTS}
    if values["momentum_5d"] is not None:
        scores["momentum_5d"] = _clip(50 + 2.5 * values["momentum_5d"])
    positions = [trend.get(f"above_ma{period}") for period in (5, 10, 20)]
    if all(isinstance(value, bool) for value in positions):
        values["ma_position"] = sum(positions)
        scores["ma_position"] = 100 * sum(positions) / 3
    if values["drawdown_control"] is not None:
        scores["drawdown_control"] = _clip(100 - 5 * values["drawdown_control"])
    by_date = {row["date"]: row for row in history}
    day_return = None
    if len(sessions) >= 2:
        before = by_date.get(sessions[-2], {}).get("close_price")
        current = by_date.get(sessions[-1], {}).get("close_price")
        if before is not None and before > 0 and current is not None:
            day_return = (current / before - 1) * 100
    if day_return is not None and values["volume_confirmation"] is not None:
        sign = (day_return > 0) - (day_return < 0)
        scores["volume_confirmation"] = _clip(50 + 25 * sign * _clip(values["volume_confirmation"] - 1, 0, 2))
    coverage = sum(TREND_WEIGHTS[key] for key, score in scores.items() if score is not None)
    raw_score = sum(TREND_WEIGHTS[key] * score for key, score in scores.items() if score is not None) / coverage if coverage else None
    eligible = coverage >= .65 - 1e-12 and trend.get("close") is not None and trend.get("status") in ("ready", "partial")
    factors = {key: {"label": TREND_LABELS[key], "value": values[key],
                     "score": round(scores[key], 4) if scores[key] is not None else None,
                     "weight": TREND_WEIGHTS[key], "available": scores[key] is not None,
                     "contribution": round(scores[key] * TREND_WEIGHTS[key] / coverage, 4) if scores[key] is not None and coverage else None,
                     "formula": TREND_FORMULAS[key]} for key in TREND_WEIGHTS}
    factors["volume_confirmation"]["day_return_pct"] = round(day_return, 4) if day_return is not None else None
    return round(raw_score, 4) if eligible else None, factors, round(coverage, 4)


def analyze_stock(provider, metadata, date, calendar, context=None) -> dict:
    """Analyze one resolved stock as of one closed session, with one remote call.

    ``context`` is optional existing pool context and does not trigger any reads.
    The ordinary snapshot helper is intentionally not called: it has no date
    parameter, and must never leak a later quote into a past-date trend score.
    """
    code = _validated_metadata(metadata)
    now = now_sh()
    sessions = _calendar_window(date, calendar, now)
    start = sessions[0]
    warnings = []
    if len(sessions) < 20:
        warnings.append("可用交易日历窗口不足20日，长期均线与回撤因子可能缺失")
    data = None
    try:
        data = provider.historical(code, start, date, adjust="forward")
    except Exception as exc:
        warnings.append("日线读取失败：" + (str(exc) if isinstance(exc, APIError) else type(exc).__name__))
    clean, history = _clean_history(data, start, date, sessions, warnings)
    trend = _price_trend(clean, date, start, sessions)
    warnings.extend(trend.get("warnings", []))
    score, factors, coverage = _trend_score(trend, history, sessions)
    if coverage < 1:
        warnings.append("趋势因子存在缺失，分数按可用权重归一化；覆盖率低于65%不提供综合分")
    warnings.append("趋势分为未回测的研究公式，不代表上涨概率或买卖建议")
    safe_context = None
    if isinstance(context, dict):
        supplied = context.get(code, context)
        if isinstance(supplied, dict) and supplied.get("thscode", code) == code:
            source_date = supplied.get("context_date") or supplied.get("date")
            try:
                source_date = datetime.strptime(source_date, "%Y-%m-%d").date().isoformat()
            except (ValueError, TypeError):
                source_date = None
            if source_date is not None and source_date <= date:
                safe_context = {key: copy.deepcopy(supplied.get(key)) for key in
                                ("thscode", "name", "context_date", "date", "continue_day_cnt", "continue_day_text", "limit_up_reason")}
                safe_context["thscode"] = code
            elif supplied:
                warnings.append("已有涨停上下文未标注可验证日期或晚于分析日，未纳入个股结果")
    status = "unavailable" if trend["status"] in ("unavailable", "date_mismatch") else "partial" if trend["status"] != "ready" or len(warnings) > 1 else "ready"
    return {"thscode": code, "ticker": metadata["ticker"], "name": metadata["name"],
            "metadata": copy.deepcopy(metadata), "date": date, "generated_at": now.isoformat(),
            "status": status, "trend": trend, "trend_score": score, "trend_factors": factors,
            "trend_coverage": coverage, "warnings": list(dict.fromkeys(warnings)), "history": history,
            "quote": None, "context": safe_context,
            "score_method": {"version": "stock-trend-v1", "weights": dict(TREND_WEIGHTS),
                             "minimum_coverage": .65, "range": [0, 100],
                             "description": "可用因子按权重归一化；只使用指定已收盘日期及以前的前复权日线；未回测研究分"},
            "source": {"provider": "HiThink Financial-API", "endpoint": "/api/a-share/prices/historical",
                       "adjust": "forward", "requested_start": start, "requested_end": date,
                       "timestamp": data.get("timestamp") if isinstance(data, dict) else None,
                       "response_adjust": data.get("adjust") if isinstance(data, dict) else None,
                       "latest_bar_date": trend.get("as_of"), "request_count": 1,
                       "revision_note": "前复权历史以当前数据商结果为准，可能后续修订；不等于当时保存的数据版本",
                       "quote_requested": False, "auction_requested": False}}
