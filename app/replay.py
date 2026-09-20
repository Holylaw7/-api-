"""Pure, point-in-time auction replay and retrospective pool baselines.

No I/O is performed here. A manifest is evidence supplied by the caller; an
importer's claimed collection time cannot be independently authenticated here.
"""
from __future__ import annotations

import copy
import re
from datetime import date as Date, datetime, time

from .engine import AuctionEngine, DEFAULT_WEIGHTS, SHANGHAI, strict_continuity

_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_CODE = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")
_BUCKETS = ("1", "2", "3", "4", "5+", "unknown")


def _date(value):
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError("日期必须为严格的 YYYY-MM-DD")
    try:
        Date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("日期无效") from exc
    return value


def _stamp(value):
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("时间必须为带时区的 ISO 时间") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("时间必须明确标注时区")
    return stamp.astimezone(SHANGHAI)


def _calendar(values):
    if not isinstance(values, list):
        raise ValueError("交易日历必须是日期列表")
    days = [_date(value) for value in values]
    if len(set(days)) != len(days):
        raise ValueError("交易日历含重复日期")
    return sorted(days)


def _codes(values):
    if not isinstance(values, list) or any(not isinstance(code, str) or not _CODE.fullmatch(code) for code in values):
        raise ValueError("股票代码必须为完整的六位代码及交易所后缀")
    if len(set(values)) != len(values):
        raise ValueError("股票代码列表含重复代码")
    return values


def _pool(pool, day):
    """Return verified members, or unknown. Empty verified pools remain valid."""
    if not isinstance(pool, dict) or pool.get("date") != day or pool.get("complete") is not True:
        return None, "缺少日期一致且完整的官方涨停池"
    if pool.get("mode", "live") != "live":
        return None, "演示或未知模式不能作真实结果标签"
    try:
        retrieved = _stamp(pool.get("retrieved_at"))
        cutoff = datetime.combine(Date.fromisoformat(day), time(15, 10), SHANGHAI)
        if retrieved < cutoff:
            return None, "涨停池不是在目标交易日 15:10 之后取得"
        rows = pool.get("rows")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return None, "涨停池明细格式无效"
        codes = _codes([row.get("thscode") for row in rows])
    except (ValueError, TypeError):
        return None, "涨停池时间或完整代码无效、重复"
    return dict(zip(codes, rows)), None


def replay_session(manifest, batches, outcome, weights, *, checkpoint="09:24:50", should_stop=None):
    """Replay only locally received batches visible at a fixed decision time.

    Rows always describe the manifest's complete previous-session limit-up
    candidate universe, including missing observations. Today's outcome never
    affects candidate selection, factors, scores or ranks.
    """
    if not isinstance(manifest, dict) or manifest.get("mode") != "live":
        raise ValueError("历史竞价研究只接受明确的 live 清单，演示数据不可混入")
    day, previous = _date(manifest.get("date")), _date(manifest.get("previous_date"))
    if not isinstance(checkpoint, str) or not re.fullmatch(r"[0-9]{2}:[0-9]{2}:[0-9]{2}", checkpoint):
        raise ValueError("研究截止时刻须为 HH:MM:SS")
    try:
        clock = time.fromisoformat(checkpoint)
    except ValueError as exc:
        raise ValueError("研究截止时刻无效") from exc
    if not time(9, 15) <= clock <= time(9, 26, 59):
        raise ValueError("研究截止时刻须处于 09:15:00–09:26:59")
    cutoff = datetime.combine(Date.fromisoformat(day), clock, SHANGHAI)
    start = cutoff.replace(hour=9, minute=15, second=0)
    calendar = _calendar(manifest.get("calendar"))
    codes = set(_codes(manifest.get("codes")))
    source = manifest.get("source")
    if source not in ("local_preparation", "historical_import"):
        raise ValueError("研究清单来源必须为 local_preparation 或 historical_import")
    context = manifest.get("context")
    if not isinstance(context, dict):
        raise ValueError("昨日候选上下文必须是按完整代码索引的对象")
    candidates = sorted(_codes(list(context)))
    if any(not isinstance(row, dict) for row in context.values()):
        raise ValueError("昨日候选上下文明细格式无效")
    position = calendar.index(day) if day in calendar else -1
    adjacent = position > 0 and calendar[position - 1] == previous
    context_verified = (adjacent and manifest.get("context_complete") is True
                        and all(row.get("context_date") == previous for row in context.values()))
    try:
        prepared = _stamp(manifest.get("prepared_at"))
        prepared_in_time = prepared <= start
    except ValueError:
        prepared = None
        prepared_in_time = False
    point_in_time = manifest.get("point_in_time") is True and prepared_in_time and context_verified
    warnings = ["结果标签为收盘后取得的官方涨停池成员；命中率不是可成交收益率。",
                "只对昨日完整涨停池候选回放，未观察及缺因子股票保留缺失；共同样本不代表全部候选。"]
    if not context_verified:
        warnings.append("昨日完整池或相邻交易日证据未核验，不可用于优化。")
    if not prepared_in_time or manifest.get("point_in_time") is not True:
        warnings.append("清单没有在当日 09:15 前固定的证据，仅供回溯描述，不可用于优化。")
    if source == "historical_import":
        warnings.append("外部导入的准备时刻与时点声明由提供者保证，程序不能认证外部采集时间。")
    members, outcome_warning = _pool(outcome, day)
    if outcome_warning:
        warnings.append(outcome_warning + "；本会话结果标签全部为空。")
    engine = AuctionEngine(weights)
    safe_context = context if context_verified else {}
    selected = set(candidates) & codes
    ignored = {"after_checkpoint": 0, "other_date": 0, "before_auction": 0, "invalid": 0}
    considered = 0
    cancelled = False
    if not isinstance(batches, (list, tuple)):
        raise ValueError("竞价批次必须为保留存储原顺序的列表")
    for batch in batches:
        if should_stop and should_stop():
            cancelled = True
            break
        try:
            if not isinstance(batch, (list, tuple)) or len(batch) != 3:
                raise ValueError("批次格式无效")
            received, stage, payload = batch
            received = _stamp(received)
            if stage not in ("live", "final") or not isinstance(payload, dict):
                raise ValueError("批次阶段或载体无效")
        except ValueError:
            ignored["invalid"] += 1
            continue
        if received.date().isoformat() != day:
            ignored["other_date"] += 1
            continue
        if received > cutoff:
            ignored["after_checkpoint"] += 1
            continue
        if received < start:
            ignored["before_auction"] += 1
            continue
        # Restrict the original payload without changing its dates or metadata.
        payload = copy.deepcopy(payload)
        body = payload.get("data") if "data" in payload else payload
        if isinstance(body, dict):
            if isinstance(body.get("item"), list):
                body["item"] = [row for row in body["item"] if isinstance(row, dict) and row.get("thscode") in selected]
            requested = body.get("_requested_codes")
            if isinstance(requested, str):
                requested = requested.split(",")
            if isinstance(requested, list):
                body["_requested_codes"] = [code for code in requested if code in selected]
        engine.ingest(payload, received, safe_context)
        considered += 1
    if cancelled:
        warnings.append("回放已取消，部分结果不可用于优化。")
    if ignored["invalid"]:
        warnings.append("存在无效批次，已排除并保留计数；不允许将此会话用于优化。")
    ranked = {row["thscode"]: row for row in engine.rankings(cutoff)}
    rows = []
    keys = ("thscode", "name", "score", "raw_score", "rank", "auction_pct", "auction_amount",
            "factors", "quality", "phase", "updated_at", "data_status", "context_date", "continue_day_cnt")
    for code in candidates:
        observed = ranked.get(code)
        if observed is not None:
            row = {key: copy.deepcopy(observed.get(key)) for key in keys}
        else:
            reason = "未在盘前固定的采集范围" if code not in codes else "截止时刻前没有有效竞价观察"
            row = {"thscode": code, "name": str(context[code].get("name") or code)[:80],
                   "score": None, "raw_score": None, "rank": None, "auction_pct": None,
                   "auction_amount": None, "context_date": previous, "continue_day_cnt": None,
                   "factors": {factor: {"score": None, "value": None, "available": False,
                                        "weight": engine.weights[factor], "contribution": None}
                               for factor in DEFAULT_WEIGHTS},
                   "quality": {"status": "unobserved", "observation_count": 0,
                               "factor_coverage": 0, "flags": [reason]}, "missing_reason": reason}
        row["label"] = code in members if members is not None else None
        rows.append(row)
    rows.sort(key=lambda row: (row["rank"] is None, row["rank"] or 0, row["thscode"]))
    observed_count = sum(row["quality"].get("observation_count", 0) > 0 for row in rows)
    scored_count = sum(row["score"] is not None for row in rows)
    engine_summary = engine.summary()
    integrity_ok = not (ignored["invalid"] or engine_summary["rejected_records"] or engine_summary["out_of_order_responses"])
    eligible = point_in_time and members is not None and not cancelled and integrity_ok
    if not integrity_ok:
        warnings.append("存在无效、被引擎拒绝或倒序的记录；仅保留诊断回放，不进入权重优化。")
    warnings.extend(engine_summary["warnings"])
    return {"date": day, "previous_date": previous,
            "mode": "live" if point_in_time else "reconstructed", "origin": source,
            "checkpoint": checkpoint, "prepared_at": prepared.isoformat() if prepared else None,
            "point_in_time": point_in_time, "status": "cancelled" if cancelled else "ready" if eligible and scored_count == len(candidates) else "partial",
            "rows": rows, "weights": engine.weights,
            "quality": {"candidate_count": len(candidates), "observed_count": observed_count,
                        "scored_count": scored_count, "unobserved_count": len(candidates) - observed_count,
                        "context_verified": context_verified, "outcome_verified": members is not None,
                        "integrity_verified": integrity_ok,
                        "eligible_for_optimization": eligible, "processed_batches": considered,
                        "ignored_batches": ignored, "cancelled": cancelled,
                        "engine_rejected_records": engine_summary["rejected_records"],
                        "engine_duplicate_responses": engine_summary["duplicate_responses"],
                        "engine_out_of_order_responses": engine_summary["out_of_order_responses"]},
            "warnings": list(dict.fromkeys(warnings)),
            "definition": "在本地接收时刻截止前逐批重放现有七因子；当天涨停池仅作标签，不进入候选和因子。"}


def _bucket(row):
    count = strict_continuity(row)
    return "unknown" if count is None or count < 1 else "5+" if count >= 5 else str(int(count))


def pool_transitions(pools, calendar):
    """Describe adjacent complete official pools, never an auction prediction."""
    if not isinstance(pools, dict):
        raise ValueError("涨停池必须按日期索引")
    days = _calendar(calendar)
    for day in pools:
        _date(day)
    verified, skipped, warnings = {}, [], []
    for day, pool in pools.items():
        if day not in days:
            skipped.append({"date": day, "reason": "日期不在已核验交易日历"})
            continue
        members, warning = _pool(pool, day)
        if members is not None:
            verified[day] = members
        else:
            skipped.append({"date": day, "reason": warning})
    rows = []
    for day in sorted(pools):
        if day not in verified:
            continue
        index = days.index(day)
        if index == 0 or days[index - 1] not in verified:
            skipped.append({"date": day, "reason": "缺少日历中紧邻的前一交易日完整池，不跨缺日连接"})
            continue
        previous = days[index - 1]
        old, new = verified[previous], verified[day]
        repeats = set(old) & set(new)
        groups = {bucket: {"yesterday_count": 0, "repeat_count": 0, "repeat_rate_pct": None} for bucket in _BUCKETS}
        for code, item in old.items():
            group = groups[_bucket(item)]
            group["yesterday_count"] += 1
            group["repeat_count"] += code in repeats
        for group in groups.values():
            total = group["yesterday_count"]
            group["repeat_rate_pct"] = round(100 * group["repeat_count"] / total, 4) if total else None
        prior = days[max(0, index - 5):index]
        complete_days = [value for value in prior if value in verified]
        full_window = len(prior) == 5 and len(complete_days) == 5
        background_rows = []
        for code in sorted(old):
            count = sum(code in verified[value] for value in complete_days)
            background_rows.append({"thscode": code, "known_appearance_count": count,
                                    "appearance_count": count if full_window else None})
        rows.append({"date": day, "previous_date": previous, "yesterday_count": len(old),
                     "repeat_count": len(repeats),
                     "repeat_rate_pct": round(100 * len(repeats) / len(old), 4) if old else None,
                     "groups": groups,
                     "prior_five_sessions": {"dates": prior, "expected_days": 5,
                                             "complete_days": len(complete_days), "complete": full_window,
                                             "rows": background_rows}})
    total = sum(row["yesterday_count"] for row in rows)
    repeated = sum(row["repeat_count"] for row in rows)
    if skipped:
        warnings.append("部分日期缺少已收盘完整池或相邻交易日证据，已跳过；缺失不补零。")
    return {"status": "ready" if rows and not skipped else "partial" if rows else "unavailable",
            "rows": rows, "skipped": skipped,
            "summary": {"day_count": len(rows), "total_candidates": total, "total_repeats": repeated,
                        "weighted_repeat_rate_pct": round(100 * repeated / total, 4) if total else None},
            "warnings": warnings,
            "definition": "相邻交易日官方完整涨停池的成员延续基线，不含竞价，亦非预测命中率；五日出现次数只使用目标日前的数据。"}
