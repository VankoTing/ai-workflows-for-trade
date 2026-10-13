"""
给已入库的切片回填「版本与时效」元数据。

关键：**不重跑 OCR、不重算向量** —— 只更新 metadata 字段。
因为时间信息可以从文件路径直接推断，所以 4 万切片几秒即可完成。
设计见 docs/versioning.md。

用法：
    python scripts/backfill_time_meta.py --dry-run    # 先看会怎么标
    python scripts/backfill_time_meta.py              # 真正写回
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

import chromadb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import CHROMA_DIR, DATA_ROOT  # noqa: E402
from src.ingest.versioning import (  # noqa: E402
    assign_status,
    days_ago,
    fingerprint,
    version_meta,
)

BATCH = 4000


def read_all(col) -> tuple[list[str], list[dict]]:
    """分页读出全部 id 与 metadata（一次性 get 会撞 SQL 变量上限，见 lessons L18）。"""
    total = col.count()
    ids: list[str] = []
    metas: list[dict] = []
    off = 0
    while off < total:
        d = col.get(include=["metadatas"], limit=BATCH, offset=off)
        got = d["ids"]
        if not got:
            break
        ids += got
        metas += d["metadatas"]
        off += len(got)
    return ids, metas


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写入")
    args = ap.parse_args()

    root = Path(DATA_ROOT)
    col = chromadb.PersistentClient(path=str(CHROMA_DIR)).get_collection("knowledge")

    t0 = time.time()
    ids, metas = read_all(col)
    print(f"读出 {len(ids)} 个切片（{time.time()-t0:.1f}s）")

    # ---------- 文件层面：每个 source 只推断一次 ----------
    uniq = sorted({m.get("source", "") for m in metas})
    print(f"涉及 {len(uniq)} 个来源文件，开始推断版本/时效…")

    per_file: dict[str, dict] = {}
    missing = 0
    for s in uniq:
        p = root / s
        if p.exists():
            meta = version_meta(p, root)
            # 内容指纹：用于区分「同一份文件的副本」（重复）与「不同版本」（取代）
            meta["doc_fp"] = fingerprint(p)
            per_file[s] = meta
        else:
            missing += 1
            per_file[s] = {
                "doc_family": Path(s).stem.lower(),
                "version": "",
                "version_rank": 0,
                "doc_date": 0,
                "doc_date_src": "missing",
                "effective_from": 0,
                "effective_to": 0,
                "doc_fp": "",
            }

    # ---------- 集合层面：判定谁是 current ----------
    items = [dict(v, source=k) for k, v in per_file.items()]
    assign_status(items)
    by_src = {it["source"]: it for it in items}

    src_cnt = Counter(it["doc_date_src"] for it in items)
    st_cnt = Counter(it["status"] for it in items)
    dates = [it["doc_date"] for it in items if it["doc_date"]]
    fams = Counter(it["doc_family"] for it in items)
    multi = {k: v for k, v in fams.items() if v > 1}

    print()
    print("=" * 62)
    print(f"文件数        : {len(items)}")
    print(f"日期来源分布  : {dict(src_cnt)}")
    print(f"状态分布      : {dict(st_cnt)}")
    print(f"最多/最新日期 : {min(dates) if dates else '-'} ~ {max(dates) if dates else '-'}")
    print(f"多版本家族数  : {len(multi)}（共含 {sum(multi.values())} 个文件）")
    print(f"文件缺失      : {missing}")
    print("=" * 62)

    if args.dry_run:
        print("\n[dry-run] 未写入。")
        return

    # ---------- 切片层面：合并元数据并写回 ----------
    print("\n开始写回…")
    n_ok = 0
    for i in range(0, len(ids), 2000):
        b_ids = ids[i:i + 2000]
        b_meta = []
        for j, _ in enumerate(b_ids):
            old = dict(metas[i + j])
            v = by_src.get(old.get("source", ""), {})
            old.update({
                "doc_family": v.get("doc_family", ""),
                "version": v.get("version", ""),
                "version_rank": v.get("version_rank", 0),
                "doc_date": v.get("doc_date", 0),
                "doc_date_src": v.get("doc_date_src", ""),
                "effective_from": v.get("effective_from", 0),
                "effective_to": v.get("effective_to", 0),
                "status": v.get("status", "active"),
                "doc_fp": v.get("doc_fp", ""),
                "dup_of": v.get("dup_of", ""),
            })
            b_meta.append(old)
        col.update(ids=b_ids, metadatas=b_meta)
        n_ok += len(b_ids)
        print(f"  已写回 {n_ok}/{len(ids)}", end="\r")

    print(f"\n完成：{n_ok} 个切片已带上版本/时效元数据，耗时 {time.time()-t0:.1f}s")

    # 抽样验证
    d = col.get(limit=3, include=["metadatas"])
    print("\n抽样：")
    for m in d["metadatas"]:
        print(f"  {m.get('source','')[:46]:<48} {m.get('doc_date')} "
              f"[{m.get('doc_date_src')}] v={m.get('version') or '-'} "
              f"{m.get('status')}")
    # 顺带算一下最老的有效文档
    active = [it for it in items if it["status"] == "active" and it["doc_date"]]
    if active:
        oldest = min(active, key=lambda x: x["doc_date"])
        print(f"\n最老的 active 文档：{oldest['source'][:52]} "
              f"（{oldest['doc_date']}，已 {days_ago(oldest['doc_date'])} 天）")


if __name__ == "__main__":
    main()
