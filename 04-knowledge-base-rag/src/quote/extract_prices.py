"""
从历史报价 Excel 中抽取「型号 → 单价」数据，建立价格库。

阶段二的第一步：**参考报价靠的是"我们历史上报过多少价"**。
好消息是这些报价单里的型号（GKV125-1"-800LB）和单价都是结构化的，可以自动抽取。

用法：
    python src/quote/extract_prices.py            # 抽取并写入 data/price_book.jsonl
    python src/quote/extract_prices.py --stats    # 只看统计，不写文件

⚠️ 抽出来的价格是**机密数据**，只写在本机 data/ 目录，绝不外传。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from openpyxl import load_workbook  # noqa: E402

from src.config import DATA_ROOT  # noqa: E402

QUOTE_DIR = DATA_ROOT / "报价单"
OUT = Path(__file__).resolve().parents[2] / "data" / "price_book.jsonl"

# ---------------------------------------------------------------- 列头识别

FIELD_KEYS = {
    "no":        [r"^no\.?$", r"^序号", r"^item", r"^#"],
    "model":     [r"\bmodel\b", r"型号", r"\btype\b", r"product\s*code"],
    "size":      [r"尺寸", r"口径", r"\bsize\b", r"\bDN\b"],
    "rating":    [r"压力等级", r"磅级", r"\bclass\b", r"\brating\b", r"\bPN\b"],
    "name":      [r"product\s*name", r"产品", r"描述", r"description", r"规格", r"\bname\b"],
    "qty":       [r"\bqty\b", r"quantity", r"数量", r"\bpcs\b"],
    "price":     [r"unit\s*price", r"单价", r"/\s*pc\b", r"\bprice\b"],
    "total":     [r"^total", r"合计", r"\bamount\b", r"金额"],
    # 影响价格的两个重要维度（同口径同压力下，材质/是否带执行器会让价格差好几倍）
    "material":  [r"材质", r"\bmaterial\b", r"body"],
    "operation": [r"操作方式", r"\boperation\b", r"actuat", r"驱动", r"传动"],
    # 阀门类型：客户询价单常只给"口径+压力+阀体类型"，没有我方型号，
    # 这时就得靠"阀门类型"来做规格匹配（见 make_quote.py）。
    "vtype":     [r"阀体类型", r"阀门类型", r"valve\s*type", r"\btype\b"],
}
FIELD_KEYS = {k: [re.compile(p, re.I) for p in v] for k, v in FIELD_KEYS.items()}

# 英寸 → DN（公称通径 mm）。阀门口径不是线性的，必须查表。
INCH2DN = {
    "1/2": 15, "3/4": 20, "1": 25, "1.1/4": 32, "1-1/4": 32, "11/4": 32,
    "1.1/2": 40, "1-1/2": 40, "11/2": 40, "2": 50, "2.1/2": 65, "2-1/2": 65,
    "21/2": 65, "3": 80, "4": 100, "5": 125, "6": 150, "8": 200, "10": 250,
    "12": 300, "14": 350, "16": 400, "18": 450, "20": 500, "24": 600,
}


def norm_size(raw) -> str:
    """
    口径统一成 `DN<mm>`。因为同一个尺寸有三种写法：
        1"（英寸）· 15（mm）· DN15
    不统一就没法比较（这也是"价格区间大得离谱"的根因之一）。
    """
    if raw is None:
        return ""
    s = str(raw).strip().replace("“", '"').replace("”", '"')
    if not s:
        return ""
    m = re.search(r"DN\s*(\d+)", s, re.I)
    if m:
        return f"DN{m.group(1)}"
    m = re.fullmatch(r"(\d+)(?:\.0+)?", s)          # 纯数字 → 当作 mm
    if m and int(m.group(1)) >= 8:
        return f"DN{int(m.group(1))}"
    t = s.replace(" ", "").replace("-", ".").replace("”", "").replace('"', "")
    m = re.search(r"(\d+/\d+|\d+(?:\.\d+)?)", t)
    if m:
        key = m.group(1)
        if key in INCH2DN:
            return f"DN{INCH2DN[key]}"
        try:                                        # 兜底：按 1"=25mm 线性折算
            v = float(eval(key))                    # noqa: S307  (仅用于分数)
            return f"DN{int(round(v * 25))}"
        except Exception:
            return ""
    return ""


def norm_rating(raw) -> str:
    """
    压力等级统一：`800#` / `800LB` / `Class800` / `800` → `CL800`；`PN16` 保持 `PN16`。
    公制（PN）和美标（Class）是两套体系，这里不强行换算，只归一化写法。
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    m = re.search(r"PN\s*(\d+)", s, re.I)
    if m:
        return f"PN{m.group(1)}"
    m = re.search(r"(\d{2,4})", s)
    if m:
        return f"CL{int(m.group(1))}"
    return ""


# 阀门类型归一化。客户询价单里常写「Ball Valve / 球阀 / GLOBE / 闸阀」等，
# 统一成英文短标签，方便跨表匹配。
VTYPE_MAP = [
    (re.compile(r"ball|球阀|球\s*阀|球形", re.I), "ball"),
    (re.compile(r"gate|闸阀|闸\s*阀", re.I), "gate"),
    (re.compile(r"globe|截止阀|截止", re.I), "globe"),
    (re.compile(r"check|止回|单向|逆止", re.I), "check"),
    (re.compile(r"butterfly|蝶阀|蝶\s*阀", re.I), "butterfly"),
    (re.compile(r"plug|旋塞", re.I), "plug"),
    (re.compile(r"needle|针\s*阀", re.I), "needle"),
    (re.compile(r"diaphragm|隔膜", re.I), "diaphragm"),
]


def norm_vtype(raw) -> str:
    """把阀门类型描述归一化成 ball/gate/globe/check/butterfly/plug…"""
    s = str(raw or "")
    if not s:
        return ""
    for pat, v in VTYPE_MAP:
        if pat.search(s):
            return v
    return ""


def parse_spec(text: str) -> tuple[str, str]:
    """
    从任意文本里解析出 (口径, 压力等级)，**输出与 norm_size / norm_rating 一致的格式**。

    例：
        GKV225-1"-300LB     -> ("DN25", "CL300")
        GKV212-DN50-PN16    -> ("DN50", "PN16")
        尺寸/DN 列的值 15    -> ("DN15", "")
    """
    t = (text or "").replace("“", '"').replace("”", '"')
    m = SIZE_FRAC.search(t) or SIZE_NUM.search(t)
    size_raw = ""
    if m:
        size_raw = m.group(0)
    else:
        md = SIZE_DN.search(t)
        if md:
            size_raw = f"DN{md.group(1)}"

    rating_raw = ""
    for rx in (RATING_CLASS, RATING_LB, RATING_PN, RATING_K):
        mm = rx.search(t)
        if mm:
            rating_raw = mm.group(0)
            break
    return norm_size(size_raw), norm_rating(rating_raw)

# 阀门型号的特征：GK 开头的系列号（GKV125 / GKV522A / GKP110DA / GKV710 ...）
MODEL_RE = re.compile(r"\b(GK[A-Z]{0,2}\d{2,4}[A-Z]{0,3})\b", re.I)
# 完整型号：系列号 + 后续规格（GKV125-1"-800LB）
FULL_RE = re.compile(r"\bGK[A-Z]{0,2}\d{2,4}[A-Z]{0,3}\s*[-–]\s*[^\s,，;；|]{1,24}", re.I)

# ⚠️ 关键：**同系列不同口径的价格能差几百倍**（GKV225-1" 82 元 vs GKV225-14" 44820 元）。
#    所以参考报价必须匹配到「口径 + 压力等级」，只对系列是没用的。
SIZE_FRAC = re.compile(r'(\d+/\d+)\s*["”]')          # 3/4"
SIZE_NUM = re.compile(r'(\d+(?:\.\d+)?)\s*["”]')     # 1" / 14"
SIZE_DN = re.compile(r"\bDN\s*(\d+)", re.I)          # DN50

RATING_LB = re.compile(r"(\d{2,4})\s*(?:LB|#)\b", re.I)   # 300LB / 800#
RATING_CLASS = re.compile(r"CLASS\s*(\d{2,4})", re.I)     # Class150
RATING_PN = re.compile(r"\bPN\s*(\d{1,3})", re.I)         # PN16
RATING_K = re.compile(r"\b(\d{1,3})\s*K\b", re.I)         # 10K（日标）


# 注：parse_spec 已移到上面（与 norm_size / norm_rating 放在一起，保证格式一致）。

CUR_RE = [
    (re.compile(r"euro|eur|€", re.I), "EUR"),
    (re.compile(r"usd|dollar|\$", re.I), "USD"),
    (re.compile(r"rmb|cny|人民币|¥|元", re.I), "CNY"),
]


def norm_num(v) -> float | None:
    """把单元格里的价格变成数字。能处理 '28.5' / '€28.5' / '1,234.5' 等。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    s = s.replace(",", "").replace("，", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


def norm_qty(v) -> int | None:
    n = norm_num(v)
    return int(n) if n is not None and n >= 0 else None


def norm_model(s: str) -> str:
    """规范化型号：统一引号、压掉多余空格。"""
    if not s:
        return ""
    t = str(s).replace("”", '"').replace("“", '"').replace("’", "'")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def find_header(ws, max_scan: int = 18):
    """在前若干行里找"最像表头"的一行，返回 (行号, 字段→列号)。"""
    rows = list(ws.iter_rows(values_only=True, max_row=max_scan))
    best = (0, -1, {})
    for i, row in enumerate(rows):
        cells = [("" if c is None else str(c).strip()) for c in row]
        idx: dict[str, int] = {}
        # ⚠️ 顺序即优先级：先"身份"类字段（序号/型号），再"规格"类（口径/压力），
        #    最后数量/价格。而且一个列只能被一个字段占用（见 j in idx.values()）。
        for f in ("no", "model", "size", "rating", "name", "qty", "price", "total",
                  "material", "operation", "vtype"):
            for j, c in enumerate(cells):
                if not c or j in idx.values():
                    continue
                if any(p.search(c) for p in FIELD_KEYS[f]):
                    idx[f] = j
                    break
        if len(idx) > best[0]:
            best = (len(idx), i, idx)
    return best[1], best[2]


def guess_currency(ws, header_row: int, price_col: int | None = None) -> str:
    """
    判断这笔报价用什么币种。按可信度依次看：
      ① 价格列自己的表头（`EURO/pc` / `单价(元)` —— 最权威）
      ② 表头整行
      ③ 表格前几行

    ⚠️ 业务规则（已与用户确认）：**文件里没有明确币种标识的，一律按人民币(CNY)计。**
    国内客户/国内供应商的报价单通常直接写数字，不写币种。
    """
    try:
        rows = list(ws.iter_rows(values_only=True, max_row=max(header_row + 2, 6)))
    except Exception:
        return "CNY"

    texts: list[str] = []
    if price_col is not None and header_row < len(rows):
        r = rows[header_row]
        if price_col < len(r) and r[price_col] is not None:
            texts.append(str(r[price_col]))
    if header_row < len(rows):
        texts.append(" ".join(str(c) for c in rows[header_row] if c is not None))
    texts.append(" ".join(str(c) for r in rows for c in r if c is not None))

    for t in texts:
        for pat, cur in CUR_RE:
            if pat.search(t):
                return cur
    return "CNY"      # 没有标识 → 人民币


def extract_sheet(ws) -> list[dict]:
    """从一个 sheet 里抽价格记录。"""
    hi, idx = find_header(ws)
    if hi < 0 or "price" not in idx:
        return []
    cur = guess_currency(ws, hi, idx.get("price"))
    out: list[dict] = []

    def cell(row, key: str) -> str:
        j = idx.get(key)
        if j is None or j >= len(row) or row[j] is None:
            return ""
        return norm_model(row[j])[:28]

    pi = idx["price"]
    for row in ws.iter_rows(min_row=hi + 2, max_row=hi + 400, values_only=True):
        if row is None:
            continue
        price = norm_num(row[pi] if pi < len(row) else None)
        if price is None or price <= 0:
            continue
        # 型号：先看 model 列，再全行兜底找 GKxxxx
        model = ""
        for key in ("model", "name"):
            j = idx.get(key)
            if j is not None and j < len(row) and row[j]:
                cand = norm_model(row[j])
                if MODEL_RE.search(cand):
                    model = cand
                    break
        if not model:
            for c in row:
                if c and MODEL_RE.search(str(c)):
                    model = norm_model(c)
                    break
        if not model:
            continue

        mm = MODEL_RE.search(model)
        series = mm.group(1).upper() if mm else ""
        full = FULL_RE.search(model)
        spec = norm_model(full.group(0)) if full else model

        # 口径 / 压力等级：**优先读专门的列**（最准）。
        # 很多规范表格有独立的「尺寸/DN」和「压力等级/Class」列，
        # 直接读比从型号字符串里猜可靠得多；读不到再退回文本解析。
        raw_size = row[idx["size"]] if "size" in idx and idx["size"] < len(row) else None
        raw_rating = row[idx["rating"]] if "rating" in idx and idx["rating"] < len(row) else None
        size = norm_size(raw_size)
        rating = norm_rating(raw_rating)

        if not size or not rating:
            s2, r2 = parse_spec(spec or model)
            size = size or s2
            rating = rating or r2
        if not size or not rating:
            # 再兜底：从整行文本里找（规格常写在另一列，如 Product Name 列 `1" 800# 2 piece`）
            row_txt = " ".join(str(c) for c in row if c is not None)
            s3, r3 = parse_spec(row_txt)
            size = size or s3
            rating = rating or r3

        out.append({
            "series": series,
            "model": model,
            "spec": spec,
            "size": size,            # 口径（1" / DN50），价格匹配的关键
            "rating": rating,        # 压力等级（300LB / PN16），同样是关键
            "material": cell(row, "material"),     # 材质：同规格下能让价格差几倍
            "operation": cell(row, "operation"),   # 操作方式：手动 / 气动执行器…
            "vtype": norm_vtype(cell(row, "vtype")),  # 阀门类型：无型号时靠它匹配规格
            "qty": norm_qty(row[idx["qty"]] if "qty" in idx and idx["qty"] < len(row) else None),
            "price": price,
            "currency": cur,
            "source": "",            # 由调用方填
        })
    return out


class _ArraySheet:
    """
    把「二维数组」伪装成 worksheet —— 这样 **PDF 表格就能直接复用 xlsx 的抽取逻辑**，
    不必为 PDF 再写一套解析（避免"同一件事两套实现"的坑）。
    只实现 extract_sheet 用到的那一个方法。
    """

    def __init__(self, rows: list[list]):
        self._rows = [tuple(r) for r in rows]

    def iter_rows(self, min_row: int = 1, max_row: int | None = None,
                  values_only: bool = True):
        start = max(0, min_row - 1)
        end = max_row if max_row is not None else len(self._rows)
        for r in self._rows[start:end]:
            yield r


def extract_pdf(path: Path) -> list[dict]:
    """
    从 PDF 报价单里抽价格：逐页抽表格 → 套用与 xlsx **完全相同**的抽取逻辑。

    实测 PDF 报价单（该企业 的 offer）表格比 xlsx 还规整：
        NO. | Description | Size | Model | Body Material | QTY | PRICE (US$) unit | Total
    """
    try:
        import pdfplumber
    except ImportError:
        return []
    out: list[dict] = []
    try:
        with pdfplumber.open(path) as pdf:
            for pg in pdf.pages:
                try:
                    tables = pg.extract_tables()
                except Exception:
                    continue
                for table in (tables or []):
                    if not table or len(table) < 3:
                        continue
                    try:
                        out += extract_sheet(_ArraySheet(table))
                    except Exception:
                        continue
    except Exception:
        return out
    return out


def extract_all(verbose: bool = True) -> list[dict]:
    """遍历报价单目录，抽出全部价格记录（xlsx + PDF）。"""
    xlsx = [p for p in sorted(QUOTE_DIR.rglob("*.xlsx"))
            if not p.name.startswith(("._", "~$"))]
    # ⚠️ 目录下有 561 个 PDF，全跑太慢，且大多数是产品资料而不是报价。
    #    先用文件名筛出"像报价单"的（约 52 个）—— 保守策略，宁可漏抽也不乱抽。
    OFFER_HINT = re.compile(r"offer|quo|price|报价|quotation", re.I)
    pdfs = [p for p in sorted(QUOTE_DIR.rglob("*.pdf"))
            if not p.name.startswith(("._", "~$")) and OFFER_HINT.search(p.name)]
    if verbose:
        print(f"扫描报价单目录：{len(xlsx)} 个 xlsx + {len(pdfs)} 个 PDF")

    records: list[dict] = []
    n_files = 0
    n_pdf_files = 0
    for p in xlsx + pdfs:
        is_pdf = p.suffix.lower() == ".pdf"
        got: list[dict] = []
        if is_pdf:
            got = extract_pdf(p)
            if got:
                n_pdf_files += 1
        else:
            try:
                wb = load_workbook(p, read_only=True, data_only=True)
            except Exception:
                continue
            try:
                for sn in wb.sheetnames:
                    try:
                        got += extract_sheet(wb[sn])
                    except Exception:
                        continue
            finally:
                wb.close()
        if got:
            rel = str(p.relative_to(DATA_ROOT)).replace("\\", "/")
            mt = datetime.fromtimestamp(p.stat().st_mtime)
            for r in got:
                r["source"] = rel
                r["file_date"] = mt.year * 10000 + mt.month * 100 + mt.day
                r["src_type"] = "pdf" if is_pdf else "xlsx"
            records += got
            n_files += 1
    if verbose and n_pdf_files:
        print(f"其中 PDF 报价单贡献了价格：{n_pdf_files} 个文件")

    # 去重：同一文件里重复出现的"同型号+同价格"（多 sheet 或重复行很常见）
    seen: set = set()
    uniq: list[dict] = []
    for r in records:
        key = (r["source"], r["series"], r["size"], r["rating"], r["price"],
               r["qty"], r.get("material", ""), r.get("operation", ""))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    if verbose and len(uniq) != len(records):
        print(f"去掉重复记录：{len(records)} → {len(uniq)} 条")
    records = uniq

    if verbose:
        print(f"抽到价格记录：{len(records)} 条，来自 {n_files} 个文件")
    return records


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true", help="只看统计，不写文件")
    args = ap.parse_args()

    records = extract_all()
    if not records:
        return

    from collections import Counter

    print("币种分布:", dict(Counter((r["currency"] or "未知") for r in records)))
    by_spec: dict = {}
    for r in records:
        key = (r["series"], r.get("size", ""), r.get("rating", ""))
        by_spec.setdefault(key, []).append(r)
    multi = {k: v for k, v in by_spec.items() if len(v) > 1}
    print(f"规格组合（系列+口径+压力）：{len(by_spec)} 个，"
          f"其中 **{len(multi)} 个有多条历史报价** ← 可直接作为参考")
    print()
    print("--- 同规格历史报价最多的（参考价值最高）---")
    for k, v in sorted(by_spec.items(), key=lambda x: -len(x[1]))[:8]:
        prices = [x["price"] for x in v]
        cur = v[0]["currency"] or "?"
        tag = f"{k[0]}-{k[1]}-{k[2]}".ljust(26)
        print(f"   {tag} {len(v):>3} 条  {min(prices):>10.2f} ~ {max(prices):>10.2f} {cur}")
    print()
    print("--- 抽样 3 条 ---")
    for r in records[:3]:
        print(f"   {r['series']} | {r['model'][:38]:<40} 数量{r['qty']} "
              f"单价 {r['price']} {r['currency']}")

    if args.stats:
        print("\n[stats] 未写入文件。")
        return

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n价格库已写入：{OUT}")


if __name__ == "__main__":
    main()
