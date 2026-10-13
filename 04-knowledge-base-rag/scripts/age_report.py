"""
质量衰减自动诊断（设计见 docs/versioning.md 第七节）。

输出四类问题：
  ① 过期预警   active 但已很老（> 365 天）
  ② 版本冲突   同一家族存在多个 active 且内容不同  ← 最高优先级，需业务裁决
  ③ 重复冗余   内容指纹相同的副本散落多处
  ④ 更新缺口   关键类型（价格表/目录/规格书）最新版本太久没更新

用法：
    python scripts/age_report.py            # 控制台报告 + 写 data/age_report.json
    python scripts/age_report.py --top 20   # 每类多列几条
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import chromadb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import CHROMA_DIR  # noqa: E402
from src.ingest.versioning import days_ago, load_decisions  # noqa: E402

EXPIRED_DAYS = 365      # 超过这个天数且仍 active → 过期预警
STALE_DAYS = 180        # 关键类型最新版本超过这个天数 → 更新缺口

KEY_TYPES = {
    "价格表 / 报价": re.compile(r"price|价格|报价|quotation|\boffer\b", re.I),
    "产品目录 / 样本": re.compile(r"catalog|目录|样本|brochure", re.I),
    "技术规格书": re.compile(r"spec|规格|datasheet", re.I),
    "证书 / 资质": re.compile(r"certificat|证书|资质|qualification", re.I),
}


def load_files(col) -> dict[str, dict]:
    """按 source 去重，拿到文件层面的元数据（分页读，见 lessons L18）。"""
    total = col.count()
    seen: dict[str, dict] = {}
    off = 0
    while off < total:
        d = col.get(include=["metadatas"], limit=4000, offset=off)
        got = d["ids"]
        if not got:
            break
        for m in d["metadatas"]:
            seen[m.get("source", "")] = m
        off += len(got)
    return seen


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=12, help="每类列出前 N 条")
    args = ap.parse_args()

    col = chromadb.PersistentClient(path=str(CHROMA_DIR)).get_collection("knowledge")
    files = load_files(col)
    today = int(datetime.now().strftime("%Y%m%d"))
    print(f"扫描 {len(files)} 个文件（向量库切片 {col.count()} 个）\n")

    dated = [m for m in files.values() if m.get("doc_date")]
    st_cnt = Counter(m.get("status", "?") for m in files.values())
    src_cnt = Counter(m.get("doc_date_src", "?") for m in files.values())

    # ---------- ① 过期预警 ----------
    expired = []
    for m in dated:
        if m.get("status") == "active":
            age = days_ago(m["doc_date"], today)
            if age > EXPIRED_DAYS:
                expired.append({
                    "source": m.get("source"),
                    "doc_date": m["doc_date"],
                    "age_days": age,
                    "doc_date_src": m.get("doc_date_src"),
                })
    expired.sort(key=lambda x: -x["age_days"])

    # ---------- ② 版本冲突 ----------
    # 已被人工裁定为「都算有效」的家族不再算冲突 —— 人已经拍板，程序不该再啰嗦。
    decisions = load_decisions()
    keep_all_fams = {f for f, d in decisions.items()
                     if d.get("action") == "keep_all" or d.get("keep") == "__all__"}

    fams: dict[str, list[dict]] = defaultdict(list)
    for m in dated:
        fams[m.get("doc_family", "")].append(m)
    conflicts = []
    for fam, arr in fams.items():
        if fam in keep_all_fams:
            continue
        live = [m for m in arr if m.get("status") == "active"]
        if len(live) > 1:
            conflicts.append({
                "family": fam,
                "count": len(live),
                "members": sorted(
                    [{"source": m.get("source"), "doc_date": m["doc_date"],
                      "src": m.get("doc_date_src")} for m in live],
                    key=lambda x: -x["doc_date"],
                ),
            })
    conflicts.sort(key=lambda x: -x["count"])

    # ---------- ③ 重复冗余 ----------
    dups: dict[str, dict] = defaultdict(lambda: {"keep": "", "dups": []})
    for m in dated:
        if m.get("status") == "duplicate":
            key = m.get("dup_of") or m.get("doc_family") or "?"
            dups[key]["keep"] = key
            dups[key]["dups"].append(m.get("source"))
    dup_list = [{"keep": v["keep"], "count": len(v["dups"]), "dups": v["dups"]}
                for v in dups.values()]
    dup_list.sort(key=lambda x: -x["count"])

    # ---------- ④ 更新缺口 ----------
    stale = []
    for label, pat in KEY_TYPES.items():
        hit = [m for m in dated
               if pat.search(m.get("source", "")) and m.get("status") == "active"]
        if not hit:
            stale.append({"type": label, "latest": 0, "age_days": -1, "n": 0})
            continue
        newest = max(hit, key=lambda x: x["doc_date"])
        age = days_ago(newest["doc_date"], today)
        stale.append({
            "type": label,
            "latest": newest["doc_date"],
            "age_days": age,
            "n": len(hit),
            "sample": newest.get("source"),
            "stale": age > STALE_DAYS,
        })
    stale.sort(key=lambda x: -x["age_days"])

    # ---------- 健康分 ----------
    active = [m for m in dated if m.get("status") == "active"]
    fresh_ratio = 1 - (len(expired) / len(active) if active else 0)
    multi_fams = [f for f, a in fams.items() if len(a) > 1]
    conflict_ratio = (len(conflicts) / len(multi_fams)) if multi_fams else 0.0

    # 洁净度：衡量"重复是否已被治理"，而不是"库里有几份重复文件"。
    # 判据 —— 每组内容相同的文件里，最多只能有 1 份 active（即检索不会重复命中）。
    # 用"重复文件有多少"扣分会误导：那衡量的是"资料库有多乱"，不是"系统好不好用"。
    fp_groups: dict[str, list[dict]] = defaultdict(list)
    for m in dated:
        fp = m.get("doc_fp") or ""
        if fp:
            fp_groups[fp].append(m)
    dup_groups = [v for v in fp_groups.values() if len(v) > 1]
    bad_dup = [v for v in dup_groups
               if sum(1 for m in v if m.get("status") == "active") > 1]
    dup_ratio = (len(bad_dup) / len(dup_groups)) if dup_groups else 0.0

    score = round(40 * fresh_ratio + 35 * (1 - conflict_ratio) + 25 * (1 - dup_ratio))

    # ---------- 打印 ----------
    print("=" * 66)
    print(f"  时效健康分：{score} / 100")
    print("=" * 66)
    print(f"  状态分布   : {dict(st_cnt)}")
    print(f"  日期来源   : {dict(src_cnt)}")
    print(f"  有日期的文件: {len(dated)} / {len(files)}")
    if decisions:
        n1 = sum(1 for d in decisions.values()
                 if d.get("action") == "keep_one" and d.get("keep") != "__all__")
        na = len([f for f in keep_all_fams])
        print(f"  已人工裁决 : {len(decisions)} 个家族"
              f"（保留其一 {n1} / 全部保留 {na}）✅ 人的判断优先于程序")

    print(f"\n① 过期预警（active 且 > {EXPIRED_DAYS} 天）: {len(expired)} 个")
    for e in expired[:args.top]:
        print(f"    {e['age_days']:>5} 天  {e['doc_date']} [{e['doc_date_src']:<13}] {e['source'][:52]}")
    if len(expired) > args.top:
        print(f"    … 还有 {len(expired)-args.top} 个")

    print(f"\n② 版本冲突（同族多份 active）: {len(conflicts)} 个家族")
    for c in conflicts[:args.top]:
        print(f"    ▸ {c['family'][:44]}  （{c['count']} 份 active）")
        for mem in c["members"][:4]:
            print(f"        {mem['doc_date']} [{mem['src']:<13}] {mem['source'][:56]}")
    if len(conflicts) > args.top:
        print(f"    … 还有 {len(conflicts)-args.top} 个家族")

    print(f"\n③ 重复冗余（内容相同的副本）: {len(dup_list)} 组 / {sum(d['count'] for d in dup_list)} 个副本")
    for d in dup_list[:args.top]:
        print(f"    ▸ {d['count']} 份  {d['keep'][:56]}")
        for s in d["dups"][:3]:
            print(f"        {s[:64]}")
    if len(dup_list) > args.top:
        print(f"    … 还有 {len(dup_list)-args.top} 组")

    print(f"\n④ 更新缺口（关键类型最新版本 > {STALE_DAYS} 天）")
    for s in stale:
        flag = "⚠️ " if s.get("stale") else "✅ "
        latest = s["latest"] or "无"
        age = f"{s['age_days']} 天" if s["age_days"] >= 0 else "无可判定日期"
        print(f"    {flag}{s['type']:<14} 最新 {latest}  （{age}，共 {s['n']} 份）")

    # ---------- 落盘 ----------
    out = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "health_score": score,
        "files_total": len(files),
        "chunks_total": col.count(),
        "status": dict(st_cnt),
        "date_src": dict(src_cnt),
        "expired": expired,
        "conflicts": conflicts,
        "duplicates": dup_list,
        "stale_types": stale,
    }
    out_path = Path(__file__).resolve().parents[1] / "data" / "age_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n报告已写入：{out_path}")


if __name__ == "__main__":
    main()
