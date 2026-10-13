"""
切片模块：把长文本切成一段段"语义完整的小块"。

为什么必须切片？
  大模型有「上下文窗口」上限，而且——更重要的——检索是按"块"做的。
  如果整篇文档是一块，检索时只能整篇给你，重点会被淹没；
  切得太碎，一句话被拦腰截断，语义就丢了。
  chunk_size（块大小）和 overlap（重叠）就是在「精度」和「完整性」之间找平衡。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import CHUNK_OVERLAP, CHUNK_SIZE  # noqa: E402


def normalize(text: str) -> str:
    """压缩多余空白：PDF 抽出来的文本常有大量空格和空行。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_paragraphs(text: str) -> list[str]:
    """
    先按「空行」切自然段；如果整篇没有空行（PDF 常见），
    再退化成按单行切。段落是切片的天然边界。
    """
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) <= 1:
        paras = [p.strip() for p in text.split("\n") if p.strip()]
    return paras


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    把文本切成若干块。

    规则：
      1. 段落优先——尽量不让一段话被切开；
      2. 累积段落直到接近 size，就产出一块；
      3. 超长段落（本身 > size）才硬切；
      4. 相邻块之间保留 overlap 字符，避免"跨块语义"丢失。
    """
    text = normalize(text)
    if not text:
        return []

    paras = split_paragraphs(text)
    chunks: list[str] = []
    buf = ""

    for p in paras:
        # 超长段落：先把缓冲区冲掉，再按固定步长硬切
        if len(p) > size:
            if buf:
                chunks.append(buf)
                buf = ""
            step = max(size - overlap, 1)
            for i in range(0, len(p), step):
                chunks.append(p[i : i + size])
            continue

        if len(buf) + len(p) + 1 <= size:
            buf = p if not buf else buf + "\n" + p
        else:
            chunks.append(buf)
            tail = buf[-overlap:] if overlap else ""   # 重叠部分：上一块结尾
            buf = (tail + "\n" + p) if tail else p

    if buf.strip():
        chunks.append(buf)

    return [c.strip() for c in chunks if c.strip()]


if __name__ == "__main__":
    demo = (
        "GKV 系列球阀采用两片式结构，阀体材质为 ASTM A216 WCB，"
        "工作压力范围 Class 150 至 Class 600，适用温度 -29℃ 至 425℃。\n\n"
        "该系列产品广泛应用于石油化工、电力、水处理等行业，"
        "可根据客户工况选配气动执行器与智能定位器。\n\n"
        "所有产品出厂前均按 API 598 进行压力试验，"
        "确保零泄漏交付。"
    )
    cs = chunk_text(demo, size=120, overlap=20)
    print(f"原文 {len(demo)} 字 → 切成 {len(cs)} 块\n")
    for i, c in enumerate(cs, 1):
        print(f"--- 第 {i} 块（{len(c)} 字）---")
        print(c)
        print()
