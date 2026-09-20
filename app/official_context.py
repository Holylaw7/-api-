"""Bounded, dated official observations for an already completed report.

This module never changes auction scores or starts calendar/member-list scans.
The caller owns calendar/closed-session checks and persistence. All four public
requests carry an explicit date; their complete data remains under ``raw``.
"""
from __future__ import annotations

import copy
import math
import re
import statistics
from collections import Counter
from datetime import datetime

from .provider import APIError, SHANGHAI, date_ms

BENCHMARK_PATH = "/api/a-share/auction/short-term-benchmark"
DRAGON_TIGER_PATH = "/api/a-share/special-data/dragon-tiger-list"
ROW_LIMIT = 30
BOARDS = ("all", "org", "hot_money")
GROUPS = ("one_day", "three_day", "other")
_CODE = re.compile(r"[0-9]{6}\.(SH|SZ|BJ)\Z")

DEFINITION = (
    "补充观察仅对应所选已收盘报告日期，不改变竞价和涨停评分。短线风向标是官方样本，"
    "不是全市场；龙虎榜是公开上榜记录净买入，不是全市场或板块主力资金流。"
    "全部、机构、游资三榜存在重叠，不可相加。"
)
BOARD_DEFINITION = (
    "金额单位为元；change、net_rate等原字段为小数，带_pct的派生字段为百分数。"
    "1日榜、3日榜和未知区间分开；同股票同区间的多条记录完整保留并标记重复，"
    "不假设同股同区间多条记录互斥，不去重取最大值或累计净额。游资记录按游资与股票展开，"
    "reported_hot_money_net_value为接口提供的该游资聚合值，不能逐行重复相加。"
    "本模块不计算榜单或概念净额合计。"
)


class _InvalidData(ValueError):
    """Only locally authored, safe contract validation messages."""


def _number(value, *, nonnegative=False):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        if not math.isfinite(value) or (nonnegative and value < 0):
            return None
    except OverflowError:
        return None
    return value


def _integer(value, *, nonnegative=True):
    if not isinstance(value, int) or isinstance(value, bool) or (nonnegative and value < 0):
        return None
    return value


def _text(value):
    return value if isinstance(value, str) else None


def _pct(value):
    number = _number(value)
    result = number * 100 if number is not None else None
    return _number(result)


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("观察日期须为 YYYY-MM-DD")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("观察日期无效") from None
    if parsed > datetime.now(SHANGHAI).date():
        raise ValueError("不能补充未来日期的观察")
    return value


def _items(data, key):
    rows = data.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise _InvalidData(f"{key} 不是有效记录数组")
    return rows


def _benchmark(data, target):
    if data.get("date") != target:
        raise _InvalidData("风向标 date 与所选报告日期不一致")
    if (_integer(data.get("date_ms")) is None
            or data["date_ms"] != date_ms(target)):
        raise _InvalidData("风向标 date_ms 不等于所选日期的上海时区零点")
    raw_rows = _items(data, "item")
    codes = [row.get("thscode") for row in raw_rows]
    if any(not isinstance(code, str) or not _CODE.fullmatch(code) for code in codes):
        raise _InvalidData("风向标包含无效的完整 A 股代码")
    if len(set(codes)) != len(codes):
        raise _InvalidData("风向标包含重复股票，不能可靠计算样本统计")
    rows, warnings = [], []
    bad_values, bad_tags = 0, 0
    for index, source in enumerate(raw_rows):
        value = _number(source.get("auction_pct"))
        if value is None:
            bad_values += 1
        tags = source.get("tags")
        if not isinstance(tags, list):
            tags, bad_tags = [], bad_tags + 1
        elif any(not isinstance(tag, str) for tag in tags):
            tags, bad_tags = [tag for tag in tags if isinstance(tag, str)], bad_tags + 1
        rows.append({"thscode": source["thscode"], "ticker": source["thscode"][:6],
                     "name": _text(source.get("name")), "auction_pct": value,
                     "tags": list(dict.fromkeys(tags)), "record_index": index + 1})
    values = [row["auction_pct"] for row in rows if row["auction_pct"] is not None]
    if bad_values:
        warnings.append(f"{bad_values} 只样本缺少有效竞价涨跌幅，不计入均值、中位数和上涨占比")
    if bad_tags:
        warnings.append(f"{bad_tags} 条标签缺失或格式异常，标签不参与任何评分")
    rows.sort(key=lambda row: (row["auction_pct"] is None,
                               -(row["auction_pct"] or 0), row["thscode"]))
    mean = statistics.mean(values) if values else None
    median = statistics.median(values) if values else None
    return {
        "status": "partial" if warnings else "ready", "date": target,
        "date_ms": data["date_ms"], "response_timestamp": _number(data.get("timestamp")),
        "endpoint": BENCHMARK_PATH, "sample_count": len(rows),
        "valid_auction_count": len(values), "mean_auction_pct": _number(mean),
        "median_auction_pct": _number(median),
        "positive_ratio_pct": 100 * sum(value > 0 for value in values) / len(values) if values else None,
        "rows": rows[:ROW_LIMIT], "shown_count": min(len(rows), ROW_LIMIT),
        "truncated": len(rows) > ROW_LIMIT, "warnings": warnings,
        "sort": "竞价涨跌幅降序、缺值末尾、同值按完整代码升序；最多显示30只",
        "definition": "auction_pct为官方百分数原值，不乘100；上涨占比分母为有效涨幅样本，零涨幅计入分母但不计上涨。样本不是全市场，标签只作解释。timestamp仅为响应组装时间，不证明交易日期。",
    }


def _stock_row(source, index, *, actor=None, actor_total=None):
    """Explicit public whitelist: future upstream fields stay in raw only."""
    code = source.get("thscode")
    code = code if isinstance(code, str) and _CODE.fullmatch(code) else None
    row = {"thscode": code, "ticker": code[:6] if code else None,
           "name": _text(source.get("name")), "range_days": _integer(source.get("range_days")),
           "limit_reason": _text(source.get("limit_reason")), "record_index": index,
           "hot_money_name": actor, "reported_hot_money_net_value": actor_total,
           "quality": []}
    if code is None:
        row["quality"].append("完整股票代码缺失或无效")
    if row["range_days"] not in (1, 3):
        row["quality"].append("上榜区间未知，不并入1日或3日榜")
    for field in ("net_value", "org_net_value", "hot_money_net_value", "hot_money_item_net_value"):
        row[field] = _number(source.get(field))
    for field in ("buy_value", "sell_value", "amount"):
        row[field] = _number(source.get(field), nonnegative=True)
    for field in ("change", "net_rate", "org_net_rate", "hot_money_net_rate", "hot_money_item_net_rate"):
        row[field] = _number(source.get(field))
        row[field + "_pct"] = _pct(source.get(field))
    for field in ("hot_rank", "org_buy_num", "org_sell_num"):
        row[field] = _integer(source.get(field))
    concepts = source.get("concept_list")
    row["concept_list"] = [{"name": item["name"]} for item in concepts
                            if isinstance(item, dict) and isinstance(item.get("name"), str)] if isinstance(concepts, list) else []
    money_field = "hot_money_item_net_value" if actor is not None else "net_value"
    if row[money_field] is None:
        row["quality"].append("记录净额缺失，保留为空")
    return row


def _board(data, target, board_type):
    if data.get("trade_date") != target:
        raise _InvalidData("龙虎榜 trade_date 与所选报告日期不一致")
    if data.get("board_type") != board_type:
        raise _InvalidData("龙虎榜 board_type 与请求榜单不一致")
    rows, warnings, actors = [], [], []
    timestamp = _integer(data.get("timestamp"))
    timestamp_matches_date = timestamp == date_ms(target) if timestamp is not None else None
    if timestamp_matches_date is False:
        warnings.append("龙虎榜 timestamp 与 trade_date 的上海时区零点不一致；按明确 trade_date 展示但日期证据有冲突，未静默改写时间")
    elif timestamp_matches_date is None:
        warnings.append("龙虎榜 timestamp 缺失或无效；仅有明确 trade_date，缺少约定的零点时间交叉核验")
    if board_type == "hot_money":
        for actor in _items(data, "hot_money_items"):
            name = _text(actor.get("name"))
            total = _number(actor.get("buying"))
            if not name:
                warnings.append("游资名称缺失，不能确认该组席位身份")
            actor_rows = _items(actor, "rows")
            actors.append({"name": name, "reported_net_value": total, "row_count": len(actor_rows)})
            for source in actor_rows:
                row = _stock_row(source, len(rows) + 1, actor=name, actor_total=total)
                if row["hot_money_item_net_value"] is None and "记录净额缺失，保留为空" not in row["quality"]:
                    row["quality"].append("记录净额缺失，保留为空")
                rows.append(row)
    else:
        rows = [_stock_row(source, index + 1) for index, source in enumerate(_items(data, "stock_items"))]

    def duplicate_key(row):
        return row["hot_money_name"], row["thscode"], row["range_days"]

    keys = Counter(duplicate_key(row) for row in rows if row["thscode"] is not None)
    duplicates = 0
    for row in rows:
        row["duplicate_record"] = row["thscode"] is not None and keys[duplicate_key(row)] > 1
        if row["duplicate_record"]:
            duplicates += 1
            row["quality"].append("同股票同区间存在多记录，保留原记录，不累计净额")
    if duplicates:
        warnings.append(f"{duplicates} 条记录处于同股票同区间的重复组，未去重或累计金额")
    invalid_codes = sum(row["thscode"] is None for row in rows)
    if invalid_codes:
        warnings.append(f"{invalid_codes} 条记录缺少有效完整代码，不计入可识别股票数")
    other = sum(row["range_days"] not in (1, 3) for row in rows)
    if other:
        warnings.append(f"{other} 条记录上榜区间不是1日或3日，单独归为未知区间")
    reported_count = _integer(data.get("count"))
    reported_stocks = _integer(data.get("stock_count"))
    if reported_count is None or reported_stocks is None:
        warnings.append("接口记录数或去重股票数缺失，不能据此判断完整覆盖")
    observed_stocks = len({row["thscode"] for row in rows if row["thscode"] is not None})
    if board_type == "hot_money":
        # The container counts describe upstream records/stocks, while this
        # board exposes only the actors' associated rows. They are not the
        # same universe; one code may also occur under several actors.
        coverage = {
            "status": "unknown", "scope": "hot_money_items.rows",
            "record_count_matches": None, "stock_count_matches": None,
            "definition": "上游count/stock_count与游资关联记录的展开条数/去重股票数范围不同；两组分别展示，不用上游数量作游资样本覆盖率分母，无法据此证明全量覆盖或缺失。",
        }
    else:
        record_matches = reported_count == len(rows) if reported_count is not None else None
        stock_matches = reported_stocks == observed_stocks if reported_stocks is not None else None
        inconsistent = record_matches is False or stock_matches is False or bool(invalid_codes)
        coverage = {
            "status": "inconsistent" if inconsistent else "verified" if record_matches and stock_matches else "unknown",
            "scope": "stock_items", "record_count_matches": record_matches,
            "stock_count_matches": stock_matches,
            "definition": "本榜stock_items条数与上游count核对，有效完整代码去重数与stock_count核对；数量吻合只说明该响应的计数一致，不证明净额可相加或全市场资金覆盖。",
        }
        if record_matches is False:
            warnings.append(f"上游声明{reported_count}条记录，实际收到{len(rows)}条，记录数量不一致，不能视为完整榜单或完整空榜")
        if stock_matches is False:
            warnings.append(f"上游声明{reported_stocks}只去重股票，实际识别{observed_stocks}只，股票数量不一致，不能视为完整覆盖")
    groups = {"one_day": [], "three_day": [], "other": []}
    for row in rows:
        group = "one_day" if row["range_days"] == 1 else "three_day" if row["range_days"] == 3 else "other"
        groups[group].append(row)
    # Retain every observation before display truncation; ordering uses the
    # relevant board's net field, never an invented total across categories.
    net_field = {"all": "net_value", "org": "org_net_value", "hot_money": "hot_money_item_net_value"}[board_type]
    missing_net = sum(row[net_field] is None for row in rows)
    if missing_net:
        warnings.append(f"{missing_net} 条记录缺少本榜单对应的净额，排序时置于末尾且不补零")
    for group in groups.values():
        group.sort(key=lambda row: (row[net_field] is None, -(row[net_field] or 0),
                                   row["thscode"] or "~", row["hot_money_name"] or "", row["record_index"]))
    group_counts = {key: len(group) for key, group in groups.items()}
    visible = {key: group[:ROW_LIMIT] for key, group in groups.items()}
    warnings = list(dict.fromkeys(warnings))
    return {
        "status": "partial" if warnings else "ready", "board_type": board_type,
        "trade_date": target, "timestamp": timestamp,
        "timestamp_matches_date": timestamp_matches_date, "date_basis": "explicit_trade_date",
        "endpoint": DRAGON_TIGER_PATH, "reported_count": reported_count,
        "reported_stock_count": reported_stocks, "received_rows": len(rows),
        "observed_stock_count": observed_stocks, "coverage": coverage,
        "groups": visible, "group_counts": group_counts,
        "shown_count": sum(len(group) for group in visible.values()),
        "truncated": any(count > ROW_LIMIT for count in group_counts.values()),
        "actor_count": len(actors) if board_type == "hot_money" else None,
        "actors": actors[:ROW_LIMIT], "actors_truncated": len(actors) > ROW_LIMIT,
        "duplicate_record_count": duplicates, "warnings": warnings,
        "sort": f"每区间按{net_field}降序、缺值末尾，再按代码/游资名称/原始序号；每组最多30条",
        "definition": BOARD_DEFINITION,
    }


def build_official_context(provider, date, should_stop=None):
    """Read at most four official endpoints, isolating failures per section.

    ``provider.get`` must return validated ApiResponse.data, as HiThinkProvider
    does. Cancellation is checked before every request. No fallback to another
    date, default-date request, synthetic data or additional scan is allowed.
    """
    target = _date(date)
    result = {"date": target, "mode": "live", "generated_at": None,
              "status": "unavailable", "cancelled": False,
              "requests": {"attempted": 0, "max": 4}, "benchmark": None,
              "dragon_tiger": {board: None for board in BOARDS},
              "warnings": [], "errors": [], "raw": {}, "definition": DEFINITION}
    requests = [("benchmark", BENCHMARK_PATH, {"date": target})]
    requests.extend((board, DRAGON_TIGER_PATH, {"date": target, "board_type": board}) for board in BOARDS)
    for section, path, params in requests:
        if should_stop is not None and should_stop():
            result["cancelled"] = True
            result["warnings"].append("补充观察已中断，未请求的分项保留为空；已到数据不能代表完整四项")
            break
        result["requests"]["attempted"] += 1
        try:
            data = provider.get(path, params)
            result["raw"][section] = copy.deepcopy(data)
            if not isinstance(data, dict):
                raise _InvalidData("接口 data 不是有效对象")
            normalized = _benchmark(data, target) if section == "benchmark" else _board(data, target, section)
            if section == "benchmark":
                result["benchmark"] = normalized
            else:
                result["dragon_tiger"][section] = normalized
            result["warnings"].extend(f"{section}：{warning}" for warning in normalized["warnings"])
        except APIError as error:
            code = error.code if isinstance(error.code, int) and not isinstance(error.code, bool) else None
            kind = "not_ready" if code == 3002 else "api_error"
            message = "官方数据尚未就绪" if kind == "not_ready" else "官方分项请求失败"
            result["errors"].append({"section": section, "kind": kind, "code": code, "message": message})
            result["warnings"].append(f"{section}：{message}，缺失不视为零")
        except _InvalidData as error:
            result["errors"].append({"section": section, "kind": "invalid_data", "code": None, "message": str(error)})
            result["warnings"].append(f"{section}：{error}，该分项不用于观察")
        except Exception:
            # Third-party/provider exception bodies may contain credentials.
            result["errors"].append({"section": section, "kind": "request_error", "code": None, "message": "分项读取或格式处理失败"})
            result["warnings"].append(f"{section}：分项读取或格式处理失败，缺失不视为零")
    available = int(result["benchmark"] is not None) + sum(value is not None for value in result["dragon_tiger"].values())
    result["status"] = ("cancelled" if result["cancelled"] else "unavailable" if not available
                        else "partial" if available < 4 or result["warnings"] else "ready")
    result["generated_at"] = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    return result
