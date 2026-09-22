"""Rebuild one frozen auction ranking from the local raw batches (no network).

Use this only when the frozen sessions cannot be scored because of a documented
normalization or implementation defect that has since been fixed.  The frozen
archive is never overwritten: the rebuild is published as a new, separately
identified correction version plus a Markdown ranking summary, and the closing
label step then only adds outcome labels to the rebuilt scores.

Example:
    python tools/rebuild_daily_ranking.py --date 2026-09-21 --reason "上游阶段命名修复后按当日原批次重建评分"
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.daily_validation import DailyValidation  # noqa: E402
from app.engine import SHANGHAI  # noqa: E402
from app.storage import Store  # noqa: E402


def summarize(value):
    lines = [f"校正档案 {value['id']} · {value['date']} · {value['status']}",
             f"原因：{value['reason']}",
             f"被校正的原冻结档案：{value['corrects_frozen_id']}",
             f"本次评分引擎 SHA-256：{value['correction_source_sha256']}",
             f"原始批次：{value['batch_count']} 个"]
    for session in value["sessions"]:
        quality = session["quality"]
        lines.append("")
        lines.append(f"[{session['checkpoint']}] 来源：按当日记录权重重放 · 候选 {quality['candidate_count']} · "
                     f"可评分 {quality['scored_count']} · 未观察 {quality['unobserved_count']} · "
                     f"完整性 {quality['integrity_verified']}")
        lines.append("权重：" + json.dumps(session["weights"], ensure_ascii=False))
        ranked = [row for row in session["rows"] if row.get("score") is not None]
        if not ranked:
            lines.append("本时点仍无可评分记录。")
            continue
        lines.append("排名（最多显示前 10 名）：")
        for row in ranked[:10]:
            lines.append(f"  {row['rank']:>2}. {row['thscode']} {row['name']} 评分 {row['score']} · "
                         f"因子覆盖 {row['quality']['factor_coverage']} · 竞价涨幅 {row['auction_pct']}")
        if len(ranked) > 10:
            lines.append(f"  （另有 {len(ranked) - 10} 只可评分记录，详见 Markdown 与 JSON。）")
    lines.append("")
    lines.append(f"Markdown：{value['date']}-{value['id']}.md")
    lines.append(f"JSON：{value['date']}-{value['id']}.json")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="按当日原始批次重建一个已冻结交易日的竞价排名（不联网）")
    parser.add_argument("--date", required=True, help="已冻结的交易日 YYYY-MM-DD")
    parser.add_argument("--reason", required=True, help="4—400 字的明确校正原因，会写入档案")
    parser.add_argument("--data-dir", default=str(ROOT / "data"), help="包含 market.sqlite3 的本机 data 目录")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).resolve()
    if not (data_dir / "market.sqlite3").is_file():
        print(f"找不到 {data_dir / 'market.sqlite3'}；请确认 --data-dir 指向本机数据目录。")
        return 2
    store = Store(data_dir / "market.sqlite3")
    try:
        daily = DailyValidation(store, data_dir)
        value = daily.correct(args.date, datetime.now(SHANGHAI), args.reason)
    except ValueError as exc:
        print(f"未创建校正版本：{exc}")
        return 1
    finally:
        store.close()
    print(summarize(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
