"""
按型号查历史报价 —— 参考报价的核心引擎。

思路：
    客户报来 `GKV225-3"-150LB × 10` → 去价格库里找**同系列+同口径+同压力等级**的历史报价
    → 给出中位数、区间、以及依据（哪几份报价、什么日期）。

匹配分三层（越往下越"参考"，要标清楚可信度）：
    ① 精确  同系列 + 同口径 + 同压力等级     → 最可信
    ② 同口径 同系列 + 同口径（压力等级不同）  → 可参考（价格随压力等级变化）
    ③ 同系列 只有系列相同                     → 只作量级参考

用法：
    python src/quote/lookup.py "GKV225-3\"-150LB"
    python src/quote/lookup.py "GKV225-3\"-150LB" --top 5
    python src/quote/lookup.py --series GKV412 --size 25 --rating 800
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.quote.extract_prices import MODEL_RE, norm_rating, norm_size, parse_spec  # noqa: E402

BOOK = Path(__file__).resolve().parents[2] / "data" / "price_book.jsonl"


def load_book() -> list[dict]:
    if not BOOK.exists():
        print("价格库不存在，先运行：python src/quote/extract_prices.py")
        return []
    return [json.loads(line) for line in BOOK.open(encoding="utf-8") if line.strip()]


def by_currency(rows: list[dict]) -> dict[str, list[float]]:
    d: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        d[r.get("currency") or "未知"].append(r["price"])
    return d


def summarize(rows: list[dict]) -> list[str]:
    out = []
    for cur, ps in sorted(by_currency(rows).items(), key=lambda x: -len(x[1])):
        ps = sorted(ps)
        med = statistics.median(ps)
        out.append(f"{cur} {len(ps)} 条｜中位 {med:,.2f}｜区间 {ps[0]:,.2f} ~ {ps[-1]:,.2f}")
    return out


def show(rows: list[dict], top: int) -> None:
    for r in sorted(rows, key=lambda x: -x.get("file_date", 0))[:top]:
        m = (r.get("spec") or r.get("model") or "")[:36]
        print(f"      {r.get('file_date')}  {m:<38} "
              f"{r['price']:>10,.2f} {r.get('currency') or '?'}  {r['source'][:44]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", help="型号，如 GKV225-3\"-150LB")
    ap.add_argument("--series", default="", help="系列（与 size/rating 配合）")
    ap.add_argument("--size", default="", help="口径（1\" / DN25 / 25）")
    ap.add_argument("--rating", default="", help="压力等级（150LB / PN16 / 150）")
    ap.add_argument("--top", type=int, default=6, help="每层列几条明细")
    args = ap.parse_args()

    if args.query:
        mm = MODEL_RE.search(args.query)
        series = mm.group(1).upper() if mm else ""
        size, rating = parse_spec(args.query)
    else:
        series = args.series.upper()
        size = norm_size(args.size)
        rating = norm_rating(args.rating)

    print(f"查询：系列={series or '?'}  口径={size or '?'}  压力等级={rating or '?'}\n")
    rows = load_book()
    if not rows:
        return
    print(f"价格库共 {len(rows)} 条历史报价记录\n")

    if not series:
        print("没能从输入里识别出型号系列（GK 开头的编号）")
        return

    exact = [r for r in rows if r["series"] == series
             and r["size"] == size and r["rating"] == rating]
    same_size = [r for r in rows if r["series"] == series and r["size"] == size]
    same_series = [r for r in rows if r["series"] == series]

    print("=" * 76)
    print(f"① 精确匹配（同系列+同口径+同压力等级）：{len(exact)} 条")
    print("=" * 76)
    if exact:
        for line in summarize(exact):
            print("   建议参考价位 →", line)
        print("   依据（最近几条）：")
        show(exact, args.top)
    else:
        print("   无精确匹配")

    print()
    print("=" * 76)
    print(f"② 同口径（压力等级不同）：{len(same_size)} 条")
    print("=" * 76)
    if same_size:
        for line in summarize(same_size):
            print("   区间 →", line)
        show(same_size, args.top)
    else:
        print("   无")

    print()
    print("=" * 76)
    print(f"③ 同系列（口径压力都放宽，仅作量级参考）：{len(same_series)} 条")
    print("=" * 76)
    if same_series:
        for line in summarize(same_series):
            print("   区间 →", line)
    else:
        print("   无")


if __name__ == "__main__":
    main()
