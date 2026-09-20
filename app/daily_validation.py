"""Immutable daily auction observations and later labels, with no network I/O."""
import copy
import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path

from . import engine
from .engine import AuctionEngine, aware, finite_number, DEFAULT_WEIGHTS
from .replay import _calendar, _date, _pool, _stamp, replay_session

CHECKPOINTS = ("09:24:50", "09:26:00")
ROW_FIELDS = ("thscode", "name", "score", "raw_score", "rank", "auction_pct", "auction_amount",
              "phase", "updated_at", "last_attempt_at", "data_status", "context_date", "continue_day_cnt")
QUALITY_FIELDS = ("status", "factor_coverage", "observation_count", "changed_observations", "window_coverage",
                  "covered_seconds", "first_observed_at", "last_receipt_age_seconds", "late_points",
                  "late_span_seconds", "upstream_freshness", "normalization_pct", "flags")


def _recorded(decision, day, checkpoint, now):
    """Verify a captured decision's receipt boundary without re-scoring it."""
    try:
        cutoff = _stamp(day + "T" + checkpoint + "+08:00")
        if not isinstance(decision, dict) or any(decision.get(key) != value for key, value in
                (("date", day), ("mode", "live"), ("checkpoint", checkpoint))):
            return None
        captured = _stamp(decision.get("captured_at"))
        if captured.date().isoformat() != day or not cutoff <= captured <= now:
            return None
        source_hash = decision.get("engine_source_sha256")
        if not isinstance(source_hash, str) or not re.fullmatch("[a-f0-9]{64}", source_hash):
            return None
        weights = AuctionEngine.validate_weights(decision.get("weights"))
        if not isinstance(decision.get("rows"), list):
            return None
        result, seen = [], set()
        for raw in decision["rows"]:
            code = raw.get("thscode") if isinstance(raw, dict) else None
            if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", code) or code in seen:
                return None
            seen.add(code)
            for key in ("updated_at", "last_attempt_at"):
                received = _stamp(raw.get(key))
                if received.date().isoformat() != day or received > cutoff:
                    return None
            for key in ("score", "raw_score"):
                if raw.get(key) is not None and (type(raw[key]) not in (int, float) or finite_number(raw[key]) is None or not 0 <= raw[key] <= 100):
                    return None
            if not isinstance(raw.get("quality"), dict) or not isinstance(raw.get("factors"), dict):
                return None
            count = raw["quality"].get("observation_count")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                return None
            coverage = raw["quality"].get("factor_coverage")
            if type(coverage) not in (int, float) or finite_number(coverage) is None or not 0 <= coverage <= 1:
                return None
            row = {key: copy.deepcopy(raw.get(key)) for key in ROW_FIELDS}
            row["name"] = str(raw.get("name") or code)[:100]
            row["quality"] = {key: copy.deepcopy(raw["quality"][key]) for key in QUALITY_FIELDS if key in raw["quality"]}
            row["factors"] = {}
            for factor in DEFAULT_WEIGHTS:
                source = raw["factors"].get(factor)
                if not isinstance(source, dict):
                    return None
                score = source.get("score")
                if score is not None and (type(score) not in (int, float) or finite_number(score) is None or not 0 <= score <= 100):
                    return None
                factor_weight = finite_number(source.get("weight"))
                if factor_weight is None or abs(factor_weight - weights[factor]) > 1e-9:
                    return None
                row["factors"][factor] = {key: copy.deepcopy(source.get(key)) for key in
                                            ("label", "value", "score", "weight", "available", "contribution")}
            row["label"] = None
            result.append(row)
        return {"rows": result, "weights": weights, "captured_at": captured.isoformat(),
                "engine_source_sha256": source_hash, "sha256": _digest(decision)}
    except (ValueError, TypeError, AttributeError):
        return None


def _use_recorded(session, decision):
    available = {row["thscode"]: row for row in decision["rows"]}
    rows = []
    for candidate in session["rows"]:
        row = available.get(candidate["thscode"])
        if row is None:
            row = {"thscode": candidate["thscode"], "name": candidate["name"], "score": None, "raw_score": None,
                   "rank": None, "label": None, "missing_reason": "当时保存的排名中没有该昨日候选",
                   "factors": {key: {"score": None, "value": None, "weight": session["weights"][key], "available": False,
                                      "contribution": None} for key in DEFAULT_WEIGHTS},
                   "quality": {"status": "unobserved", "observation_count": 0, "factor_coverage": 0,
                               "flags": ["当时保存的排名中没有该昨日候选；不以事后重放补齐"]}}
        rows.append(row)
    rows.sort(key=lambda row: (row["score"] is None, -(row["score"] or 0),
                              -row["quality"].get("factor_coverage", 0), row["thscode"]))
    session["rows"] = rows
    quality = session["quality"]
    quality["observed_count"] = sum(row["quality"].get("observation_count", 0) > 0 for row in rows)
    quality["scored_count"] = sum(row["score"] is not None for row in rows)
    quality["unobserved_count"] = len(rows) - quality["observed_count"]


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _cancel(should_stop):
    if should_stop and should_stop():
        raise ValueError("每日竞价核验已取消，已有不可变档案保留")


def _write_once(path, content):
    """Publish a complete immutable file; another writer cannot overwrite it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".daily-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)


def _cell(value):
    return ("—" if value is None else str(value)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|").replace("\n", " ")


def render_daily(record):
    lines = ["# 每日竞价评分与收盘核验", "", f"日期：{record['date']}；状态：{record['status']}", "",
             record["definition"], "", f"竞价存档：{record['frozen_id']}",
             f"冻结时本机引擎源码 SHA-256：{record['engine_source_sha256']}", ""]
    for session in record["sessions"]:
        method = "原始评分记录" if session.get("method") == "recorded_ranking" else "按原权重重放"
        lines += [f"## {session['checkpoint']}", "", f"评分来源：{method}",
                  f"该评分对应引擎 SHA-256：{session.get('engine_source_sha256', record['engine_source_sha256'])}", "", "权重及来源：", "```json",
                  json.dumps({"weights": session["weights"], "provenance": session["strategy_provenance"]}, ensure_ascii=False, indent=2),
                  "```", "", "| 股票代码 | 名称 | 冻结分数 | 当日涨停池成员 |", "|---|---|---:|---|"]
        for row in session["rows"]:
            label = "未知" if row.get("label") is None else "是" if row["label"] else "否"
            lines.append("| " + " | ".join(_cell(v) for v in (row["thscode"], row.get("name"), row.get("score"), label)) + " |")
        lines += ["", "评价：`" + json.dumps(session.get("evaluation", {}), ensure_ascii=False) + "`", ""]
    lines += ["## 数据边界", "", *["- " + _cell(value) for value in record["warnings"]], "",
              "完整 JSON 保留每股七项因子、缺失与原权重；原始批次留在本机 SQLite，可用批次 SHA-256 对照。", ""]
    return "\n".join(lines)


class DailyValidation:
    def __init__(self, store, data_dir):
        self.store, self.root = store, Path(data_dir) / "research" / "daily"
        self.lock = threading.RLock()

    def _read(self, path):
        if not path.exists():
            return None
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("每日核验档案超过读取上限")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("mode") != "live":
            raise ValueError("每日核验档案格式或模式无效")
        return value

    def freeze(self, date, now, should_stop=None):
        day, current = _date(date), aware(now)
        if current < _stamp(day + "T09:27:00+08:00"):
            raise ValueError("竞价结束后的 09:27 起才能冻结每日档案")
        path = self.root / (day + "-auction.json")
        with self.lock:
            _cancel(should_stop)
            existing = self._read(path)
            if existing:
                return existing
            manifest, batches = self.store.manifest(day), self.store.batches(day)
            unavailable = {"date": day, "mode": "live", "status": "unavailable", "sessions": [],
                           "warnings": ["缺少当时固定清单、实盘批次或原权重，未创建竞价档案。"]}
            if not manifest or not batches:
                return unavailable
            sessions, weight_records = [], []
            source_hash = hashlib.sha256(Path(engine.__file__).read_bytes()).hexdigest()
            for index, entry in enumerate(batches):
                _cancel(should_stop)
                try:
                    received, stage, payload = entry
                    stamp = _stamp(received)
                    weights = AuctionEngine.validate_weights(payload.get("_strategy_weights"))
                    if stamp.date().isoformat() == day and stamp <= current:
                        weight_records.append((stamp, index, weights))
                except (ValueError, TypeError, AttributeError):
                    continue
            weight_records.sort(key=lambda row: (row[0], row[1]))
            for checkpoint in CHECKPOINTS:
                _cancel(should_stop)
                records = [record for record in weight_records if record[0] <= _stamp(day + "T" + checkpoint + "+08:00")]
                stored_decision = self.store.decision(day, checkpoint) if hasattr(self.store, "decision") else None
                decision = _recorded(stored_decision, day, checkpoint, current)
                try:
                    weights = decision["weights"] if decision else records[-1][2] if records else AuctionEngine.validate_weights(manifest.get("weights"))
                    session = replay_session(manifest, batches, None, weights, checkpoint=checkpoint, should_stop=should_stop)
                except (ValueError, TypeError):
                    _cancel(should_stop)
                    return unavailable
                _cancel(should_stop)
                changes = sum(records[index][2] != records[index - 1][2] for index in range(1, len(records)))
                session["method"] = "recorded_ranking" if decision else "replayed"
                session["engine_source_sha256"] = decision["engine_source_sha256"] if decision else source_hash
                session["strategy_provenance"] = {"weights_source": "decision" if decision else "batch" if records else "legacy_manifest_fallback",
                    "weights_at": decision["captured_at"] if decision else records[-1][0].isoformat() if records else manifest.get("prepared_at"),
                    "weight_changes": changes, "weight_change_scope": "recorded_batches_through_checkpoint",
                    "decision_differs_from_last_batch_weights": decision["weights"] != records[-1][2] if decision and records else None,
                    "legacy_fallback": not bool(decision or records)}
                if decision:
                    session["recorded_captured_at"] = decision["captured_at"]
                    session["decision_sha256"] = decision["sha256"]
                    _use_recorded(session, decision)
                elif stored_decision is not None:
                    session["warnings"].append("已存排名的日期、观察截止或字段校验失败，此时点仅保留明确标注的重放结果。")
                sessions.append(session)
            value = {"schema_version": 1, "kind": "auction_snapshot", "date": day, "mode": "live", "status": "frozen",
                     "frozen_at": current.isoformat(), "scope": "previous_limit_up_only", "sessions": sessions,
                     "engine_source_sha256": source_hash,
                     "manifest_sha256": _digest(manifest), "batches_sha256": _digest(batches), "batch_count": len(batches),
                     "definition": "仅昨日涨停池候选；09:24:50 为截止时点评分，09:26:00 是事后终态核验。每时点分别标记原始评分记录或按原权重重放，二者不能混称。",
                     "warnings": ["竞价原始批次保留在 SQLite，此档案保存逐股因子和冻结评分；算法改变不会覆盖既有日档。",
                                  "原始评分使用该时点实际权重，重放取截止前最后一批合法权重；变化次数只覆盖已记录批次，不代表整日单一策略。"]}
            if any(session["strategy_provenance"]["legacy_fallback"] for session in sessions):
                value["warnings"].append("部分时点没有批次原权重，已回退当时清单权重；这是旧记录的受限复原。")
            if any(session["method"] == "replayed" for session in sessions):
                value["warnings"].append("部分时点没有合格原始排名，保存的是所标识引擎的重放评分，不宣称是当时已发布的原评分。")
            value["frozen_id"] = _digest(value)[:24]
            _cancel(should_stop)
            _write_once(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
            return self._read(path)

    def label(self, report, now, should_stop=None):
        if not isinstance(report, dict) or report.get("mode") != "live":
            raise ValueError("每日收盘核验只接受明确的真实报告")
        day, current = _date(report.get("date")), aware(now)
        if current < _stamp(day + "T15:10:00+08:00"):
            raise ValueError("每日收盘核验需要等到目标日 15:10")
        with self.lock:
            frozen = self.freeze(day, current, should_stop)
            if frozen["status"] == "unavailable":
                return frozen
            _cancel(should_stop)
            raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
            warnings, members, outcome = [], None, None
            try:
                calendar = _calendar(raw.get("calendar"))
                generated = _stamp(report.get("generated_at"))
                if day not in calendar or generated > current:
                    raise ValueError("报告日期或生成时间未核验")
                outcome = {"date": day, "complete": True, "retrieved_at": generated.isoformat(),
                           "rows": raw.get("pools_by_date", {}).get(day)}
                members, warning = _pool(outcome, day)
                if warning:
                    warnings.append(warning)
            except (ValueError, TypeError, AttributeError):
                warnings.append("报告的交易日历、生成时间或当日完整池不合格")
            value = copy.deepcopy(frozen)
            value.update(kind="daily_validation", status="ready" if members is not None else "partial",
                         matched_at=current.isoformat(), report_generated_at=report.get("generated_at"), outcome_verified=members is not None)
            value["warnings"] += warnings
            if members is None:
                value["warnings"].append("结果标签尚未核验，保留未知；不会把缺池当作未涨停。")
            for session in value["sessions"]:
                _cancel(should_stop)
                for row in session["rows"]:
                    row["label"] = row["thscode"] in members if members is not None else None
                quality = session["quality"]
                quality["outcome_verified"] = members is not None
                quality["eligible_for_optimization"] = bool(members is not None and session["mode"] == "live" and session["point_in_time"]
                    and quality["context_verified"] and not quality["cancelled"] and not quality["ignored_batches"]["invalid"]
                    and not quality.get("engine_rejected_records") and not quality.get("engine_out_of_order_responses"))
                session["status"] = "ready" if quality["eligible_for_optimization"] and quality["scored_count"] == quality["candidate_count"] else "partial"
                session["warnings"] = [text for text in session["warnings"] if "本会话结果标签全部为空" not in text]
                session["warnings"] += warnings
                ranked = [row for row in session["rows"] if row.get("score") is not None and isinstance(row["label"], bool)]
                top = ranked[:10]
                session["evaluation"] = {"top_k": 10, "selected_count": len(top), "hits": sum(row["label"] for row in top),
                    "precision_pct": round(100 * sum(row["label"] for row in top) / len(top), 2) if top else None,
                    "scored_labeled_count": len(ranked), "candidate_count": len(session["rows"])}
            # The result identity depends on evidence, not on the retry time.
            value["outcome_sha256"] = _digest({"date": day, "generated_at": report.get("generated_at"),
                                               "members": sorted(members) if members is not None else None, "warnings": warnings})
            value["id"] = _digest({"frozen_id": frozen["frozen_id"], "outcome": value["outcome_sha256"]})[:24]
            path = self.root / (day + "-" + value["id"] + ".json")
            _cancel(should_stop)
            _write_once(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
            saved = self._read(path)
            _write_once(path.with_suffix(".md"), render_daily(saved))
            return saved

    def get(self, date):
        day = _date(date)
        with self.lock:
            versions = [self._read(path) for path in self.root.glob(day + "-*.json")
                        if re.fullmatch(re.escape(day) + "-[a-f0-9]{24}\\.json", path.name)]
            if versions:
                return max(versions, key=lambda value: (value.get("matched_at", ""), value["id"]))
            frozen = self._read(self.root / (day + "-auction.json"))
            if frozen:
                return frozen
        return {"date": day, "mode": "live", "status": "unavailable", "sessions": [],
                "warnings": ["该日期尚无每日竞价存档；不会用今天的数据补写历史。"]}

    def list(self):
        result = []
        for path in sorted(self.root.glob("????-??-??-auction.json"), reverse=True)[:365]:
            value = self.get(path.name[:10])
            result.append({key: value.get(key) for key in ("date", "status", "frozen_id", "id", "frozen_at", "matched_at", "outcome_verified")})
        return result

    def export(self, date, format="json"):
        if format not in ("json", "markdown"):
            raise ValueError("每日档案只支持 json 或 markdown 导出")
        value = self.get(date)
        if value["status"] == "unavailable":
            raise ValueError("该日期尚无每日竞价存档可导出")
        name = value["date"] + "-" + value.get("id", value["frozen_id"])
        return (name + ".json", value) if format == "json" else (name + ".md", render_daily(value))
