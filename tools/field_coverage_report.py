"""Summarize raw upstream field availability across recorded auction days.

Read-only: it opens the local SQLite database, walks saved live batches and the
immutable preparation manifest, and prints how many candidates actually carried
each audited item key at the two checkpoints.  No network, no model, no writes.

Why: the 2026-09-21/22 sessions showed ``auction_volume_ratio`` delivered only in
the first snapshot and the complete final response, leaving the 09:24:50 primary
checkpoint with six of seven factors.  The official field name was verified
against the official documentation, so this is an upstream availability gap, not
a local naming bug.  Decision A keeps it missing and accumulates evidence before
changing anything, so run this report every few days instead of guessing.

Usage:
    python tools/field_coverage_report.py --days 10
    python tools/field_coverage_report.py --date 2026-09-21 --date 2026-09-22
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.daily_validation import CHECKPOINTS, FIELD_AUDIT_FIELDS, _field_coverage  # noqa: E402
from app.storage import Store  # noqa: E402

SCORING_FIELDS = ("auction_turnover_pct", "auction_volume_ratio")


def collect(database, dates):
    """Per-day audit rows read from batches and the prepared candidate manifest."""
    store = Store(database)
    try:
        rows = []
        for day in dates:
            manifest = store.manifest(day)
            batches = store.batches(day, "live")
            if not batches:
                continue
            codes = sorted(set((manifest or {}).get("context") or {}))
            source = "manifest_context"
            if not codes:
                codes = sorted({str(row.get("thscode") or "").upper()
                                for _received, _stage, payload in batches
                                for row in (payload.get("item") or [])
                                if isinstance(row, dict) and row.get("thscode")})
                source = "batches_fallback"
            rows.append({"date": day, "candidate_source": source, "candidates": len(codes),
                         "coverage": _field_coverage(batches, day, codes)})
        return rows
    finally:
        store.close()


def render(rows):
    lines = ["原始字段可得性（只读 batches + 盘前清单；不联网、不补造）", ""]
    lines.append("| 日期 | 候选（来源） | 有值批次 | 量比 09:24:50 | 量比 09:26:00 | 换手 09:24:50 | 七因子完整样本 |")
    lines.append("|---|---|---:|---:|---:|---:|---|")
    for row in rows:
        coverage = row["coverage"]
        checkpoints = coverage["checkpoints"]
        def cell(field, checkpoint):
            entry = checkpoints[checkpoint][field]
            return f"{entry['with_value']}/{entry['candidate_count']}"
        missing = [field for field in SCORING_FIELDS
                   if checkpoints[CHECKPOINTS[0]][field]["with_value"]
                   < checkpoints[CHECKPOINTS[0]][field]["candidate_count"]]
        verdict = "是" if not missing else "否（缺 " + "、".join(missing) + "）"
        lines.append("| " + " | ".join([
            row["date"], f"{row['candidates']}（{row['candidate_source']}）",
            str(coverage["batches"]), cell("auction_volume_ratio", CHECKPOINTS[0]),
            cell("auction_volume_ratio", CHECKPOINTS[1]), cell("auction_turnover_pct", CHECKPOINTS[0]),
            verdict]) + " |")
    lines.append("")
    definition = rows[0]["coverage"]["definition"] if rows else "无可用日期"
    lines.append("字段口径：" + definition)
    lines.append("审计字段：" + "、".join(FIELD_AUDIT_FIELDS))
    lines.append("注：量比缺失为上游实时阶段整键停发（官方字段名已核对，见 docs/DATA_SOURCES.md）；本报告不替代原始批次，也不改变任何档案。")
    return "\n".join(lines)


def recorded_days(database, limit):
    store = Store(database)
    try:
        return [entry["date"] for entry in store.batch_dates("live", limit=limit)]
    finally:
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="汇总各交易日原始竞价字段可得性（只读本机数据）")
    parser.add_argument("--days", type=int, default=10, help="未显式给出 --date 时，读取最近 N 个有 live 批次的交易日")
    parser.add_argument("--date", action="append", default=[], help="指定交易日 YYYY-MM-DD，可重复")
    parser.add_argument("--data-dir", default=str(ROOT / "data"), help="本机 data 目录")
    parser.add_argument("--database", default=None, help="默认 <data-dir>/market.sqlite3")
    args = parser.parse_args(argv)
    database = Path(args.database) if args.database else Path(args.data_dir) / "market.sqlite3"
    if not database.is_file():
        print(f"找不到 {database}；请确认 --data-dir/--database。")
        return 2
    dates = list(args.date) or recorded_days(database, max(1, args.days))
    rows = collect(database, dates)
    print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
