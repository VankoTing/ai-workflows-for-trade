r"""
问答主流程：提问 → 检索（带权限过滤 + 时间过滤）→ 拼提示词 → 本地模型生成答案。

用法：
    python src/search/ask.py "GKV 球阀的工作压力范围是多少？"
    python src/search/ask.py --role sales --owner sales_a "XX 项目的报价是多少？"
    python src/search/ask.py --role gm --show "检查阀的试验标准是什么？"

时间维度（设计见 docs/versioning.md）：
    python src/search/ask.py "这个型号多少钱？" --as-of 20250101   # 时间旅行：2025年初的价格
    python src/search/ask.py "这个型号多少钱？" --all-versions    # 含历史版本（排查用）
    （默认只检索「当前有效」的版本，自动排除已被取代的旧版和重复副本）

三种角色能看到的密级范围不同 —— 这就是 docs/roles.md 里权限设计的落地。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import chromadb  # noqa: E402
import ollama  # noqa: E402

from src.config import CHROMA_DIR, EMBED_MODEL, LLM_MODEL, TOP_K  # noqa: E402
from src.search.hybrid import hybrid_query  # noqa: E402

COLLECTION = "knowledge"

# 角色 → 可见密级（与 docs/roles.md 的权限矩阵一致）
ROLE_SENSITIVITY = {
    "gm": ["public", "internal", "confidential", "restricted"],
    "manager": ["public", "internal", "confidential"],
    "sales": ["public", "internal"],
}


def build_time_where(as_of: int | None = None,
                     all_versions: bool = False) -> dict | None:
    """
    时间维度的过滤条件（设计见 docs/versioning.md 第六节）。

    · 默认           → 只看 status=active（当前有效版本），**默认不掉进旧版本的坑**
    · --as-of 日期   → 时间旅行：返回那个时点生效的版本
    · --all-versions → 不过滤（考古 / 排查用）
    """
    if all_versions:
        return None
    if as_of:
        return {
            "$and": [
                {"effective_from": {"$lte": as_of}},   # 那天之前已经生效
                {"status": {"$ne": "duplicate"}},      # 排除重复副本
            ]
        }
    return {"status": {"$eq": "active"}}


def _merge(a: dict | None, b: dict | None) -> dict | None:
    """合并两个过滤条件（任一为 None 则取另一个）。"""
    if a is None:
        return b
    if b is None:
        return a
    return {"$and": [a, b]}


def build_where(role: str, owner: str | None,
                as_of: int | None = None,
                all_versions: bool = False) -> dict | None:
    """
    把「角色 + 时间」翻译成向量库的过滤条件。

    关键思想：**权限过滤和时间过滤都发生在检索之前** ——
    能看到什么、哪个时点有效，才检索什么。
    如果先检索再隐藏，模型在生成时可能已经"读"到不该看的内容。
    """
    perm = None
    if role != "gm":                       # 总经理：不做密级过滤
        allowed = ROLE_SENSITIVITY.get(role, ["public"])
        if role == "sales" and owner:
            # 业务员：公共/内部全可见 + 机密里"自己负责的"那一部分
            perm = {
                "$or": [
                    {"sensitivity": {"$in": allowed}},
                    {"$and": [{"sensitivity": {"$eq": "confidential"}},
                              {"owner": {"$eq": owner}}]},
                ]
            }
        else:
            perm = {"sensitivity": {"$in": allowed}}

    return _merge(perm, build_time_where(as_of, all_versions))


PROMPT = """你是公司的技术资料助理。请**严格依据**下面提供的资料回答问题。

规则：
1. 只使用资料中的信息作答，**不要编造**任何参数、型号或结论；
2. 如果资料不足以回答，直接说「资料中未找到相关内容」，不要猜；
3. 每段资料都带**日期**。若新旧资料内容冲突，**优先采纳日期最新的**，
   并明确指出「存在新旧版本冲突，我采用了 X 版本（日期）」；
4. 回答末尾用 `[编号]` 标出你依据了哪几段资料。

--- 资料开始 ---
{context}
--- 资料结束 ---

问题：{question}
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="知识库问答")
    ap.add_argument("question", help="你的问题")
    ap.add_argument("--role", default="gm", choices=list(ROLE_SENSITIVITY), help="角色")
    ap.add_argument("--owner", default=None, help="业务员账号（role=sales 时用），如 sales_a")
    ap.add_argument("--top-k", type=int, default=TOP_K, help="检索几段")
    ap.add_argument("--show", action="store_true", help="显示检索到的原文")
    ap.add_argument("--as-of", type=int, default=None, metavar="YYYYMMDD",
                    help="时间旅行：按指定日期检索「当时有效」的版本")
    ap.add_argument("--all-versions", action="store_true",
                    help="包含已被取代的历史版本（排查用）")
    ap.add_argument("--max-per-doc", type=int, default=2, metavar="N",
                    help="同一份文件最多贡献几条切片（默认 2；0 = 不限）")
    args = ap.parse_args()

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    col = client.get_or_create_collection(name=COLLECTION)
    total = col.count()
    where = build_where(args.role, args.owner, args.as_of, args.all_versions)
    mode = ("全部版本" if args.all_versions
            else (f"截至 {args.as_of}" if args.as_of else "仅当前有效"))
    print(f"知识库共 {total} 个切片 | 角色={args.role}"
          f"{'/' + args.owner if args.owner else ''} | 时间范围={mode}")
    print(f"过滤条件={where}\n")
    if total == 0:
        print("向量库是空的，先跑：python src/ingest/build_index.py --dir ... ")
        return

    # ① 检索（混合：精确标识匹配 + 语义召回，带权限 + 时间过滤）
    #    ⚠️ 型号/单号这类**精确标识**必须走精确匹配 —— 语义检索对它不敏感，
    #       实测会把 GKV326（闸阀）的参数写成 GKV225（球阀）的。详见 src/search/hybrid.py
    docs, metas, dists = hybrid_query(
        col, args.question, where, args.top_k,
        embed_fn=lambda t: ollama.embed(model=EMBED_MODEL, input=[t])["embeddings"][0],
        max_per_doc=args.max_per_doc,
    )

    if not docs:
        print("（当前角色下没有检索到任何资料）")
        return

    # ③ 拼提示词
    ctx = "\n\n".join(
        f"[{i}] （来源：{m['source']}｜日期：{m.get('doc_date') or '未知'}"
        f"｜状态：{m.get('status', '?')}｜密级：{m['sensitivity']}）\n{d}"
        for i, (d, m) in enumerate(zip(docs, metas), 1)
    )
    if args.show:
        print("=" * 62)
        print("检索到的资料（按相关度排序）")
        print("=" * 62)
        for i, (d, m, dist) in enumerate(zip(docs, metas, dists), 1):
            print(f"\n[{i}] 距离={dist:.4f}  来源={m['source']}  密级={m['sensitivity']}")
            print("    " + d[:200].replace("\n", "\n    "))
        print()

    # ④ 生成
    print("=" * 62)
    print("回答")
    print("=" * 62)
    stream = ollama.chat(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": PROMPT.format(context=ctx, question=args.question)}],
        stream=True,
    )
    for part in stream:
        print(part["message"]["content"], end="", flush=True)
    print("\n")

    print("=" * 62)
    print("引用来源（知识血缘：哪一份、什么日期、什么状态）")
    print("=" * 62)
    for i, m in enumerate(metas, 1):
        print(f"  [{i}] {m['source']}")
        print(f"       日期 {m.get('doc_date') or '未知'}"
              f"（{m.get('doc_date_src', '?')}）"
              f"｜版本 {(m.get('version') or '-')}"
              f"｜状态 {m.get('status', '?')}"
              f"｜密级 {m['sensitivity']}")


if __name__ == "__main__":
    main()
