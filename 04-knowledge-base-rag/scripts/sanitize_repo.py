"""
仓库脱敏：把代码 / 文档 / 示例数据里的客户名、业务员名、公司名替换成泛化表述。

**为什么需要它**：项目要开源到 GitHub，但代码注释、文档示例、评测集里
散落着真实客户地区、业务员姓名、公司名称 —— 必须在上传前统一处理。

**设计要点**：敏感词表**外置**在 `data/mask_terms.txt`（该目录不进仓库），
所以这个脚本本身不含任何具体名称，可以安全地放进仓库。

用法：
    python scripts/sanitize_repo.py            # 只报告（dry-run）
    python scripts/sanitize_repo.py --apply    # 真正替换（先自动备份到 data/_sanitize_backup/）
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
TERMS = PROJECT / "data" / "mask_terms.txt"
BACKUP = PROJECT / "data" / "_sanitize_backup"

SKIP_DIRS = {"data", ".git", "__pycache__", ".vscode", "node_modules", "dashboard"}
EXTS = {".py", ".md", ".html", ".css", ".js", ".json", ".jsonl", ".txt", ".yml", ".yaml"}

REPLACE = {"company": "该企业", "person": "业务员", "client": "某地区"}


def load_terms() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in TERMS.read_text(encoding="utf-8").splitlines():
        if "|" not in line:
            continue
        kind, _, word = line.partition("|")
        kind, word = kind.strip(), word.strip()
        if kind in REPLACE and word:
            pairs.append((word, REPLACE[kind]))
    # 长词优先：否则"短词"会先把"长词"的一部分替换掉，长词就再也匹配不上了
    pairs.sort(key=lambda x: -len(x[0]))
    return pairs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正替换（默认只报告）")
    args = ap.parse_args()

    if not TERMS.exists():
        print(f"找不到词表：{TERMS}")
        return
    pairs = load_terms()
    print(f"词表 {len(pairs)} 条｜模式：{'替换' if args.apply else '仅报告'}\n")

    n_file = n_hit = 0
    for f in sorted(PROJECT.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in EXTS:
            continue
        if any(s in f.parts for s in SKIP_DIRS):
            continue
        try:
            txt = f.read_text(encoding="utf-8")
        except Exception:
            continue
        hits = [(w, r) for w, r in pairs if w in txt]
        if not hits:
            continue
        n_file += 1
        n_hit += sum(txt.count(w) for w, _ in hits)
        print(f"  {str(f.relative_to(PROJECT)):<44} {len(hits):>2} 类 / "
              f"{sum(txt.count(w) for w, _ in hits):>3} 处")
        if args.apply:
            new = txt
            for w, r in pairs:
                new = new.replace(w, r)
            bp = BACKUP / f.relative_to(PROJECT)
            bp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, bp)
            f.write_text(new, encoding="utf-8")

    print()
    if n_file == 0:
        print("✅ 未发现敏感词，仓库可以安全发布")
    elif args.apply:
        print(f"✅ 已处理 {n_file} 个文件 / {n_hit} 处；备份在 {BACKUP}")
    else:
        print(f"⚠️  {n_file} 个文件含敏感词（{n_hit} 处）—— 加 --apply 执行替换")


if __name__ == "__main__":
    main()
