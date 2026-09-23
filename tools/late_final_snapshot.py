"""Re-read today's final auction snapshot after the fact, as a local audit aid.

The official auction snapshot has **no date parameter**, so this can only be
done for the current trading day.  The result is explicitly *not* the evidence
that was received at 09:2x: it records what the upstream reports later, next to
the differences against the immutable daily archive, so a stale first
``matched/final`` response (2026-09-23) can be compared without rewriting any
archive or feeding scoring/labels.

Usage:
    python tools/late_final_snapshot.py
    python tools/late_final_snapshot.py --date 2026-09-23
"""
import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import finance_key  # noqa: E402
from app.engine import SHANGHAI  # noqa: E402
from app.provider import HiThinkProvider  # noqa: E402

ITEM_FIELDS = ("thscode", "name", "auction_price", "auction_pct", "auction_volume", "auction_amount",
               "auction_unmatched", "auction_turnover_pct", "auction_yesterday_ratio_pct",
               "auction_volume_ratio", "pre_close_price", "open_price", "last_price", "float_market_cap")


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".late-final-", suffix=".tmp", dir=path.parent)
    try:
        with open(fd, "w", encoding="utf-8", newline="\n") as target:
            target.write(content)
            target.flush()
        if path.exists():
            path.unlink()
        Path(temporary).replace(path)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


def _archive_rows(data_dir, day):
    path = Path(data_dir) / "research" / "daily" / f"{day}-auction.json"
    if not path.is_file():
        return None, {}
    document = json.loads(path.read_text(encoding="utf-8"))
    sessions = document.get("sessions") or []
    if not sessions:
        return path, {}
    return path, {row.get("thscode"): row for row in sessions[-1].get("rows", [])}


def render_markdown(value):
    lines = ["# 集合竞价终值补录（本机审计用，非原始证据）", "",
             f"日期：{value['date']}；补录时间：{value['retrieved_at']}",
             f"上游：{value['source']}（auction_phase={value['upstream'].get('auction_phase')}，"
             f"data_status={value['upstream'].get('data_status')}）", "",
             f"代码数：{value['codes']}；与冻结档案不同的股票：**{value['comparison']['differences']}** 只", "",
             "| 股票代码 | 名称 | 冻结档案（09:26 时点） | 本次补录终值 | 差值(百分点) |",
             "|---|---|---:|---:|---:|"]
    for row in value["comparison"]["rows"][:80]:
        lines.append("| " + " | ".join(str(item) for item in (
            row["thscode"], row.get("name"), row.get("archive_pct"), row.get("late_pct"),
            row.get("delta_pp"))) + " |")
    if value["comparison"]["differences"] > 80:
        lines.append(f"| … | 另有 {value['comparison']['differences'] - 80} 只见同目录 JSON | | | |")
    lines += ["", "## 口径", "", value["definition"], ""]
    lines += [*["- " + warning for warning in value["warnings"]], ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="补录当日集合竞价终值并与冻结档案对比（只读本机档案，联网一次）")
    parser.add_argument("--date", default=datetime.now(SHANGHAI).date().isoformat(), help="仅支持当日")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    args = parser.parse_args(argv)
    today = datetime.now(SHANGHAI).date().isoformat()
    if args.date != today:
        print(f"官方集合竞价接口没有历史日期参数，只能补录当日终值（今天 {today}，请求 {args.date}）。")
        return 2
    data_dir = Path(args.data_dir)
    database = data_dir / "market.sqlite3"
    if not database.is_file():
        print(f"找不到 {database}")
        return 2
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=8)
    try:
        row = connection.execute("select payload from research_manifests where date=?", (args.date,)).fetchone()
    finally:
        connection.close()
    if not row:
        print("该日期没有盘前清单，无法确定当时的采集范围。")
        return 2
    codes = json.loads(row[0]).get("codes") or []
    if not codes:
        print("盘前清单没有代码，停止。")
        return 2
    key = finance_key()
    if not key:
        print("本机没有可用的金融凭据，停止。")
        return 2
    provider = HiThinkProvider(key)
    items, upstream = [], {}
    for offset in range(0, len(codes), 100):
        data = provider.auction(codes[offset:offset + 100], "final")
        upstream = {"timestamp": data.get("timestamp"), "auction_phase": data.get("auction_phase"),
                    "data_status": data.get("data_status")}
        for raw in (data.get("item") or []):
            items.append({field: raw.get(field) for field in ITEM_FIELDS})
    retrieved = datetime.now(SHANGHAI)
    archive_path, archive = _archive_rows(data_dir, args.date)
    differences = []
    for item in items:
        mine = archive.get(item["thscode"]) or {}
        a, b = mine.get("auction_pct"), item.get("auction_pct")
        if a is None or b is None or abs(a - b) < 1e-9:
            continue
        differences.append({"thscode": item["thscode"], "name": item.get("name"),
                            "archive_pct": a, "late_pct": b,
                            "delta_pp": round(b - a, 6),
                            "archive_amount": mine.get("auction_amount"), "late_amount": item.get("auction_amount")})
    differences.sort(key=lambda entry: abs(entry["delta_pp"]), reverse=True)
    value = {"schema_version": 1, "kind": "late_final_snapshot", "date": args.date,
             "retrieved_at": retrieved.isoformat(), "mode": "live",
             "source": "HiThink /api/a-share/auction/snapshot?stage=final（本机事后补录，不是 09:2x 当时的接收证据）",
             "upstream": upstream, "codes": len(codes), "items": items,
             "comparison": {"frozen_archive": archive_path.as_posix() if archive_path else None,
                            "differences": len(differences), "rows": differences},
             "definition": ("在当日收盘前的任意时刻重取官方集合竞价终态，并与不可变日档的 09:26:00 时点逐只对比；"
                            "只作人工核对，不参与评分、排名、收盘标签或优化样本。"),
             "warnings": ["官方接口没有历史日期参数，本文件只能补录当日终值，不能贴到其他日期。",
                          "本文件不是当时接收到的原始批次，不能替代冻结档案或作为评分证据。",
                          "冻结档案按不可变规则保持原样，如需修正必须走 docs/BACKTEST.md 的校正流程。"]}
    stamp = retrieved.strftime("%Y%m%d-%H%M%S")
    target_dir = data_dir / "research" / "late-final"
    json_path = target_dir / f"{args.date}-{stamp}.json"
    md_path = target_dir / f"{args.date}-{stamp}.md"
    _write(json_path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    _write(md_path, render_markdown(value))
    print(f"补录完成：{len(items)} 只；与冻结档案不同 {len(differences)} 只")
    for entry in differences[:10]:
        print(f"   {entry['thscode']} {entry.get('name')}: 档案 {entry['archive_pct']} -> 补录 {entry['late_pct']}")
    print("JSON:", json_path)
    print("Markdown:", md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
