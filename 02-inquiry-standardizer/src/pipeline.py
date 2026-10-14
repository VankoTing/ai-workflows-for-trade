"""主流程：读询价资料 -> AI 抽成标准字段 -> 导出中英对照询价表。

用法：
    # 正常流程（需要 DEEPSEEK_API_KEY）
    python src/pipeline.py data/input/询价.xlsx
    python src/pipeline.py data/input/*.xlsx -o output/691-2610-26_Inquiry_Bilingual.xlsx

    # 离线导出：用之前跑出来的抽取结果 JSON 重新出表（不需要 Key，便于调格式）
    python src/pipeline.py --from-json output/xxx_extracted.json
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import readers  # noqa: E402
from exporter import export_inquiry  # noqa: E402
from extractor import Extractor  # noqa: E402
from terms import TermBase  # noqa: E402

OUTPUT_DIR = PROJECT / "output"


def _default_out(stem: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    safe = stem or "Inquiry"
    return OUTPUT_DIR / f"{safe}_Inquiry_Bilingual_{stamp}.xlsx"


async def run(inputs: list[Path], out_path: Path | None, hint: str = "") -> Path | None:
    docs = []
    for path in inputs:
        doc = readers.read_document(path)
        docs.append(doc)
        print(f"  读取 {path.name}  ->  {doc.kind}")
        for w in doc.warnings:
            print(f"    ! {w}")

    if not any(d.to_text().strip() for d in docs):
        print("! 所有输入文件都没有读出内容，无法继续。")
        return None

    text = "\n\n".join(d.to_text() for d in docs)

    extractor = Extractor()
    if not extractor.enabled:
        print("! 未配置 DEEPSEEK_API_KEY。请在项目根目录的 .env 里填写后重试，")
        print("  或使用 --from-json 走离线导出。")
        return None

    print(f"正在调用 {extractor.model} 抽取标准字段…")
    result = await extractor.extract(text, hint)
    if not result.ok:
        print(f"! {result.error}")
        return None

    table = result.table
    print(f"抽取完成：{len(table.rows)} 行阀门，{len(table.notes)} 条审查备注")
    if table.ref:
        print(f"  项目编号：{table.ref}    国家/地区：{table.country or '—'}")

    if result.term_pairs:
        tb = TermBase()
        added = tb.merge(result.term_pairs)
        if added:
            print(f"  术语库新增 {added} 条（下次自动复用）")

    target = out_path or _default_out(table.ref or inputs[0].stem)
    export_inquiry(table, target)

    raw_json = target.with_name(target.stem + "_extracted.json")
    raw_json.write_text(result.raw, encoding="utf-8")
    print(f"\n已导出：{target}")
    print(f"抽取原始结果：{raw_json.name}（可用 --from-json 重跑）")
    return target


def run_from_json(json_path: Path, out_path: Path | None) -> Path | None:
    text = json_path.read_text(encoding="utf-8")
    result = Extractor._parse(text)
    if not result.ok:
        print(f"! 解析失败：{result.error}")
        return None
    table = result.table
    print(f"从 {json_path.name} 读入：{len(table.rows)} 行，备注 {len(table.notes)} 条")
    target = out_path or _default_out(table.ref or json_path.stem)
    export_inquiry(table, target)
    print(f"\n已导出：{target}")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="阀门询价整理成中英对照标准表")
    parser.add_argument("inputs", nargs="*", help="一个或多个询价文件")
    parser.add_argument("-o", "--out", default=None, help="输出 xlsx 路径")
    parser.add_argument("--from-json", default=None, help="离线模式：用已有抽取结果 JSON 出表")
    parser.add_argument("--hint", default="", help="补充给模型的已知信息，如项目编号")
    args = parser.parse_args()

    if args.from_json:
        target = run_from_json(Path(args.from_json), Path(args.out) if args.out else None)
        if not target:
            sys.exit(1)
        return

    if not args.inputs:
        parser.print_help()
        return

    paths = [Path(p) for p in args.inputs]
    for p in paths:
        if not p.exists():
            print(f"文件不存在：{p}")
            sys.exit(1)

    target = asyncio.run(
        run(paths, Path(args.out) if args.out else None, hint=args.hint)
    )
    if not target:
        sys.exit(1)


if __name__ == "__main__":
    main()
