"""
从客户询价单生成「参考报价表」。

这是阶段二的核心交付物：
    客户发来询价（型号/规格/数量）
        → 逐条去历史价格库找同类报价
        → 生成一份带「参考单价 + 依据」的 Excel

⚠️ 三条纪律：
    1. **只作参考，不是最终报价。** 表里明确标注匹配层级与依据来源，由你判断。
    2. 匹配不到的项**绝不瞎填**，统一标「需人工询价」。
    3. 输出含真实价格，只写在本机 data/ 目录。

用法：
    python src/quote/make_quote.py "报价单/20260831-某地区/xxx.xlsx"
    python src/quote/make_quote.py "xxx.xlsx" --sheet "阀门清单 Valve List" --out data/xx.xlsx
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import DATA_ROOT  # noqa: E402
from src.quote.extract_prices import (  # noqa: E402
    FULL_RE,
    MODEL_RE,
    find_header,
    norm_model,
    norm_num,
    norm_qty,
    norm_rating,
    norm_size,
    norm_vtype,
    parse_spec,
)
from src.quote.lookup import load_book  # noqa: E402

HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(color="FFFFFF", bold=True, size=10)
CELL_FONT = Font(size=10)
LEVEL_FILL = {
    "①": PatternFill("solid", fgColor="E2EFDA"),   # 精确 —— 绿
    "②": PatternFill("solid", fgColor="FFF2CC"),   # 同口径 —— 黄
    "③": PatternFill("solid", fgColor="F2F2F2"),   # 同系列 —— 灰
    "✗": PatternFill("solid", fgColor="FCE4E4"),   # 未匹配 —— 红
}


# ---------------------------------------------------------------- 读询价单

def extract_demands(path: Path, sheet: str | None = None) -> list[dict]:
    """从询价单里抽"需求行"（不需要价格列）。"""
    wb = load_workbook(path, read_only=True, data_only=True)
    targets = [sheet] if sheet and sheet in wb.sheetnames else wb.sheetnames
    out: list[dict] = []
    HEADROW = ("no", "model", "size", "rating", "material", "operation",
               "vtype", "name", "qty")

    for sn in targets:
        ws = wb[sn]
        # 询价单不一定有价格列，所以单独找一次表头
        rows = list(ws.iter_rows(values_only=True, max_row=25))
        if not rows:
            continue
        best = (0, -1, {})
        import re as _re
        KEYS = {
            "no": [r"^no\.?", r"序号", r"^item"],
            "model": [r"型号", r"\bmodel\b", r"\btype\b"],
            "size": [r"尺寸", r"口径", r"\bsize\b"],
            "rating": [r"压力等级", r"\bclass\b", r"\brating\b", r"PN"],
            "material": [r"材质", r"material"],
            "operation": [r"操作方式", r"operation", r"actuat", r"驱动"],
            "vtype": [r"阀体类型", r"阀门类型", r"valve\s*type"],
            "name": [r"名称", r"描述", r"description", r"product\s*name", r"规格"],
            "qty": [r"数量", r"\bqty\b", r"quantity"],
        }
        KEYS = {k: [_re.compile(p, _re.I) for p in v] for k, v in KEYS.items()}
        for i, row in enumerate(rows):
            cells = [("" if c is None else str(c).strip()) for c in row]
            idx: dict[str, int] = {}
            for f in HEADROW:
                for j, c in enumerate(cells):
                    if not c or j in idx.values():
                        continue
                    if any(p.search(c) for p in KEYS[f]):
                        idx[f] = j
                        break
            if len(idx) > best[0]:
                best = (len(idx), i + 1, idx)   # type: ignore[assignment]
        hi, idx = best[1], best[2]              # type: ignore[misc]
        # 客户的技术询价清单常常没有"数量"列，甚至没有"型号"列 —— 只要有规格类字段就受理
        if hi < 0 or not ({"size", "rating", "series", "model", "vtype"} & set(idx)):
            continue

        def get(row, key, default=None):
            j = idx.get(key)
            if j is None or j >= len(row):
                return default
            return row[j]

        for row in ws.iter_rows(min_row=hi + 1, max_row=hi + 500, values_only=True):
            if row is None:
                continue
            model = norm_model(get(row, "model") or get(row, "name") or "")
            vtype = norm_vtype(str(get(row, "vtype") or "")) or norm_vtype(model)
            size = norm_size(get(row, "size")) or parse_spec(model)[0]
            rating = norm_rating(get(row, "rating")) or parse_spec(model)[1]

            series = ""
            mm = MODEL_RE.search(model)
            if mm:
                series = mm.group(1).upper()

            # 有效需求 = 有我方型号，或者有（阀门类型 + 口径 + 压力等级）三件套
            if not series and not (vtype and size and rating):
                continue

            qty = norm_qty(get(row, "qty")) or 1    # 技术清单没有数量 → 记 1（表里会标注）

            if series:
                raw = model
            else:
                raw = " ".join(p for p in (vtype.upper(), size, rating) if p)

            out.append({
                "raw": raw,
                "series": series,
                "vtype": vtype,
                "size": size,
                "rating": rating,
                "material": norm_model(str(get(row, "material") or ""))[:28],
                "operation": norm_model(str(get(row, "operation") or ""))[:28],
                "qty": qty,
                "sheet": sn,
            })
    wb.close()
    return out


# ---------------------------------------------------------------- 匹配价格

def _pick(rows: list[dict], d: dict, level: str) -> dict | None:
    """从候选里挑建议价：同材质优先，取中位数（抗异常值）。"""
    if not rows:
        return None
    pick = rows
    if d.get("material"):
        mt = [r for r in rows
              if d["material"][:6].lower() in (r.get("material") or "").lower()]
        if mt:
            pick = mt
    cur = Counter(r["currency"] for r in pick).most_common(1)[0][0]
    ps = sorted(r["price"] for r in pick if r["currency"] == cur)
    if not ps:
        return None
    latest = max(pick, key=lambda x: x.get("file_date", 0))
    return {
        "level": level,
        "unit": statistics.median(ps),
        "currency": cur,
        "low": ps[0],
        "high": ps[-1],
        "n": len(ps),
        "basis": latest.get("source", ""),
        "basis_date": latest.get("file_date", 0),
        "same_material": pick is not rows,
        # 匹配到的历史型号 —— 规格书要用它去产品资料里找技术参数
        "ref_series": latest.get("series", ""),
        "ref_model": latest.get("model", ""),
    }


def price_for(book: list[dict], d: dict) -> dict:
    """
    两条匹配路径：
      A. **有我方型号** → 按型号匹配（同系列 + 口径 + 压力等级），最准。
      B. **没有型号**（客户技术询价清单就是这样）→ 按「阀门类型 + 口径 + 压力等级」匹配规格。

    这是本项目最重要的认知：客户询价是按**工况规格**提的，他们不知道我们的型号。
    """
    # ---------- A. 型号路径 ----------
    if d.get("series"):
        sizes = (
            ("①", [r for r in book if r["series"] == d["series"]
                   and r["size"] == d["size"] and r["rating"] == d["rating"]]),
            ("②", [r for r in book if r["series"] == d["series"] and r["size"] == d["size"]]),
            ("③", [r for r in book if r["series"] == d["series"]]),
        )
        for level, rows in sizes:
            got = _pick(rows, d, level)
            if got:
                got["path"] = "型号"
                return got
        return {"level": "✗", "hint": "价格库里没有同系列记录"}

    # ---------- B. 规格路径 ----------
    if d.get("vtype"):
        base = [r for r in book if r.get("vtype") == d["vtype"]]
        for level, rows in (
            ("①", [r for r in base if r["size"] == d["size"] and r["rating"] == d["rating"]]),
            ("②", [r for r in base if r["size"] == d["size"]]),
            ("③", [r for r in base if r["rating"] == d["rating"]]),
        ):
            got = _pick(rows, d, level)
            if got:
                got["path"] = "规格"
                return got
        got = _pick(base, d, "③")
        if got:
            got["path"] = "规格"
            return got
        return {"level": "✗", "hint": f"价格库里没有 {d['vtype']} 类型的记录"}

    return {"level": "✗", "hint": "既无我方型号也无规格信息，无法匹配"}


# ---------------------------------------------------------------- 出表

def write_quote(demands: list[dict], results: list[dict], out_path: Path, src_name: str) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "参考报价"

    heads = ["序号", "客户型号 / 描述", "系列 / 参考型号", "口径", "压力等级", "材质",
             "数量", "参考单价", "参考总价", "币种", "匹配置信度", "依据（最近一次同类报价）"]
    widths = [6, 40, 10, 8, 10, 22, 7, 12, 13, 7, 12, 52]
    ws.append(heads)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
        c = ws.cell(row=1, column=i)
        c.fill, c.font, c.alignment = HDR_FILL, HDR_FONT, Alignment(vertical="center", horizontal="center")
    ws.row_dimensions[1].height = 22
    ws.freeze_panes = "A2"

    total_amt = 0.0
    for i, (d, r) in enumerate(zip(demands, results), 1):
        lv = r.get("level", "✗")
        if lv == "✗":
            unit_s, tot_s, cur = "需人工询价", "", ""
            basis = r.get("hint", "")
        else:
            unit = r.get("unit")
            unit_s = round(unit, 2)
            tot_s = round(unit * d["qty"], 2)
            cur = r.get("currency", "")
            total_amt += tot_s
            basis = f"[{r.get('path','')}] {r.get('basis_date','')} · {r.get('basis','')}"
        # 客户没给我方型号时，"系列"列显示**匹配到的历史型号** —— 规格书要靠它。
        series_show = d["series"] or r.get("ref_series", "") or "—"
        row = [i, d["raw"], series_show, d["size"] or "—", d["rating"] or "—",
               d["material"] or "—", d["qty"], unit_s, tot_s, cur, lv, basis]
        ws.append(row)
        for j in range(1, len(heads) + 1):
            c = ws.cell(row=i + 1, column=j)
            c.font = CELL_FONT
            c.alignment = Alignment(vertical="center", wrap_text=(j in (2, 6, 12)))
        ws.cell(row=i + 1, column=11).fill = LEVEL_FILL.get(lv, LEVEL_FILL["✗"])
        for j in (8, 9):
            ws.cell(row=i + 1, column=j).number_format = "#,##0.00"

    # 合计行
    last = len(demands) + 2
    ws.cell(row=last, column=1, value="合计").font = Font(bold=True, size=10)
    ws.cell(row=last, column=9, value=round(total_amt, 2)).font = Font(bold=True, size=10)
    ws.cell(row=last, column=9).number_format = "#,##0.00"
    ws.cell(row=last, column=11, value="金额为参考值，未按币种折算").font = Font(size=9, italic=True, color="808080")

    # ---------- 说明 sheet ----------
    ws2 = wb.create_sheet("说明与免责")
    notes = [
        ["本表由历史报价自动生成，仅供报价参考，不构成最终报价。"],
        [""],
        ["匹配方式（两条路径）"],
        ["路径 A · 型号：给出了我方型号（如 GKV225-3″-150LB）→ 按型号精确匹配，最准。"],
        ["路径 B · 规格：客户只给工况规格（口径 + 压力等级 + 阀门类型），没有我方型号"],
        ["　　　　　　　→ 按「阀门类型 + 口径 + 压力等级」匹配历史同类阀门。客户询价单多属此类。"],
        [""],
        ["匹配置信度（① 最可信 → ③ 最宽松）"],
        ["①  完全匹配：型号路径=同系列+同口径+同压力等级；规格路径=同类型+同口径+同压力等级"],
        ["②  次级匹配：口径相同、压力等级不同（压力等级会影响价格）"],
        ["③  宽松匹配：仅系列相同 或 仅压力等级相同 —— 只作量级参考，差异大时慎用"],
        ["✗  未匹配：价格库里没有同类记录，需人工询价"],
        [""],
        ["参考单价取法"],
        ["取同类历史报价的【中位数】（抗异常值）；若询价单标了材质，优先取同材质的记录。"],
        ["金额按对应币种分别合计，未做汇率折算。"],
        ["币种规则：文件内无明确币种标识时，按人民币(CNY)计。"],
        [""],
        ["数据来源"],
        [f"客户询价单：{src_name}"],
        ["历史价格库：data/price_book.jsonl（由 src/quote/extract_prices.py 从历史报价抽取）"],
    ]
    for i, row in enumerate(notes, 1):
        ws2.cell(row=i, column=1, value=row[0]).font = (
            Font(bold=True, size=11) if row and row[0] and row[0].endswith("说明")
            or (row and row[0] in ("匹配置信度说明", "参考单价取法", "数据来源"))
            else CELL_FONT)
        ws2.cell(row=i, column=1).alignment = Alignment(wrap_text=False)
    ws2.column_dimensions["A"].width = 96

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("inquiry", help="客户询价单路径（xlsx）")
    ap.add_argument("--sheet", default=None, help="指定 sheet（默认取全部）")
    ap.add_argument("--out", default=None, help="输出路径")
    args = ap.parse_args()

    p = Path(args.inquiry)
    if not p.exists():
        p = DATA_ROOT / args.inquiry
    if not p.exists():
        print(f"找不到询价单：{args.inquiry}")
        return

    book = load_book()
    if not book:
        return
    print(f"价格库 {len(book)} 条\n询价单：{p.name}")

    demands = extract_demands(p, args.sheet)
    print(f"从询价单提取到需求：{len(demands)} 行")
    if not demands:
        print("没提取到有效需求行（检查表头是否能识别）")
        return

    results = [price_for(book, d) for d in demands]
    cnt = Counter(r.get("level") for r in results)
    print("匹配情况：", dict(cnt))
    matched = sum(v for k, v in cnt.items() if k != "✗")
    print(f"可自动给出参考价：{matched} / {len(demands)}"
          f"（{100*matched/len(demands):.0f}%）\n")

    out = Path(args.out) if args.out else (
        Path(__file__).resolve().parents[2] / "data" / f"参考报价_{p.stem}.xlsx")
    write_quote(demands, results, out, str(p))
    print(f"参考报价表已生成：{out}")

    print("\n预览（前 8 行）：")
    for d, r in list(zip(demands, results))[:8]:
        unit = f"{r.get('unit', 0):,.2f} {r.get('currency','')}" if r.get("level") != "✗" else "需人工"
        print(f"   [{r.get('level','✗')}] {d['raw'][:34]:<36} "
              f"{d['size'] or '-':<6} {d['rating'] or '-':<7} x{d['qty']:<4} → {unit}")


if __name__ == "__main__":
    main()
