"""
混合检索：**精确标识匹配 + 语义召回**。

────────────────────────────────────────────────────────────
为什么需要这个模块（本项目最贵的一条经验）：

    向量检索找的是「语义相似」，对**精确标识**（型号 GKV326、询价单号 0190045）**并不敏感** ——
    因为所有阀门规格书长得都很像，一段讲 GKV225 的文本和"GKV326 的参数"在语义空间里几乎一样近。

    实测代价：问「GKV326 闸阀符合什么标准」，检索把 GKV225（球阀）的段落排在前面，
    模型据此把**闸阀写成了球阀**。这种错误在技术规格书里是致命的。
    评测集里这道题（P02）也是失分的典型。

    结论：**精确标识就该靠精确匹配**，语义检索只负责补"没写型号"的那部分召回。

用法：
    from src.search.hybrid import hybrid_query
    docs, metas, dists = hybrid_query(col, question, where, top_k, embed_fn)
────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import re
from typing import Callable

# 我方型号：GK 开头的系列号（GKV326 / GKP110DA / GKV-522A / GKV 412）
MODEL_RE = re.compile(r"GK[A-Z]{0,3}[\s\-]?\d{2,4}[A-Z]{0,3}", re.I)
# 询价单号 / 合同号：6~9 位连续数字（0190045 / 140030817 / 605010926）
NUM_RE = re.compile(r"\b\d{6,9}\b")


def _is_date_like(s: str) -> bool:
    """把 20250101 这种"长得像单号的日期"排除掉，免得误当单号去检索。"""
    if len(s) != 8 or not s.isdigit():
        return False
    y, m, d = int(s[:4]), int(s[4:6]), int(s[6:8])
    return 2015 <= y <= 2030 and 1 <= m <= 12 and 1 <= d <= 31


def extract_identifiers(question: str) -> list[str]:
    """从问题里抽出"精确标识"：型号、询价单号。去重并保持原序。"""
    out: list[str] = []
    for m in MODEL_RE.finditer(question or ""):
        out.append(m.group(0))
    for m in NUM_RE.finditer(question or ""):
        if not _is_date_like(m.group(0)):
            out.append(m.group(0))
    return list(dict.fromkeys(out))


def variants(ident: str) -> list[str]:
    """标识在资料里的常见写法：GKV326 / GKV-326 / GKV 326。"""
    m = re.match(r"([A-Za-z]+)[\s\-]*(\d+)([A-Za-z0-9]*)", ident)
    if not m:
        return [ident]
    h, n, t = m.group(1), m.group(2), m.group(3)
    return list(dict.fromkeys([f"{h}{n}{t}", f"{h}-{n}{t}", f"{h} {n}{t}"]))


def hybrid_query(
    col,
    question: str,
    where: dict | None,
    top_k: int,
    embed_fn: Callable[[str], list[float]],
    max_per_doc: int = 2,
    precise_limit: int = 8,
) -> tuple[list[str], list[dict], list[float]]:
    """
    混合检索，返回 (documents, metadatas, distances)。

    ① **精确标识匹配**：问题里带型号/单号时，优先捞正文含该标识的切片（视为最相关，距离记 0）；
    ② **语义召回**：向量检索补齐剩下的名额；
    ③ 最后按"同一份文件最多 max_per_doc 条"限流，避免一本手册霸占全部名额。
    """
    raw: list[tuple[str, dict, float]] = []
    seen: set[tuple[str, str]] = set()

    # ---------- ① 精确标识 ----------
    for ident in extract_identifiers(question):
        for v in variants(ident):
            try:
                d = col.get(where=where,
                            where_document={"$contains": v},
                            limit=precise_limit,
                            include=["documents", "metadatas"])
            except Exception:
                continue
            for doc, m in zip(d.get("documents") or [], d.get("metadatas") or []):
                key = (m.get("source", ""), doc[:60])
                if key in seen:
                    continue
                seen.add(key)
                raw.append((doc, m, 0.0))     # 精确命中 → 视为最相关

    # ---------- ② 语义召回 ----------
    if len(raw) < top_k * 2:
        qv = embed_fn(question)
        res = col.query(query_embeddings=[qv],
                        n_results=max(top_k * 3, 12),
                        where=where)
        for doc, m, dist in zip(res["documents"][0], res["metadatas"][0],
                                res["distances"][0]):
            key = (m.get("source", ""), doc[:60])
            if key in seen:
                continue
            seen.add(key)
            raw.append((doc, m, float(dist)))

    # ---------- ③ 限流 + 截断 ----------
    docs: list[str] = []
    metas: list[dict] = []
    dists: list[float] = []
    per_doc: dict[str, int] = {}
    for doc, m, dist in raw:
        s = m.get("source", "")
        if max_per_doc > 0 and per_doc.get(s, 0) >= max_per_doc:
            continue
        per_doc[s] = per_doc.get(s, 0) + 1
        docs.append(doc)
        metas.append(m)
        dists.append(dist)
        if len(docs) >= top_k:
            break

    return docs, metas, dists
