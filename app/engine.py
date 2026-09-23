"""Incremental auction observations and transparent, deliberately unbacktested factors.

Input timestamps describe response assembly, never exchange quote timestamps.
The caller serializes access and supplies yesterday's context, not today's pool.
"""
from __future__ import annotations

import copy
import math
import re
from datetime import datetime, time, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))
# The upstream stops sending auction_volume_ratio seconds into the live phase, so
# the primary 09:24:50 checkpoint carries the last value seen in the same session
# for at most this long, and every ranking row records that it was carried.
VOLUME_RATIO_CARRY_SECONDS = 600
DEFAULT_WEIGHTS = {
    "gap": .15, "amount": .15, "turnover": .10, "volume_ratio": .10,
    "late_momentum": .20, "retention": .15, "continuity": .15,
}
FACTOR_LABELS = {
    "gap": "竞价溢价", "amount": "竞价金额", "turnover": "竞价换手",
    "volume_ratio": "竞价量比", "late_momentum": "后五分钟价格斜率",
    "retention": "前五分钟峰值金额留存", "continuity": "昨日连板梯队",
}
NUMERIC_FIELDS = (
    "auction_price", "auction_pct", "auction_volume", "auction_amount",
    "auction_unmatched", "auction_turnover_pct", "auction_yesterday_ratio_pct",
    "auction_volume_ratio", "pre_close_price", "open_price", "last_price",
    "float_market_cap",
)
NONNEGATIVE_FIELDS = set(NUMERIC_FIELDS) - {"auction_pct", "auction_unmatched"}
CODE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


def finite_number(value):
    """Reject bool, missing placeholders, nonfinite numbers, and formatted strings."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("received_at/now 必须是带时区的 datetime")
    return value.astimezone(SHANGHAI)


def clamp(value, lo=0.0, hi=100.0):
    return max(lo, min(hi, value))


def auction_phase(now: datetime) -> str:
    t = aware(now).time().replace(tzinfo=None)
    if t < time(9, 15):
        return "before_open"
    if t < time(9, 20):
        return "cancellable"
    if t < time(9, 25):
        return "locked"
    return "awaiting_final"


def normalize_auction_metadata(data: dict, received_at: datetime) -> dict:
    """Reconcile documented and observed upstream auction phase names.

    This validates metadata only. It does not prove the quote's trading date;
    the scheduler must still check the exchange calendar before requesting it.
    Unknown combinations are deliberately unavailable rather than guessed ready.
    """
    now = aware(received_at)
    raw_status, raw_phase = data.get("data_status"), data.get("auction_phase")
    # The public example uses final/ready.  Real authenticated responses have
    # also used order_entry/live, no_cancel/live and matched/final.  Keep this
    # an explicit allowlist so a new or contradictory upstream state remains
    # unavailable until its timing and meaning have been checked.
    final_phase = raw_phase in ("final", "closed", "matched")
    live_phase = raw_phase in ("live", "cancellable", "locked", "firm", "order_entry", "no_cancel")
    normalized_phase = "final" if final_phase else "live" if live_phase else "unknown"
    ready = (final_phase and raw_status in ("ready", "final") and now.time() >= time(9, 25)) or (live_phase and raw_status in ("ready", "live"))
    warning = None
    if final_phase and raw_status in ("ready", "final") and now.time() < time(9, 25):
        warning = "09:25 前返回的终态状态不可用"
    elif not ready and raw_status != "not_ready":
        warning = "竞价阶段与数据状态组合未经确认，未作为就绪数据"
    return {"data_status": "ready" if ready else "not_ready", "auction_phase": normalized_phase,
            "raw_data_status": copy.deepcopy(raw_status), "raw_auction_phase": copy.deepcopy(raw_phase),
            "normalization_warning": warning}


def strict_continuity(context: dict):
    """N days/M boards is not a consecutive streak unless N == M."""
    explicit = finite_number(context.get("strict_consecutive"))
    if explicit is not None and explicit >= 0 and explicit.is_integer():
        return explicit
    label = str(context.get("continue_day_text") or "").strip()
    count = finite_number(context.get("continue_day_cnt"))
    inferred = None
    if label == "首板":
        inferred = 1
    elif match := re.fullmatch(r"([1-9]\d*)连板", label):
        inferred = int(match.group(1))
    elif match := re.fullmatch(r"([1-9]\d*)天([1-9]\d*)板", label):
        if match.group(1) == match.group(2):
            inferred = int(match.group(1))
    return float(inferred) if inferred is not None and count == inferred else None


class AuctionEngine:
    """Updates factors for touched symbols, then re-ranks the current universe.

    ``history`` contains this session's accepted observations, including unchanged
    polls, for audit/replay. An identical response replay does not add observations.
    ``rankings`` never retrospectively applies future observations to an old time.
    """

    def __init__(self, weights: dict | None = None):
        self.weights = self.validate_weights(DEFAULT_WEIGHTS if weights is None else weights)
        self.reset()

    @staticmethod
    def validate_weights(weights: dict) -> dict:
        if not isinstance(weights, dict) or set(weights) - set(DEFAULT_WEIGHTS):
            raise ValueError("权重包含未知因子")
        values = {key: finite_number(weights.get(key, 0)) for key in DEFAULT_WEIGHTS}
        if any(value is None or value < 0 for value in values.values()) or sum(values.values()) <= 0:
            raise ValueError("权重必须为非负有限数，且至少一个权重大于零")
        total = sum(values.values())
        if not math.isfinite(total):
            raise ValueError("权重总和超出有限数范围")
        return {key: value / total for key, value in values.items()}

    def reset(self):
        self.session_date = None
        self.history: dict[str, list[dict]] = {}
        self._states: dict[str, dict] = {}
        self._context: dict[str, dict] = {}
        self._last_received = None
        self._batches = 0
        self._not_ready_batches = 0
        self._rejected = 0
        self._duplicate_count = 0
        self._out_of_order = 0
        self._warnings: set[str] = set()

    def ingest(self, data: dict, received_at: datetime, context: dict | None = None) -> list[dict]:
        now = aware(received_at)
        date = now.date().isoformat()
        # A late response from a prior session must not reset today's state.
        if self.session_date and date < self.session_date:
            self._out_of_order += 1
            return self.rankings()
        if date != self.session_date:
            self.reset()
            self.session_date = date
        if self._last_received and now < self._last_received:
            self._out_of_order += 1
            return self.rankings()
        self._last_received = now
        self._batches += 1
        if not isinstance(data, dict):
            self._rejected += 1
            self._warnings.add("竞价响应格式无效")
            return self.rankings(now)
        if "data" in data:
            if data.get("code", 0) != 0 or not isinstance(data["data"], dict):
                self._rejected += 1
                self._warnings.add("竞价响应业务错误或数据载体无效")
                return self.rankings(now)
            data = data["data"]
        metadata = normalize_auction_metadata(data, now)
        status = metadata["data_status"]
        source_phase = metadata["auction_phase"]
        if metadata["normalization_warning"]:
            self._warnings.add(metadata["normalization_warning"])
        if status != "ready":
            self._not_ready_batches += 1
        items = data.get("item", [])
        if not isinstance(items, list):
            self._rejected += 1
            self._warnings.add("竞价明细不是列表")
            return self.rankings(now)
        if context is not None:
            self._accept_context(context)
        assembled = self._assembly_time(data.get("timestamp"))
        if assembled and assembled.date() != now.date():
            self._rejected += 1
            self._warnings.add("响应组装日期与本地交易会话日期不一致，已拒绝该批次")
            return self.rankings(now)
        if assembled and (assembled - now).total_seconds() > 5:
            self._rejected += 1
            self._warnings.add("响应组装时间超前本地时钟超过 5 秒，已拒绝该批次")
            return self.rankings(now)
        # Request metadata is optional, needed when not_ready has an empty item[].
        if status != "ready":
            requested = data.get("_requested_codes", [])
            if isinstance(requested, str):
                requested = requested.split(",")
            for code in requested:
                if code in self._states:
                    self._states[code]["data_status"] = status
                    self._states[code]["attempt_at"] = now
                    self._states[code]["metadata"] = metadata
        seen = set()
        for raw in items:
            if not isinstance(raw, dict):
                self._rejected += 1
                continue
            code = str(raw.get("thscode", "")).upper()
            if not CODE.fullmatch(code) or code in seen:
                self._rejected += 1
                continue
            seen.add(code)
            previous = self._states.get(code)
            if previous and previous["assembled"] and assembled and assembled < previous["assembled"]:
                self._out_of_order += 1
                continue
            phase = "final" if source_phase == "final" else auction_phase(now)
            # Do not turn a final endpoint called too early into a completed auction.
            if source_phase == "final" and now.time() < time(9, 25):
                self._rejected += 1
                self._warnings.add("09:25 前返回的终态数据未纳入评分")
                continue
            if source_phase != "final" and not time(9, 15) <= now.time() <= time(9, 25):
                self._rejected += 1
                continue
            row = {"thscode": code, "name": str(raw.get("name") or code)}
            invalid = []
            for field in NUMERIC_FIELDS:
                value = finite_number(raw.get(field))
                if value is not None and field in NONNEGATIVE_FIELDS and value < 0:
                    value = None
                if raw.get(field) is not None and value is None:
                    invalid.append(field)
                row[field] = value
            fingerprint = tuple(row[field] for field in NUMERIC_FIELDS)
            repeated_time = previous and ((assembled and assembled == previous["assembled"]) or now == previous["received"])
            if previous and self.history.get(code) and status == "ready" and repeated_time and fingerprint == previous["fingerprint"] and phase == previous["phase"]:
                self._duplicate_count += 1
                previous["attempt_at"] = now
                previous["data_status"] = status
                previous["metadata"] = metadata
                continue
            if status != "ready":
                if previous:
                    previous["data_status"] = status
                    previous["attempt_at"] = now
                    previous["metadata"] = metadata
                else:
                    self._states[code] = self._new_state(row, now, assembled, phase, status, invalid, fingerprint, metadata, previous)
                    self.history[code] = []
                continue
            state = self._new_state(row, now, assembled, phase, status, invalid, fingerprint, metadata, previous)
            if previous:
                state["changed"] = previous["changed"] + (fingerprint != previous["fingerprint"])
            else:
                state["changed"] = 1
            observations = self.history.setdefault(code, [])
            observation = {
                **row, "received_at": now.isoformat(), "phase": phase,
                "response_assembled_at": assembled.isoformat() if assembled else None,
                "upstream_quote_at": None,
                "raw_data_status": copy.deepcopy(metadata["raw_data_status"]),
                "raw_auction_phase": copy.deepcopy(metadata["raw_auction_phase"]),
            }
            observations.append(observation)
            self._states[code] = state
            self._compute_factors(code)
        return self.rankings(now)

    def _new_state(self, row, now, assembled, phase, status, invalid, fingerprint, metadata, previous=None):
        carry = (previous or {}).get("volume_ratio_last")
        return {"row": row, "received": now, "attempt_at": now, "assembled": assembled,
                "phase": phase, "data_status": status, "invalid": invalid,
                "fingerprint": fingerprint, "changed": 0, "values": {}, "scores": {},
                "normalization_pct": None, "late_span": 0, "late_points": 0,
                "covered_seconds": 0, "first_observed_at": None, "metadata": metadata,
                "volume_ratio_last": copy.deepcopy(carry) if isinstance(carry, dict) else None,
                "volume_ratio_source": None, "volume_ratio_age_seconds": None,
                "volume_ratio_carried_from": None}

    @staticmethod
    def _volume_ratio_value(state):
        """Current auction volume ratio, or a bounded carry of the last one seen.

        Only the code's own observations from this session are eligible, the age
        must stay within ``VOLUME_RATIO_CARRY_SECONDS`` and the caller always
        keeps the provenance, so a carried value can never be mistaken for a
        value the upstream actually delivered at the decision time.
        """
        current = state["row"]["auction_volume_ratio"]
        if current is not None:
            return current, "current", 0.0, None
        carry = state.get("volume_ratio_last")
        if not isinstance(carry, dict):
            return None, None, None, None
        value = finite_number(carry.get("value"))
        if value is None:
            return None, None, None, None
        try:
            at = aware(datetime.fromisoformat(str(carry.get("at"))))
        except (TypeError, ValueError):
            return None, None, None, None
        age = (state["received"] - at).total_seconds()
        if not 0 <= age <= VOLUME_RATIO_CARRY_SECONDS:
            return None, None, None, None
        return value, "carried", age, at.isoformat()

    @staticmethod
    def _assembly_time(value):
        number = finite_number(value)
        if number is None:
            return None
        try:
            return datetime.fromtimestamp(number / 1000, SHANGHAI)
        except (ValueError, OverflowError, OSError):
            return None

    def _accept_context(self, context):
        if not isinstance(context, dict):
            self._warnings.add("昨日涨停池上下文无效")
            return
        changed = set()
        for code, row in context.items():
            if not isinstance(row, dict):
                continue
            source_date = row.get("context_date") or row.get("trade_date") or row.get("date")
            if source_date:
                try:
                    parsed = datetime.fromisoformat(str(source_date)).date().isoformat()
                except ValueError:
                    self._warnings.add("昨日上下文日期无法解析，已忽略")
                    continue
                if parsed >= self.session_date:
                    self._warnings.add("同日或未来涨停池不能用于盘前因子，已忽略")
                    if self._context.pop(code, None) is not None:
                        changed.add(code)
                    continue
            else:
                self._warnings.add("部分昨日上下文未标注日期；其时点由调用方保证")
            if self._context.get(code) != row:
                self._context[code] = copy.deepcopy(row)
                changed.add(code)
        # Context may arrive after snapshots, so update only affected symbols.
        for code in changed:
            if code in self._states and self.history.get(code):
                self._compute_factors(code)

    def _compute_factors(self, code):
        state = self._states[code]
        row = state["row"]
        context = self._context.get(code, {})
        scale = 5 if context.get("is_st") is True or "ST" in row["name"].upper() else 30 if code.endswith(".BJ") else 20 if code.startswith(("300", "301", "688", "689")) else 10
        state["normalization_pct"] = scale
        values = {key: None for key in DEFAULT_WEIGHTS}
        scores = dict(values)
        values["gap"] = row["auction_pct"]
        if values["gap"] is not None and context.get("is_new") is not True:
            scores["gap"] = clamp(50 + 50 * values["gap"] / scale)
        values["amount"] = row["auction_amount"]
        if values["amount"] is not None:
            scores["amount"] = clamp(100 * math.log1p(values["amount"] / 1_000_000) / math.log(101))
        values["turnover"] = row["auction_turnover_pct"]
        if values["turnover"] is not None:
            scores["turnover"] = clamp(values["turnover"] / .6 * 100)
        if row["auction_volume_ratio"] is not None:
            state["volume_ratio_last"] = {"value": row["auction_volume_ratio"], "at": state["received"].isoformat()}
        ratio, ratio_source, ratio_age, ratio_from = self._volume_ratio_value(state)
        state["volume_ratio_source"] = ratio_source
        state["volume_ratio_age_seconds"] = ratio_age
        state["volume_ratio_carried_from"] = ratio_from
        values["volume_ratio"] = ratio
        if ratio is not None:
            scores["volume_ratio"] = clamp(100 * math.log1p(ratio) / math.log(11))
        continuity = strict_continuity(context)
        if continuity is not None:
            values["continuity"] = continuity
            scores["continuity"] = clamp(continuity / 5 * 100)

        observations = self.history[code]
        now = state["received"]
        terminal = now.replace(hour=9, minute=25, second=0, microsecond=0)
        temporal_end = min(now, terminal)
        # Equal-width buckets prevent a burst of repeated polls from dominating OLS.
        buckets = {}
        pre_amounts = []
        previous_stamp = None
        covered_seconds = 0
        first_observed_at = None
        for observation in observations:
            stamp = datetime.fromisoformat(observation["received_at"])
            if time(9, 15) <= stamp.time() <= time(9, 25):
                if first_observed_at is None:
                    first_observed_at = observation["received_at"]
                if previous_stamp:
                    covered_seconds += min(30, max(0, (stamp - previous_stamp).total_seconds()))
                previous_stamp = stamp
            if stamp.time() < time(9, 20) and observation["auction_amount"] is not None:
                pre_amounts.append(observation["auction_amount"])
            if (time(9, 20) <= stamp.time() <= time(9, 25)
                    and temporal_end - timedelta(seconds=120) <= stamp <= temporal_end
                    and observation["auction_pct"] is not None):
                bucket = int(stamp.timestamp()) // 10
                buckets[bucket] = (stamp, observation["auction_pct"])
        points = sorted(buckets.values())
        span = (points[-1][0] - points[0][0]).total_seconds() if points else 0
        state["late_span"] = span
        state["late_points"] = len(points)
        state["covered_seconds"] = covered_seconds
        state["first_observed_at"] = first_observed_at
        if len(points) >= 3 and span >= 30:
            xs = [(stamp - points[0][0]).total_seconds() / 60 for stamp, _ in points]
            ys = [value for _, value in points]
            xmean, ymean = sum(xs) / len(xs), sum(ys) / len(ys)
            denom = sum((x - xmean) ** 2 for x in xs)
            slope = sum((x - xmean) * (y - ymean) for x, y in zip(xs, ys)) / denom
            values["late_momentum"] = slope
            scores["late_momentum"] = clamp(50 + 25 * slope)
        if now.time() >= time(9, 20) and pre_amounts and max(pre_amounts) > 0 and row["auction_amount"] is not None:
            values["retention"] = row["auction_amount"] / max(pre_amounts)
            scores["retention"] = clamp(values["retention"] * 100)
        state["values"], state["scores"] = values, scores

    def rankings(self, now: datetime | None = None) -> list[dict]:
        now = aware(now) if now else self._last_received
        if now is None:
            return []
        if self._last_received and now < self._last_received:
            raise ValueError("不能把已接收的未来观察用于过去时点；回放请新建引擎按时间逐条 ingest")
        weights = self.validate_weights(self.weights)
        results = []
        for code, state in self._states.items():
            context = self._context.get(code, {})
            observations = self.history.get(code, [])
            scores = state["scores"]
            coverage = sum(weights[key] for key in weights if scores.get(key) is not None)
            raw_score = sum(weights[key] * scores[key] for key in weights if scores.get(key) is not None) / coverage if coverage else None
            age = max(0, (now - state["received"]).total_seconds())
            phase = state["phase"]
            stale = (phase != "final" and age > 30) or now.date().isoformat() != self.session_date
            eligible = state["data_status"] == "ready" and not stale and coverage >= .45 - 1e-12 and (state["row"]["auction_pct"] is not None or state["row"]["auction_amount"] is not None)
            flags = ["接口未公开行情发生时间；上游实时延迟未知"]
            if state["phase"] == "cancellable":
                flags.append("09:20 前可撤单，当前为试探性强弱")
            if state["volume_ratio_source"] == "carried" and state["values"].get("volume_ratio") is not None:
                carried_at = state["volume_ratio_carried_from"] or ""
                minutes = round((state["volume_ratio_age_seconds"] or 0) / 60, 1)
                flags.append(f"竞价量比实时阶段缺失，按有界携带使用 {carried_at} 的值（约 {minutes} 分钟前，同一会话内）")
            if coverage < .999:
                flags.append("部分因子缺失；按可用权重归一化，需结合覆盖率比较")
            if stale:
                flags.append("本地有效观察超过 30 秒或跨日，暂不参加排名")
            if state["data_status"] != "ready":
                flags.append("上游数据未就绪，暂不参加排名")
            if state["metadata"].get("normalization_warning"):
                flags.append(state["metadata"]["normalization_warning"])
            if state["invalid"]:
                flags.append("无效数值已置空：" + ", ".join(state["invalid"]))
            if state["assembled"] is None:
                flags.append("缺少有效响应组装时间")
            if context.get("is_new") is True:
                flags.append("新股涨跌幅规则不确定，溢价因子停用")
            if len(observations) >= 10 and state["changed"] <= 1:
                flags.append("连续快照数值未变，无法区分价格稳定与上游停止更新")
            if not context:
                flags.append("未取得该股昨日涨停上下文，连板因子为空")
            elif state["values"].get("continuity") is None:
                flags.append("昨日板数未能由连板文本核实；多天多板不视为严格连板")
            provisional = phase != "final" and now.time() >= time(9, 25)
            if provisional:
                flags.append("尚未取得终态，保存的观察排名仅为待核验快照")
            flags.append("溢价幅度使用板别启发式归一化，非真实涨停价判定")
            # Cached by _compute_factors: ranking costs O(symbols), not O(history).
            covered_seconds = state["covered_seconds"]
            first = state["first_observed_at"]
            row = copy.deepcopy(state["row"])
            factors = {}
            for key, weight in weights.items():
                factor_score = scores.get(key)
                factors[key] = {
                    "label": FACTOR_LABELS[key], "value": state["values"].get(key),
                    "score": round(factor_score, 4) if factor_score is not None else None,
                    "weight": weight, "available": factor_score is not None,
                    "contribution": round(factor_score * weight / coverage, 4) if factor_score is not None and coverage else None,
                    "value_source": state["volume_ratio_source"] if key == "volume_ratio" else None,
                    "value_age_seconds": round(state["volume_ratio_age_seconds"], 3)
                    if key == "volume_ratio" and state["volume_ratio_age_seconds"] is not None else None,
                    "carried_from": state["volume_ratio_carried_from"] if key == "volume_ratio" else None,
                }
            row.update({
                "rank": None, "score": round(raw_score, 4) if eligible else None,
                "raw_score": round(raw_score, 4) if raw_score is not None else None,
                "provisional_score": round(raw_score, 4) if provisional and raw_score is not None else None,
                "factors": factors, "continue_day_cnt": state["values"].get("continuity"),
                "previous_limit_up": bool(context), "context_date": context.get("context_date") or context.get("date"),
                "continue_day_text": context.get("continue_day_text"),
                "limit_up_reason": context.get("limit_up_reason"), "phase": phase,
                "updated_at": state["received"].isoformat(), "last_attempt_at": state["attempt_at"].isoformat(),
                "response_assembled_at": state["assembled"].isoformat() if state["assembled"] else None,
                "upstream_quote_at": None, "data_status": state["data_status"],
                "raw_data_status": copy.deepcopy(state["metadata"]["raw_data_status"]),
                "raw_auction_phase": copy.deepcopy(state["metadata"]["raw_auction_phase"]),
                "quality": {
                    "status": "not_ready" if state["data_status"] != "ready" else "stale" if stale else "ok" if coverage >= .999 else "partial",
                    "factor_coverage": round(coverage, 4), "observation_count": len(observations),
                    "changed_observations": state["changed"], "window_coverage": round(covered_seconds / 600, 4),
                    "covered_seconds": round(covered_seconds, 3), "first_observed_at": first,
                    "last_receipt_age_seconds": round(age, 3), "late_points": state["late_points"],
                    "late_span_seconds": round(state["late_span"], 3),
                    "upstream_freshness": "unknown", "normalization_pct": state["normalization_pct"],
                    "flags": flags,
                },
            })
            results.append(row)
        results.sort(key=lambda row: (row["score"] is None, -(row["score"] or 0), -row["quality"]["factor_coverage"], row["thscode"]))
        rank = 0
        for row in results:
            if row["score"] is not None:
                rank += 1
                row["rank"] = rank
        return results

    def summary(self) -> dict:
        rows = self.rankings()
        scored = [row for row in rows if row["score"] is not None]
        return {
            "volume_ratio_carried_count": sum(1 for state in self._states.values()
                                              if state.get("volume_ratio_source") == "carried"),
            "session_date": self.session_date, "symbol_count": len(rows), "scored_count": len(scored),
            "observation_count": sum(len(items) for items in self.history.values()),
            "batches": self._batches, "not_ready_batches": self._not_ready_batches,
            "rejected_records": self._rejected, "duplicate_responses": self._duplicate_count,
            "out_of_order_responses": self._out_of_order,
            "coverage": len(scored) / len(rows) if rows else 0,
            "phase": "final" if rows and all(row["phase"] == "final" for row in rows) else auction_phase(self._last_received) if self._last_received else "idle",
            "last_received_at": self._last_received.isoformat() if self._last_received else None,
            "warnings": sorted(self._warnings | {"评分为未回测的研究因子，不代表上涨概率；未匹配量的买卖方向与单位未经文档确认，不计分"}),
            "weights": self.validate_weights(self.weights),
        }
