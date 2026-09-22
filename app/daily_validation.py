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
FIELD_AUDIT_FIELDS = ("auction_volume_ratio", "auction_turnover_pct", "auction_yesterday_ratio_pct",
                      "auction_unmatched", "open_price")
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


def _field_coverage(batches, day, codes):
    """Per-day availability of raw upstream item keys, never inferred or filled.

    The 2026-09-21/22 sessions showed ``auction_volume_ratio`` delivered only in
    the first snapshot and the complete final response while the live window
    omitted the key entirely.  This audit keeps that observation per day from
    the raw payloads themselves, so later readers can tell an upstream gap from
    a local parsing gap without trusting any single note.
    """
    stats = {field: {"batches_with_value": 0, "first_value_at": None, "last_value_at": None}
             for field in FIELD_AUDIT_FIELDS}
    seen = 0

    def latest_at(cutoff):
        """Newest observation per candidate at or before ``cutoff`` (engine semantics)."""
        latest = {}
        for entry in batches:
            try:
                received, _stage, payload = entry
                stamp = _stamp(received)
            except (TypeError, ValueError):
                continue
            if stamp.date().isoformat() != day or stamp > cutoff:
                continue
            items = payload.get("item") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                continue
            for row in items:
                if not isinstance(row, dict):
                    continue
                code = str(row.get("thscode") or "").upper()
                if code in codes and (code not in latest or stamp >= latest[code][0]):
                    latest[code] = (stamp, row)
        return latest

    for entry in batches:
        try:
            received, _stage, payload = entry
            stamp = _stamp(received)
        except (TypeError, ValueError):
            continue
        if stamp.date().isoformat() != day:
            continue
        clock = stamp.strftime("%H:%M:%S")
        if not "09:15:00" <= clock <= "09:26:00":
            continue
        items = payload.get("item") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            continue
        seen += 1
        for field in FIELD_AUDIT_FIELDS:
            if any(isinstance(row, dict) and row.get(field) is not None for row in items):
                stats[field]["batches_with_value"] += 1
                stats[field]["first_value_at"] = stats[field]["first_value_at"] or stamp.isoformat()
                stats[field]["last_value_at"] = stamp.isoformat()
    checkpoints = {}
    for checkpoint in CHECKPOINTS:
        cutoff = _stamp(day + "T" + checkpoint + "+08:00")
        latest = latest_at(cutoff)
        per_field = {}
        for field in FIELD_AUDIT_FIELDS:
            moments = [stamp for stamp, row in latest.values()
                       if row.get(field) is not None]
            per_field[field] = {"candidate_count": len(codes), "with_value": len(moments),
                                "last_value_at": max(moments).isoformat() if moments else None}
        checkpoints[checkpoint] = per_field
    return {"definition": ("按本机原始批次统计上游 item 键的可得性；键缺失与 null 都不补造，"
                           "与官方文档字段名逐一对应，用于区分上游停发本机解析失败。"),
            "batches": seen, "fields": stats, "checkpoints": checkpoints}


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


def _write_latest(path, content):
    """Atomically refresh a mutable live view; never used for frozen evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".daily-view-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _cell(value):
    return ("—" if value is None else str(value)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|").replace("\n", " ")


def _percent(value):
    return "—" if value is None else f"{100 * finite_number(value):.0f}%"


def _quality_table(quality):
    return ["| 候选 | 有观察 | 可评分 | 未观察 | 结果已核验 | 可进入优化筛选 |",
            "|---:|---:|---:|---:|---|---|",
            "| " + " | ".join(_cell(value) for value in (
                quality.get("candidate_count"), quality.get("observed_count"), quality.get("scored_count"),
                quality.get("unobserved_count"),
                "是" if quality.get("outcome_verified") is True else "否",
                "是" if quality.get("eligible_for_optimization") is True else "否")) + " |"]


def _weight_block(session):
    return ["权重及来源：", "```json",
            json.dumps({"weights": session["weights"], "provenance": session["strategy_provenance"]},
                       ensure_ascii=False, indent=2), "```"]


def _top_line(session):
    top = [row for row in session["rows"] if row.get("score") is not None][:5]
    if not top:
        return []
    return ["前五名：" + "；".join(
        f"{_cell(row.get('rank'))}. {_cell(row.get('thscode'))} {_cell(row.get('name'))} {_cell(row.get('score'))}"
        for row in top), ""]


def _close_label(row):
    return "未知" if row.get("label") is None else "是" if row["label"] else "否"


def _phase_label(row):
    return {"final": "终态", "locked": "09:20后", "cancellable": "09:20前",
            "awaiting_final": "待终态"}.get(row.get("phase"), row.get("phase") or "未知")


def _frozen_table(session):
    lines = ["| 名次 | 股票代码 | 名称 | 竞价评分 | 因子覆盖 | 竞价涨幅% | 竞价金额（元） | 严格连板 | 观察批次 | 当日涨停池成员 |",
             "|---:|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for row in session["rows"]:
        quality = row.get("quality") if isinstance(row.get("quality"), dict) else {}
        lines.append("| " + " | ".join(_cell(value) for value in (
            row.get("rank"), row["thscode"], row.get("name"), row.get("score"),
            _percent(quality.get("factor_coverage")), row.get("auction_pct"), row.get("auction_amount"),
            row.get("continue_day_cnt"), quality.get("observation_count"), _close_label(row))) + " |")
    return lines


def _live_table(session):
    """Live ranking rows carry a provisional score and the upstream stage."""
    lines = ["| 名次 | 股票代码 | 名称 | 昨日涨停候选 | 竞价评分 | 待核验分 | 因子覆盖 | 竞价涨幅% | 竞价金额（元） | 严格连板 | 观察批次 | 数据阶段 |",
             "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for row in session["rows"]:
        quality = row.get("quality") if isinstance(row.get("quality"), dict) else {}
        lines.append("| " + " | ".join(_cell(value) for value in (
            row.get("rank"), row["thscode"], row.get("name"),
            "是" if row.get("previous_limit_up") is True else "否" if row.get("previous_limit_up") is False else "未知",
            row.get("score"), row.get("provisional_score"),
            _percent(quality.get("factor_coverage")), row.get("auction_pct"), row.get("auction_amount"),
            row.get("continue_day_cnt"), quality.get("observation_count"), _phase_label(row))) + " |")
    return lines


def render_daily(record):
    """Render one frozen, corrected or labeled daily record as a ranking summary."""
    lines = ["# 每日竞价评分与收盘核验", "", f"日期：{record['date']}；状态：{record['status']}", "",
             record["definition"], "", f"竞价存档：{record['frozen_id']}",
             f"冻结时本机引擎源码 SHA-256：{record['engine_source_sha256']}", ""]
    if record.get("kind") == "auction_snapshot_correction":
        lines += [f"校正于：{_cell(record.get('corrected_at'))}",
                  f"被校正的原冻结档案：{_cell(record.get('corrects_frozen_id'))}",
                  f"校正原因：{_cell(record.get('reason'))}",
                  f"校正后评分所用引擎 SHA-256：{_cell(record.get('correction_source_sha256'))}", ""]
    if record.get("scores_basis"):
        lines += [f"本版本评分来源：{_cell(record.get('scores_basis'))}"
                  f"{' · ' + _cell(record['scores_source_id']) if record.get('scores_source_id') else ''}", ""]
    for session in record["sessions"]:
        method = "原始评分记录" if session.get("method") == "recorded_ranking" else "按原权重重放"
        quality = session.get("quality") if isinstance(session.get("quality"), dict) else {}
        lines += [f"## {session['checkpoint']}", "", f"评分来源：{method}",
                  f"该评分对应引擎 SHA-256：{session.get('engine_source_sha256', record['engine_source_sha256'])}", "", "权重及来源：", "```json",
                  json.dumps({"weights": session["weights"], "provenance": session["strategy_provenance"]}, ensure_ascii=False, indent=2),
                  "```", ""]
        lines += _quality_table(quality) + [""]
        lines += _top_line(session)
        lines += _frozen_table(session)
        lines += ["", "评价：`" + json.dumps(session.get("evaluation", {}), ensure_ascii=False) + "`", ""]
        if session.get("warnings"):
            lines += ["评分提示：", *["- " + _cell(value) for value in session["warnings"]], ""]
    coverage = record.get("field_coverage")
    if isinstance(coverage, dict) and isinstance(coverage.get("fields"), dict) and coverage["fields"]:
        checkpoints = coverage.get("checkpoints") if isinstance(coverage.get("checkpoints"), dict) else {}
        lines += ["## 原始字段可得性", "", _cell(coverage.get("definition")), "",
                  f"统计窗口内本机原始批次：{_cell(coverage.get('batches'))} 批", "",
                  "| 原始字段 | 有值批次数 | 首次有值 | 最后有值 | 09:24:50 候选中有值 | 09:26:00 候选中有值 |",
                  "|---|---:|---|---|---:|---:|"]
        for field, stat in coverage["fields"].items():
            stat = stat if isinstance(stat, dict) else {}
            cells = [field, stat.get("batches_with_value"), stat.get("first_value_at"), stat.get("last_value_at")]
            for checkpoint in CHECKPOINTS:
                entry = ((checkpoints.get(checkpoint) or {}).get(field) or {})
                cells.append(f"{entry.get('with_value')}/{entry.get('candidate_count')}")
            lines.append("| " + " | ".join(_cell(value) for value in cells) + " |")
        lines.append("")
    lines += ["## 数据边界", "", *["- " + _cell(value) for value in record["warnings"]], "",
              "完整 JSON 保留每股七项因子、缺失与原权重；原始批次留在本机 SQLite，可用批次 SHA-256 对照。",
              "未评分或未观察的行保留为空，不补零；未核验收盘池时标签保持未知。", ""]
    return "\n".join(lines)


def render_morning_ranking(record):
    """Render the live ~09:26 ranking view. It is a view, never evidence."""
    lines = ["# 早盘竞价排名（实时计算 · 09:26 定格）", "",
             f"日期：{record['date']}；生成：{record['generated_at']}", "",
             record["definition"], "",
             "本文件由每批到达后立即计算的排名生成，09:26 分钟内按新批次刷新，之后不再更新；它是人读视图，不是不可变证据。",
             f"审计版本见同目录 {record['date']}-auction.json 与 {record['date']}-auction.md（09:27 冻结，含两个时点）。", ""]
    for session in record["sessions"]:
        quality = session.get("quality") if isinstance(session.get("quality"), dict) else {}
        lines += [f"## {session['checkpoint']} 实时排名", "",
                  f"评分来源：实时逐批计算（引擎 SHA-256 {_cell(session.get('engine_source_sha256'))}）", ""]
        lines += _weight_block(session) + [""]
        lines += _quality_table(quality) + [""]
        candidates = sum(1 for row in session["rows"] if row.get("previous_limit_up") is True)
        lines += [f"范围：本机实时采集 {len(session['rows'])} 只，其中昨日涨停候选 {candidates} 只；"
                  "不可变档案只统计昨日涨停候选。", ""]
        lines += _top_line(session)
        lines += _live_table(session)
        if session.get("warnings"):
            lines += ["", "评分提示：", *["- " + _cell(value) for value in session["warnings"]]]
        lines += [""]
    lines += ["## 数据边界", "", *["- " + _cell(value) for value in record["warnings"]], "",
              "名次只给有分数的行；同分先按因子覆盖率再按证券代码排序。未取得终态的行不取名次，只保留待核验分。",
              "收盘完整涨停池此时尚未取得，标签未知；本文件不能作为收盘命中率证据。", ""]
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
                _write_once(path.with_suffix(".md"), render_daily(existing))
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
                     "field_coverage": _field_coverage(batches, day, sorted(set(manifest.get("context") or {}))),
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
            saved = self._read(path)
            _write_once(path.with_suffix(".md"), render_daily(saved))
            return saved

    def corrections(self, day):
        """Verified correction versions for one date, oldest first."""
        found = []
        for path in self.root.glob(day + "-*.json"):
            if not re.fullmatch(re.escape(day) + r"-[a-f0-9]{24}\.json", path.name):
                continue
            value = self._read(path)
            if isinstance(value, dict) and value.get("kind") == "auction_snapshot_correction":
                found.append(value)
        return found

    def _score_basis(self, day, frozen):
        """Label the frozen sessions, or a newer rebuild when it actually scored rows.

        A correction is only preferred when it belongs to that frozen archive and
        contains at least one scored row; otherwise the original stays the basis.
        """
        candidates = [value for value in self.corrections(day)
                      if value.get("corrects_frozen_id") == frozen.get("frozen_id")
                      and any((session.get("quality") or {}).get("scored_count", 0) > 0
                              for session in value.get("sessions", []))]
        if not candidates:
            return frozen, None
        best = max(candidates, key=lambda value: (value.get("corrected_at", ""), value.get("id", "")))
        return best, best.get("id")

    def morning_ranking_path(self, date):
        return self.root / (_date(date) + "-morning-ranking.md")

    def publish_morning_ranking(self, date, now, session, checkpoint="09:26:00"):
        """Refresh the live ranking view written shortly after 09:25.

        The view is rewritten atomically, never appended or partially written,
        and the scheduler refreshes it at most every 20 seconds inside the 09:26
        minute.  It is explicitly a view: the frozen archive and its Markdown
        summary remain the only evidence for the 09:24:50 and 09:26:00
        checkpoints, this call never reads the network or the closing pool, and
        it never touches any ``*-auction.json`` file.
        """
        day, current = _date(date), aware(now)
        if not isinstance(checkpoint, str) or not re.fullmatch(r"09:2[6-9]:[0-9]{2}", checkpoint):
            raise ValueError("早盘排名时点须为 09:26:00 起的收尾时刻")
        if current < _stamp(day + "T09:26:00+08:00"):
            raise ValueError("早盘排名须在当日 09:26 起生成，不能把盘中快照写成收尾排名")
        if not isinstance(session, dict) or not isinstance(session.get("rows"), list) or not session["rows"]:
            raise ValueError("早盘排名需要实时引擎的排名行")
        rows = session["rows"]
        observed = sum(1 for row in rows if (row.get("quality") or {}).get("observation_count"))
        prepared = dict(session)
        prepared.update(checkpoint=checkpoint, method="live_ranking",
                        engine_source_sha256=session.get("engine_source_sha256") or
                        hashlib.sha256(Path(engine.__file__).read_bytes()).hexdigest(),
                        quality={"candidate_count": len(rows), "observed_count": observed,
                                 "scored_count": sum(1 for row in rows if row.get("score") is not None),
                                 "unobserved_count": len(rows) - observed,
                                 "outcome_verified": False, "eligible_for_optimization": False})
        value = {"schema_version": 1, "kind": "live_ranking_view", "date": day, "mode": "live",
                 "status": "live_snapshot", "generated_at": current.isoformat(), "checkpoint": checkpoint,
                 "definition": "实时逐批计算的本机竞价排名视图；分数是未回测的七因子研究分，不代表涨停概率。",
                 "sessions": [prepared],
                 "warnings": ["本文件是实时视图，可能被同一分钟内的新批次刷新；它不是原始评分证据。",
                              "此时收盘完整涨停池尚未取得，标签未知；未知不等于未涨停。"]}
        text = render_morning_ranking(value)
        path = self.morning_ranking_path(day)
        with self.lock:
            _write_latest(path, text)
        return value

    def correct(self, date, now, reason, should_stop=None):
        """Rebuild one frozen day's sessions from its immutable raw batches.

        The frozen archive is never rewritten.  The rebuild becomes a new,
        separately identified version that keeps the original ``frozen_id``
        link, the weights recorded in the batches and the engine source hash
        used for the rebuild, so later labeling can only add outcome labels.
        """
        day, current = _date(date), aware(now)
        if not isinstance(reason, str) or not 4 <= len(reason.strip()) <= 400:
            raise ValueError("校正每日档案需要 4—400 字的明确原因")
        reason = reason.strip()
        path = self.root / (day + "-auction.json")
        with self.lock:
            _cancel(should_stop)
            if current < _stamp(day + "T09:27:00+08:00"):
                raise ValueError("竞价结束后的 09:27 起才能校正每日档案")
            frozen = self._read(path)
            if not frozen or frozen.get("status") == "unavailable":
                raise ValueError("该日期尚无竞价冻结档案，不能凭空生成排名")
            manifest, batches = self.store.manifest(day), self.store.batches(day)
            if not manifest or not batches:
                raise ValueError("缺少当日固定清单或原始批次，无法重建排名")
            source_hash = hashlib.sha256(Path(engine.__file__).read_bytes()).hexdigest()
            records = []
            for index, entry in enumerate(batches):
                _cancel(should_stop)
                try:
                    received, stage, payload = entry
                    stamp = _stamp(received)
                    weights = AuctionEngine.validate_weights(payload.get("_strategy_weights"))
                    if stamp.date().isoformat() == day and stamp <= current:
                        records.append((stamp, index, weights))
                except (ValueError, TypeError, AttributeError):
                    continue
            records.sort(key=lambda row: (row[0], row[1]))
            original = {session.get("checkpoint"): session for session in frozen.get("sessions", [])
                        if isinstance(session, dict)}
            sessions = []
            for checkpoint in CHECKPOINTS:
                _cancel(should_stop)
                up_to = [record for record in records if record[0] <= _stamp(day + "T" + checkpoint + "+08:00")]
                weights = up_to[-1][2] if up_to else AuctionEngine.validate_weights(manifest.get("weights"))
                try:
                    session = replay_session(manifest, batches, None, weights, checkpoint=checkpoint,
                                             should_stop=should_stop)
                except (ValueError, TypeError):
                    _cancel(should_stop)
                    raise ValueError("当日清单或原始批次未通过回放校验，未生成校正版本")
                _cancel(should_stop)
                session["method"] = "replayed"
                session["engine_source_sha256"] = source_hash
                session["strategy_provenance"] = {
                    "weights_source": "batch" if up_to else "legacy_manifest_fallback",
                    "weights_at": up_to[-1][0].isoformat() if up_to else manifest.get("prepared_at"),
                    "weight_changes": sum(up_to[index][2] != up_to[index - 1][2] for index in range(1, len(up_to))),
                    "weight_change_scope": "recorded_batches_through_checkpoint",
                    "decision_differs_from_last_batch_weights": None,
                    "legacy_fallback": not bool(up_to)}
                previous = original.get(checkpoint) if isinstance(original.get(checkpoint), dict) else {}
                previous_quality = previous.get("quality") if isinstance(previous.get("quality"), dict) else {}
                session["supersedes"] = {"method": previous.get("method"),
                                         "recorded_captured_at": previous.get("recorded_captured_at"),
                                         "engine_source_sha256": previous.get("engine_source_sha256"),
                                         "candidate_count": previous_quality.get("candidate_count"),
                                         "scored_count": previous_quality.get("scored_count")}
                sessions.append(session)
            if not any((session.get("quality") or {}).get("scored_count", 0) > 0 for session in sessions):
                raise ValueError("按当日原始批次重放后仍无可评分记录，未生成校正版本")
            identity = _digest({"kind": "auction_snapshot_correction", "date": day, "mode": "live",
                                "corrects_frozen_id": frozen["frozen_id"], "engine_source_sha256": source_hash,
                                "manifest_sha256": _digest(manifest), "batches_sha256": _digest(batches),
                                "sessions": sessions})[:24]
            target = self.root / (day + "-" + identity + ".json")
            if target.exists():
                _write_once(path.with_suffix(".md"), render_daily(frozen))
                return self._read(target)
            value = {"schema_version": 1, "kind": "auction_snapshot_correction", "date": day, "mode": "live",
                     "status": "partial", "scope": "previous_limit_up_only", "id": identity, "frozen_id": identity,
                     "corrected_at": current.isoformat(), "reason": reason,
                     "corrects_frozen_id": frozen["frozen_id"], "corrects_frozen_at": frozen.get("frozen_at"),
                     "correction_source_sha256": source_hash, "engine_source_sha256": source_hash,
                     "manifest_sha256": _digest(manifest), "batches_sha256": _digest(batches),
                     "batch_count": len(batches), "sessions": sessions,
                     "field_coverage": _field_coverage(batches, day, sorted(set(manifest.get("context") or {}))),
                     "definition": frozen.get("definition") or "按当日原始批次重建的七因子评分，收盘结果只作标签。",
                     "status_reason": "竞价评分已按当日原始批次重建；收盘完整池尚未核验，标签保持未知。",
                     "warnings": ["本版本按当日原始批次与当时权重在新的引擎源码下重放，不是当时页面已发布的原分。",
                                  f"原冻结档案 {frozen['frozen_id']} 保持不变，仍保存在 {day}-auction.json 内。",
                                  "只有出现可解释的归一化或实现缺陷时才创建校正版本；校正不能改写历史分数。"]}
            _write_once(target, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
            saved = self._read(target)
            _write_once(target.with_suffix(".md"), render_daily(saved))
            _write_once(path.with_suffix(".md"), render_daily(frozen))
            return saved

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
            base, correction_id = self._score_basis(day, frozen)
            raw = report.get("raw") if isinstance(report.get("raw"), dict) else {}
            warnings, members, outcome = [], None, None
            if correction_id:
                warnings.append("该日竞价评分使用校正版本（原冻结档案全部未就绪）；标签只加到重建后的分数上，原档案未改写。")
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
            value = copy.deepcopy(base)
            value.update(kind="daily_validation", status="ready" if members is not None else "partial",
                         matched_at=current.isoformat(), report_generated_at=report.get("generated_at"), outcome_verified=members is not None)
            value["auction_frozen_id"] = frozen["frozen_id"]
            value["scores_basis"] = "correction" if correction_id else "frozen_archive"
            if correction_id:
                value["scores_source_id"] = correction_id
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
            value["id"] = _digest({"frozen_id": base["frozen_id"], "outcome": value["outcome_sha256"]})[:24]
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
                return max(versions, key=lambda value: (value.get("matched_at", ""),
                                                       value.get("id") or value.get("frozen_id") or ""))
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
