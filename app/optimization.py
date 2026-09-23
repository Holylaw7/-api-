"""Pure, chronological research on frozen, labelled auction rankings.

This module never fetches data, fills missing labels, changes factor formulas,
or applies weights. The caller must replay genuine observations up to CHECKPOINT
and attach labels from an independently verified, complete closing limit-up pool.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
import hashlib
import json
import math
import random
import re

from .engine import AuctionEngine, DEFAULT_WEIGHTS, FACTOR_LABELS

VERSION = "1.0"
CHECKPOINT = "09:24:50"
FACTOR_KEYS = tuple(DEFAULT_WEIGHTS)
MIN_DAYS = 30
MIN_ROWS = 300
MIN_CLASS_ROWS = 30
MAX_CANDIDATES = 64
SEED = 20260921
CODE = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)\Z")
DEFINITION = (
    "只评价本机真实竞价在09:24:50前已收到的观察，标签为当日收盘是否进入已核验完整涨停池。"
    "七因子完整的固定共同样本按日期分割；指标为选中股票的收盘涨停命中率，不是收益率或涨停概率。"
    "最后20%日期独立保留，只评价一次，不用于候选、因子、阈值或权重选择；建议不会自动应用。"
)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (OverflowError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _score(value):
    value = _number(value)
    return value if value is not None and 0 <= value <= 100 else None


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _round(value):
    return round(value, 6) if value is not None else None


def _wilson(hits, selected):
    if not selected:
        return None
    z = 1.959963984540054
    p = hits / selected
    denom = 1 + z * z / selected
    center = (p + z * z / (2 * selected)) / denom
    radius = z * math.sqrt((p * (1 - p) + z * z / (4 * selected)) / selected) / denom
    return {
        "lower": _round(max(0, center - radius)),
        "upper": _round(min(1, center + radius)),
        "confidence_level": .95,
        "definition": "合并选中股日的Wilson描述区间；同日及同股相关，不能视为可靠独立样本置信保证。",
    }


def _normalize(weights):
    return AuctionEngine.validate_weights(weights)


def _prepare(sessions, should_stop):
    diagnostics = Counter()
    missing = Counter({key: 0 for key in FACTOR_KEYS})
    dates = Counter(_date(item.get("date")) for item in sessions
                    if isinstance(item, dict) and item.get("mode") == "live"
                    and item.get("checkpoint") == CHECKPOINT
                    and not (isinstance(item.get("quality"), dict)
                             and item["quality"].get("eligible_for_optimization") is False))
    clean, day_diagnostics, carried_rows = [], [], 0
    for session in sessions:
        if should_stop and should_stop():
            return clean, diagnostics, missing, day_diagnostics, True, carried_rows
        if not isinstance(session, dict):
            diagnostics["invalid_session"] += 1
            continue
        day = _date(session.get("date"))
        if session.get("mode") != "live":
            diagnostics["non_live_session"] += 1
            continue
        if isinstance(session.get("quality"), dict) and session["quality"].get("eligible_for_optimization") is False:
            diagnostics["ineligible_quality_session"] += 1
            continue
        if day is None:
            diagnostics["invalid_date_session"] += 1
            continue
        if session.get("checkpoint") != CHECKPOINT:
            diagnostics["checkpoint_mismatch_session"] += 1
            continue
        # Fail closed for conflicting genuine snapshots of one day, regardless of order.
        if dates[day] != 1:
            diagnostics["duplicate_date_session"] += 1
            continue
        raw_rows = session.get("rows")
        if not isinstance(raw_rows, list):
            diagnostics["invalid_rows_session"] += 1
            continue
        counts = Counter(row.get("thscode") for row in raw_rows
                         if isinstance(row, dict) and isinstance(row.get("thscode"), str))
        kept, excluded = [], Counter()
        complete = True
        day_carried = 0
        for row in raw_rows:
            diagnostics["input_rows"] += 1
            if not isinstance(row, dict) or not isinstance(row.get("thscode"), str) or not CODE.fullmatch(row["thscode"]):
                excluded["invalid_code_rows"] += 1
                complete = False
                continue
            code = row["thscode"]
            if counts[code] != 1:
                excluded["duplicate_code_rows"] += 1
                complete = False
                continue
            if not isinstance(row.get("label"), bool):
                excluded["unknown_label_rows"] += 1
                complete = False
                continue
            base = _score(row.get("score"))
            if base is None:
                excluded["unranked_rows"] += 1
                continue
            factors = row.get("factors")
            factors = factors if isinstance(factors, dict) else {}
            ratio_entry = factors.get("volume_ratio")
            if isinstance(ratio_entry, dict) and ratio_entry.get("value_source") == "carried":
                day_carried += 1
                carried_rows += 1
            values = {}
            for key in FACTOR_KEYS:
                entry = factors.get(key)
                value = _score(entry.get("score")) if isinstance(entry, dict) else None
                if value is None:
                    missing[key] += 1
                values[key] = value
            if any(value is None for value in values.values()):
                excluded["incomplete_factor_rows"] += 1
                continue
            kept.append({"thscode": code, "label": row["label"], "score": base, "factors": values})
        diagnostics.update(excluded)
        kept.sort(key=lambda row: row["thscode"])
        complete = complete and bool(kept)
        day_diagnostics.append({
            "date": day, "input_rows": len(raw_rows), "common_rows": len(kept),
            "complete": complete, "excluded": dict(sorted(excluded.items())),
            "volume_ratio_carried_rows": day_carried,
        })
        if kept:
            clean.append({"date": day, "rows": kept, "complete": complete})
    clean.sort(key=lambda item: item["date"])
    day_diagnostics.sort(key=lambda item: item["date"])
    return clean, diagnostics, missing, day_diagnostics, False, carried_rows


def _row_score(row, weights, baseline=False):
    # The stored baseline is the exact visible engine score at the frozen cutoff.
    if baseline:
        return row["score"]
    return round(sum(weights[key] * row["factors"][key] for key in FACTOR_KEYS), 4)


def _evaluate(sessions, weights, top_k, *, baseline=False, include_daily=True, should_stop=None):
    daily = []
    for session in sessions:
        if should_stop and should_stop():
            raise _Cancelled
        ranked = sorted(session["rows"], key=lambda row: (-_row_score(row, weights, baseline), row["thscode"]))
        selected = ranked[:top_k]
        hits = sum(row["label"] for row in selected)
        positives = sum(row["label"] for row in ranked)
        rate = _ratio(positives, len(ranked))
        precision = _ratio(hits, len(selected))
        daily.append({
            "date": session["date"], "pool_count": len(ranked), "positive_count": positives,
            "requested_k": top_k, "selected_count": len(selected), "hits": hits,
            "precision": _round(precision), "recall": _round(_ratio(hits, positives)),
            "pool_base_rate": _round(rate), "lift": _round(precision / rate if rate else None),
            "selected_codes": [row["thscode"] for row in selected],
            "complete": session["complete"],
        })
    pool_count = sum(item["pool_count"] for item in daily)
    positive_count = sum(item["positive_count"] for item in daily)
    selected_count = sum(item["selected_count"] for item in daily)
    hits = sum(item["hits"] for item in daily)
    base_rate = _ratio(positive_count, pool_count)
    pooled_precision = _ratio(hits, selected_count)
    result = {
        "day_count": len(daily), "pool_count": pool_count, "positive_count": positive_count,
        "requested_k": top_k, "selected_count": selected_count, "hits": hits,
        "precision": _round(sum(item["hits"] / item["selected_count"] for item in daily) / len(daily) if daily else None),
        "pooled_precision": _round(pooled_precision),
        "recall": _round(_ratio(hits, positive_count)),
        "pool_base_rate": _round(base_rate),
        "lift": _round(pooled_precision / base_rate if base_rate else None),
        "wilson_interval": _wilson(hits, selected_count),
        "definition": "precision为各日precision@K等权平均；pooled_precision/recall/lift为共同股日合并口径；不足K只按实际选中数计算。",
    }
    if include_daily:
        result["daily"] = daily
    return result


def _candidates(baseline):
    candidates, seen = [], set()

    def add(name, weights, kind):
        normalized = _normalize(weights)
        signature = tuple(round(normalized[key], 12) for key in FACTOR_KEYS)
        if signature in seen or len(candidates) >= MAX_CANDIDATES:
            return
        seen.add(signature)
        candidates.append({
            "id": name, "kind": kind, "weights": normalized,
            "distance": sum(abs(normalized[key] - baseline[key]) for key in FACTOR_KEYS),
        })

    add("baseline", baseline, "baseline")
    add("equal", {key: 1 for key in FACTOR_KEYS}, "equal")
    for key in FACTOR_KEYS:
        weights = dict(baseline)
        weights[key] = 0
        if sum(weights.values()) > 0:
            add("without_" + key, weights, "ablation")
    for key in FACTOR_KEYS:
        for multiplier in (.5, .75, 1.25, 1.5):
            weights = dict(baseline)
            weights[key] = max(weights[key], .02) * multiplier
            add(f"adjust_{key}_{multiplier}", weights, "perturbation")
    rng = random.Random(SEED)
    for index in range(MAX_CANDIDATES * 2):
        # A floor permits testing currently disabled factors without pure-factor extremes.
        weights = {key: max(baseline[key], .02) * rng.uniform(.75, 1.25) for key in FACTOR_KEYS}
        add(f"perturb_{index + 1:02d}", weights, "perturbation")
        if len(candidates) == MAX_CANDIDATES:
            break
    return candidates


def _tie(candidate):
    return (candidate["id"] != "baseline", round(candidate["distance"], 12), candidate["id"])


def _folds(sessions):
    remaining = len(sessions) - 10
    if remaining < 9:
        return []
    boundaries = [10 + remaining * index // 3 for index in range(4)]
    return [(sessions[:boundaries[index]], sessions[boundaries[index]:boundaries[index + 1]]) for index in range(3)]


class _Cancelled(Exception):
    pass


def optimize_sessions(sessions, baseline_weights, *, top_k=10, should_stop=None):
    """Return an auditable weight proposal; never change the caller's inputs.

    A complete day has known boolean labels and unique valid codes for every input
    row, plus at least one ranked row with all seven factor scores. Missing factors
    are excluded identically for every candidate, even if its weight becomes zero.
    The caller, not this pure function, proves trade dates and pre-cutoff provenance.
    """
    if not isinstance(sessions, list):
        raise ValueError("实验会话必须是列表")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 100:
        raise ValueError("top_k必须是1至100的整数")
    baseline_weights = _normalize(baseline_weights)
    clean, excluded, missing, day_diagnostics, cancelled, carried_rows = _prepare(sessions, should_stop)
    complete = [session for session in clean if session["complete"]]
    row_count = sum(len(session["rows"]) for session in complete)
    positive_count = sum(row["label"] for session in complete for row in session["rows"])
    evidence = [{"date": session["date"], "complete": session["complete"], "rows": session["rows"]} for session in clean]
    fingerprint = hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    result = {
        "version": VERSION, "status": "insufficient_data", "checkpoint": CHECKPOINT,
        "sample_summary": {
            "input_sessions": len(sessions), "usable_days": len(clean), "complete_days": len(complete),
            "common_rows": sum(len(session["rows"]) for session in clean),
            "complete_rows": row_count, "positive_count": positive_count,
            "negative_count": row_count - positive_count, "required_days": MIN_DAYS,
            "required_rows": MIN_ROWS, "required_class_rows": MIN_CLASS_ROWS,
            "excluded": dict(sorted(excluded.items())), "missing_factors": dict(missing),
            "volume_ratio_carried_rows": carried_rows,
            "days": day_diagnostics, "dataset_id": fingerprint,
            "definition": "完整日须代码唯一且标签均已核验；全部候选只评价原基线可排名、七因子分均完整的固定共同股日。",
        },
        "baseline_weights": dict(baseline_weights), "baseline": None,
        "search": {"candidate_count": 0, "seed": SEED, "folds": [], "holdout_used_for_selection": False},
        "candidate_weights": None, "recommendation": {"accepted": False, "reason": "真实完整样本不足，保留当前权重。"},
        "holdout": None, "factor_diagnostics": [], "warnings": [
            "这是一项排名研究，不含成交可行性、买卖成本和收益回测；竞价已封涨停也可能无法买入。",
            "同一股票和同日市场环境相关；短样本、候选池选择与反复查看保留集都会放大过拟合风险。",
            "未收集的历史竞价无法由收盘涨停池、当前快照或演示数据补造；缺失标签不记为失败。",
            "保留集用于一次最终检查；依据其结果继续改因子或参数后，应另积累全新日期作下一次验证。",
        ],
        "definition": DEFINITION,
    }
    if carried_rows:
        result["warnings"].append(
            f"共同样本中有 {carried_rows} 个股票日使用同会话有界携带的竞价量比（上游实时阶段停发该字段，"
            "按 600 秒内最后有效值参与评分）；逐行保留 value_source/carried_from，可与上游真正下发量比的日期分开比较，"
            "不把携带值当作 09:24:50 当时由上游提供的量比。")
    if cancelled:
        result["status"] = "cancelled"
        result["recommendation"]["reason"] = "实验已取消，未生成或应用新权重。"
        return result
    try:
        result["baseline"] = _evaluate(clean, baseline_weights, top_k, baseline=True, should_stop=should_stop)
        if len(complete) < MIN_DAYS or row_count < MIN_ROWS:
            return result
        holdout_count = max(5, math.ceil(len(complete) * .2))
        development, held_out = complete[:-holdout_count], complete[-holdout_count:]
        development_positive = sum(row["label"] for session in development for row in session["rows"])
        development_rows = sum(len(session["rows"]) for session in development)
        result["sample_summary"].update({
            "development_days": len(development), "holdout_days": len(held_out),
            "development_positive_count": development_positive,
            "development_negative_count": development_rows - development_positive,
        })
        if min(development_positive, development_rows - development_positive) < MIN_CLASS_ROWS:
            result["recommendation"]["reason"] = "开发集正负样本须各至少30股日；保留集标签不能用来放宽搜索门槛。"
            return result
        folds = _folds(development)
        if len(folds) < 3:
            result["recommendation"]["reason"] = "日期不足以形成初训10日、每次验证至少3日的三折顺序实验。"
            return result
        candidates = _candidates(baseline_weights)
        result["search"].update({
            "candidate_count": len(candidates), "objective": "mean_daily_precision_at_k",
            "top_k": top_k, "development_dates": [session["date"] for session in development],
            "holdout_dates": [session["date"] for session in held_out],
            "selection_rule": "每折只由较早训练日提名基线加最多两组候选；各折均获提名者在共同后续验证日合并比较，同分优先基线、再最小权重偏移。",
        })
        common_nominees = {candidate["id"] for candidate in candidates}
        validation_sessions = []
        for fold_index, (training, validation) in enumerate(folds, 1):
            train_metrics = {
                candidate["id"]: _evaluate(training, candidate["weights"], top_k,
                                            baseline=candidate["id"] == "baseline", include_daily=False,
                                            should_stop=should_stop)
                for candidate in candidates
            }
            ordered = sorted(candidates, key=lambda candidate: (-train_metrics[candidate["id"]]["precision"], *_tie(candidate)))
            nominees = [candidates[0]] + [candidate for candidate in ordered if candidate["id"] != "baseline"][:2]
            common_nominees.intersection_update(candidate["id"] for candidate in nominees)
            validation_metrics = {
                candidate["id"]: _evaluate(validation, candidate["weights"], top_k,
                                            baseline=candidate["id"] == "baseline", include_daily=False,
                                            should_stop=should_stop)
                for candidate in nominees
            }
            result["search"]["folds"].append({
                "fold": fold_index, "training_dates": [item["date"] for item in training],
                "validation_dates": [item["date"] for item in validation],
                "nominees": [{"id": candidate["id"], "weights": dict(candidate["weights"]),
                              "training": train_metrics[candidate["id"]], "validation": validation_metrics[candidate["id"]]}
                             for candidate in nominees],
            })
            validation_sessions.extend(validation)
        eligible = [candidate for candidate in candidates if candidate["id"] in common_nominees]
        validation_metrics = {
            candidate["id"]: _evaluate(validation_sessions, candidate["weights"], top_k,
                                        baseline=candidate["id"] == "baseline", should_stop=should_stop)
            for candidate in eligible
        }
        winner = min(eligible, key=lambda candidate: (-validation_metrics[candidate["id"]]["precision"], *_tie(candidate)))
        result["candidate_weights"] = dict(winner["weights"])
        result["search"].update({
            "selected_candidate": winner["id"], "eligible_candidates": [item["id"] for item in eligible],
            "validation_baseline": validation_metrics["baseline"],
            "validation_candidate": validation_metrics[winner["id"]],
        })
        # Factor diagnostics intentionally use only development training/validation.
        initial_train = folds[0][0]
        validation_base = validation_metrics["baseline"]["precision"]
        for key in FACTOR_KEYS:
            single = {factor: float(factor == key) for factor in FACTOR_KEYS}
            without = {factor: baseline_weights[factor] if factor != key else 0 for factor in FACTOR_KEYS}
            without = _normalize(without) if sum(without.values()) else None
            diagnostic = {"factor": key, "label": FACTOR_LABELS[key], "missing_rows": missing[key],
                          "period": "development_only", "single_factor": {}, "without_factor": None}
            for period, sample in (("training", initial_train), ("validation", validation_sessions)):
                diagnostic["single_factor"][period] = _evaluate(sample, single, top_k, include_daily=False, should_stop=should_stop)
            if without is not None:
                diagnostic["without_factor"] = {
                    period: _evaluate(sample, without, top_k, include_daily=False, should_stop=should_stop)
                    for period, sample in (("training", initial_train), ("validation", validation_sessions))
                }
                diagnostic["without_factor"]["validation_precision_delta"] = _round(diagnostic["without_factor"]["validation"]["precision"] - validation_base)
            result["factor_diagnostics"].append(diagnostic)
        # The candidate is frozen above; held-out performance cannot change it.
        held_base = _evaluate(held_out, baseline_weights, top_k, baseline=True, should_stop=should_stop)
        held_candidate = _evaluate(held_out, winner["weights"], top_k, baseline=winner["id"] == "baseline", should_stop=should_stop)
        delta = held_candidate["precision"] - held_base["precision"]
        positive_days = sum(candidate["precision"] > base["precision"]
                            for base, candidate in zip(held_base["daily"], held_candidate["daily"]))
        positive_day_ratio = positive_days / len(held_out)
        validation_delta = validation_metrics[winner["id"]]["precision"] - validation_base
        accepted = winner["id"] != "baseline" and delta >= .03 - 1e-12 and positive_day_ratio >= .6 and validation_delta > 0
        result["status"] = "completed"
        result["holdout"] = {
            "baseline": held_base, "candidate": held_candidate,
            "precision_delta": _round(delta), "precision_delta_pp": _round(delta * 100),
            "positive_gain_days": positive_days, "positive_gain_day_ratio": _round(positive_day_ratio),
            "evaluation_count": 1,
        }
        result["recommendation"] = {
            "accepted": accepted, "automatic_application": False,
            "reason": "达到预先设定的验证与保留集门槛，仅生成待复核建议，不自动应用。" if accepted else "候选未同时达到验证增益、保留集至少3个百分点增益和至少60%日期改善，保留当前权重。",
            "validation_precision_delta": _round(validation_delta),
            "required_holdout_precision_delta": .03, "required_positive_gain_day_ratio": .6,
        }
        return result
    except _Cancelled:
        result["status"] = "cancelled"
        result["candidate_weights"] = None
        result["holdout"] = None
        result["recommendation"] = {"accepted": False, "reason": "实验已取消，未生成或应用新权重。"}
        return result
