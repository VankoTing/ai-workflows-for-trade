"""
RAG 系统评测 —— 把"感觉变好了"变成"数字变好了"。

评测集在 eval/golden_set.jsonl，每题包含：
    q      问题
    keys   答案里必须出现的关键词（考"答得对不对"）
    files  应该被检索到的文件（考"找得全不全"，可留空）
    role   用哪个角色提问（考权限）

另外内置 3 项**系统底线检查**（不靠评测集，直接扫检索结果）：
    ① 版本隔离  默认检索不得返回 superseded / rejected / duplicate
    ② 权限隔离  业务员不得检索到 confidential
    ③ 时间旅行  --as-of 模式不得返回该日期之后的文档

用法：
    python scripts/run_eval.py
    python scripts/run_eval.py --top-k 8 --show-fail
    python scripts/run_eval.py --save-baseline        # 存为基线，下次自动对比
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import chromadb
import ollama

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from src.config import CHROMA_DIR, EMBED_MODEL  # noqa: E402
from src.search.ask import build_where  # noqa: E402
from src.search.hybrid import hybrid_query  # noqa: E402

GOLDEN = PROJECT / "eval" / "golden_set.jsonl"
REPORT = PROJECT / "data" / "eval_report.json"
BASELINE = PROJECT / "data" / "eval_baseline.json"

PROBE_Q = "球阀 GKV 阀门 压力等级 材质 规格"
AS_OF = 20250101


def load_cases() -> list[dict]:
    if not GOLDEN.exists():
        print(f"找不到评测集：{GOLDEN}")
        return []
    return [json.loads(line) for line in GOLDEN.open(encoding="utf-8") if line.strip()]


def embed(text: str) -> list[float]:
    return ollama.embed(model=EMBED_MODEL, input=[text])["embeddings"][0]


# ---------------------------------------------------------------- 单题评测

def run_case(col, case: dict, top_k: int) -> dict:
    # 走**生成环境的同一条检索路径**（混合检索 + 限流），否则评测数字不反映真实表现
    where = build_where(case.get("role", "gm"), None)
    docs, metas, _ = hybrid_query(col, case["q"], where, top_k, embed)
    srcs = [m.get("source", "") for m in metas]
    blob = "\n".join(docs)

    # 文件召回：期望文件里有多少出现在了检索结果中
    f_hit = f_tot = None
    if case.get("files"):
        f_tot = len(case["files"])
        f_hit = sum(1 for f in case["files"]
                    if any(f in s or s in f for s in srcs))

    # 关键词：答案关键词有没有出现在检索到的原文里
    k_hit = k_tot = None
    if case.get("keys"):
        k_tot = len(case["keys"])
        k_hit = sum(1 for k in case["keys"] if k.lower() in blob.lower())

    return {
        "id": case["id"], "cat": case.get("cat", "?"),
        "q": case["q"], "role": case.get("role", "gm"),
        "files_hit": f_hit, "files_total": f_tot,
        "keys_hit": k_hit, "keys_total": k_tot,
        "top_src": srcs[:3],
    }


# ---------------------------------------------------------------- 系统检查

def sys_checks(col) -> dict:
    out: dict[str, dict] = {}
    qv = embed(PROBE_Q)
    N = 20

    # ① 版本隔离
    ms = col.query(query_embeddings=[qv], n_results=N,
                   where=build_where("gm", None))["metadatas"][0]
    bad = [m for m in ms if m.get("status") != "active"]
    out["版本隔离"] = {"pass": not bad, "detail": f"{len(bad)}/{N} 条非 active"}

    # ② 权限隔离 —— 业务员不该看到 confidential
    ms = col.query(query_embeddings=[qv], n_results=N,
                   where=build_where("sales", "sales_a"))["metadatas"][0]
    bad = [m for m in ms if m.get("sensitivity") == "confidential"]
    out["权限隔离"] = {"pass": not bad, "detail": f"{len(bad)}/{N} 条越权"}

    # ③ 时间旅行 —— 不得返回 as_of 之后才生效的文档
    ms = col.query(query_embeddings=[qv], n_results=N,
                   where=build_where("gm", None, as_of=AS_OF))["metadatas"][0]
    bad = [m for m in ms if (m.get("doc_date") or 0) > AS_OF]
    out["时间旅行"] = {
        "pass": not bad,
        "detail": f"{len(bad)}/{N} 条超出 {AS_OF}",
    }
    return out


# ---------------------------------------------------------------- 主流程

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--show-fail", action="store_true", help="列出没达标的题")
    ap.add_argument("--save-baseline", action="store_true", help="把本次结果存为基线")
    args = ap.parse_args()

    col = chromadb.PersistentClient(path=str(CHROMA_DIR)).get_collection("knowledge")
    cases = load_cases()
    if not cases:
        return
    print(f"评测集 {len(cases)} 题 ｜ 向量库 {col.count()} 切片 ｜ top_k={args.top_k}\n")

    results = [run_case(col, c, args.top_k) for c in cases]

    fc = [r for r in results if r["files_total"]]
    kc = [r for r in results if r["keys_total"]]
    recall = (sum(r["files_hit"] / r["files_total"] for r in fc) / len(fc)) if fc else 0.0
    keyrate = (sum(1 for r in kc if r["keys_hit"] > 0) / len(kc)) if kc else 0.0

    checks = sys_checks(col)

    print("=" * 66)
    print("  系统底线检查")
    print("=" * 66)
    for k, v in checks.items():
        print(f"  {'✅' if v['pass'] else '❌'} {k:<10} {v['detail']}")

    print()
    print("=" * 66)
    print("  检索质量")
    print("=" * 66)
    print(f"  文件召回率（{len(fc)} 题）: {recall*100:5.1f}%")
    print(f"  关键词命中（{len(kc)} 题）: {keyrate*100:5.1f}%")

    by_cat: dict[str, list[dict]] = {}
    for r in results:
        by_cat.setdefault(r["cat"], []).append(r)
    print()
    print("  分类表现：")
    for cat, rs in sorted(by_cat.items()):
        kk = [r for r in rs if r["keys_total"]]
        rate = (sum(1 for r in kk if r["keys_hit"] > 0) / len(kk)) if kk else 0
        ff = [r for r in rs if r["files_total"]]
        rc = (sum(r["files_hit"] / r["files_total"] for r in ff) / len(ff)) if ff else 0
        print(f"    {cat:<9} {len(rs):>2} 题 ｜ 召回 {rc*100:5.1f}% ｜ 关键词 {rate*100:5.1f}%")

    if args.show_fail:
        print()
        print("  未达标（关键词未命中）：")
        for r in results:
            if r["keys_total"] and r["keys_hit"] == 0:
                print(f"    {r['id']} {r['q'][:34]:<36} → 实际命中 {r['top_src'][0] if r['top_src'] else '无'}")

    summary = {
        "cases": len(cases), "top_k": args.top_k,
        "recall": round(recall, 4), "keyrate": round(keyrate, 4),
        "checks": {k: v["pass"] for k, v in checks.items()},
        "by_cat": {c: len(r) for c, r in by_cat.items()},
    }

    old = None
    if BASELINE.exists():
        try:
            old = json.loads(BASELINE.read_text(encoding="utf-8")).get("summary")
        except Exception:
            pass
    if old:
        print()
        print("=" * 66)
        print("  对比基线")
        print("=" * 66)

        def d(k, pct=False):
            a, b = old.get(k, 0), summary[k]
            f = (lambda x: f"{x*100:.1f}%") if pct else str
            arrow = "→" if abs(b - a) < 0.005 else ("↑" if b > a else "↓")
            print(f"  {k:<10} {f(a)} {arrow} {f(b)}")

        d("recall", True)
        d("keyrate", True)

    REPORT.write_text(json.dumps({"summary": summary, "results": results,
                                  "checks": checks}, ensure_ascii=False, indent=1),
                      encoding="utf-8")
    print(f"\n报告已写入：{REPORT}")

    if args.save_baseline:
        BASELINE.write_text(json.dumps({"summary": summary}, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        print(f"已存为基线：{BASELINE}")


if __name__ == "__main__":
    main()
