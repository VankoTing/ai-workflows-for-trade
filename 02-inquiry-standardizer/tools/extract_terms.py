"""一次性工具：从历史成品 xlsx 中提取中英术语对照，生成初始术语库。

用法：
    python tools/extract_terms.py

产出：
    data/terms.json   程序读取的术语库
    data/terms.csv    给人看的对照表，方便后续人工增补

提取两条路径：
    1. 单元格内混排： "Ball Valve 球阀"  -> (Ball Valve, 球阀)
    2. 同行分列配对： "Butterfly Valve" | "蝶阀" -> (Butterfly Valve, 蝶阀)
"""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import openpyxl

SRC_DIR = Path(r"C:\Users\Administrator\WorkBuddy\2026-08-09-11-18-29")
PROJECT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT / "glossary"   # 术语库是项目资产，随代码入库

CJK = re.compile(r"[\u4e00-\u9fff]")
LATIN = re.compile(r"[A-Za-z]")
TRIM = " /·-—,;:：、|"

# 表头类词汇，作为术语价值低，直接丢掉
STOP_EN = {
    "no", "item", "english", "chinese", "remarks", "qty", "unit", "total",
    "note", "date", "size", "class", "type", "description", "ser",
}


def norm(text: object) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def split_inline(text: object) -> tuple[str, str] | None:
    """把 "English 中文" 这类混排单元格拆成一对。"""
    s = norm(text)
    if not s or len(s) > 80:
        return None
    m = CJK.search(s)
    if not m:
        return None
    en = s[: m.start()].strip(TRIM)
    zh = s[m.start():].strip()
    if not en or not zh:
        return None
    if not LATIN.search(en):          # 英文侧必须有字母，挡掉 '4" 4英寸'
        return None
    if len(en) > 60 or len(zh) > 8:   # 太长的是句子，不是术语
        return None
    if en.lower().strip(".") in STOP_EN and len(zh) <= 4:
        return None

    en = re.sub(r"\.+$", "", en).strip()      # 去掉尾部句点：'EA.' -> 'EA'
    if not en or CJK.search(en):              # 英文侧混了中文，说明提取错了
        return None

    words = [w for w in re.split(r"[\s,;/]+", en) if w]
    if len(words) >= 3 and len(set(words)) * 1.5 < len(words):
        return None                            # '316 AISI 316 AISI' 这类重复串，是提取错误

    nums = re.findall(r"\d+", en)
    if len(nums) >= 2 and len(set(nums)) < len(nums):
        return None                            # '316 Stainless Steel / 316' 数字重复，也是提取错误
    return en, zh


def is_en_only(text: object) -> bool:
    s = norm(text)
    if not s or len(s) > 60 or CJK.search(s):
        return False
    return bool(LATIN.search(s))


def is_zh_only(text: object) -> bool:
    """纯中文且足够短，才可能是术语。"""
    s = norm(text)
    if not s or len(s) > 8 or not CJK.search(s):
        return False
    return not LATIN.search(s)


def main() -> None:
    files = sorted(SRC_DIR.glob("*Bilingual*.xlsx"))
    if not files:
        print("没找到历史成品文件：", SRC_DIR)
        return

    bucket: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"count": 0, "sources": set()}
    )

    for f in files:
        try:
            wb = openpyxl.load_workbook(f, data_only=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  跳过 {f.name}：{exc}")
            continue
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = [norm(c) for c in row if c is not None and norm(c)]
                if not cells:
                    continue
                for c in cells:
                    hit = split_inline(c)
                    if hit:
                        rec = bucket[hit]
                        rec["count"] += 1
                        rec["sources"].add(f.name)
                ens = [c for c in cells if is_en_only(c)]
                zhs = [c for c in cells if is_zh_only(c)]
                if len(ens) == 1 and len(zhs) == 1:
                    en_c = re.sub(r"\.+$", "", ens[0]).strip()
                    if en_c and not CJK.search(en_c):
                        rec = bucket[(en_c, zhs[0])]
                        rec["count"] += 1
                        rec["sources"].add(f.name)
        wb.close()
        print(f"  已扫描 {f.name}")

    items = [
        {"en": en, "zh": zh, "count": rec["count"], "sources": sorted(rec["sources"])}
        for (en, zh), rec in bucket.items()
    ]
    items.sort(key=lambda x: (-x["count"], x["en"].lower()))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_files": [f.name for f in files],
        "term_count": len(items),
        "terms": items,
    }
    (DATA_DIR / "terms.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with (DATA_DIR / "terms.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["中文", "英文", "出现次数", "来源文件"])
        for it in items:
            writer.writerow([it["zh"], it["en"], it["count"], "; ".join(it["sources"])])

    print(f"\n完成：提取 {len(items)} 条术语 -> data/terms.json + data/terms.csv")
    print("\n出现次数最多的 25 条：")
    for it in items[:25]:
        print(f'  {it["count"]:>3}x  {it["zh"]:<16} {it["en"]}')


if __name__ == "__main__":
    main()
