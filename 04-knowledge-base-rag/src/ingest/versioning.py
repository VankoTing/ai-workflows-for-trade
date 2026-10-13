"""
文档版本与时效推断。

设计见 docs/versioning.md。
本模块只做「从文件路径 / 文件名 / 系统时间推断元数据」，
不读文件内容（内容里的日期留阶段二）。

核心函数：
  find_date(path)     -> (YYYYMMDD, 来源)
  find_version(path)  -> (版本标识, 可比较序号)
  doc_family(path)    -> 家族 ID（同一份资料的不同版本共享）
  version_meta(path)  -> 汇总成一个 dict，可直接写进向量库 metadata
  assign_status(items)-> 整个集合层面判定谁是 current
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- 日期

DATE8 = re.compile(r"(20\d{2})(\d{2})(\d{2})")
YEAR4 = re.compile(r"(20\d{2})")
# 年份范围，如 "2019~2022" / "2019-2022"：
# 注意这是「项目跨度」不是「文档日期」，应取较晚的那年（文档一般在其末期/之后生成）
RANGE = re.compile(r"(20\d{2})\s*[~\-–—]\s*(20\d{2})")

YEAR_MIN, YEAR_MAX = 2015, 2030


def _mk_date(y: int, m: int, d: int) -> int | None:
    if not (YEAR_MIN <= y <= YEAR_MAX and 1 <= m <= 12 and 1 <= d <= 31):
        return None
    return y * 10000 + m * 100 + d


def find_date(path: Path) -> tuple[int, str]:
    """
    按可信度从高到低找日期，返回 (YYYYMMDD, 来源)。

    来源取值：dir / filename / filename-year / mtime
    —— 必须记下来！审计时要能回答"这个日期是读出来的还是猜的"。
    """
    # ① 目录名里的 8 位日期（就近优先：越靠近文件的目录越可信）
    for seg in reversed(list(path.parts[:-1])):
        for m in DATE8.finditer(seg):
            d = _mk_date(*map(int, m.groups()))
            if d:
                return d, "dir"

    # ② 文件名里的 8 位日期
    for m in DATE8.finditer(path.stem):
        d = _mk_date(*map(int, m.groups()))
        if d:
            return d, "filename"

    # ③ 文件名里的「年份范围」（如 2019~2022）→ 取较晚的那一年
    #    坑：这是项目的年份跨度，不是文档日期；文档通常在其末期或之后生成。
    #    实测踩到过：REFERENCE LIST ... 2019~2022.pdf 曾被误判成 2019 年。
    for m in RANGE.finditer(path.stem):
        y = int(m.group(2))
        if YEAR_MIN <= y <= YEAR_MAX:
            return y * 10000 + 101, "filename-range"

    # ④ 文件名里只有 4 位年份 → 按当年年初估算（保证相对新旧顺序正确）
    for m in YEAR4.finditer(path.stem):
        y = int(m.group(1))
        if YEAR_MIN <= y <= YEAR_MAX:
            return y * 10000 + 101, "filename-year"

    # ⑤ 兜底：文件修改时间
    t = datetime.fromtimestamp(path.stat().st_mtime)
    return t.year * 10000 + t.month * 100 + t.day, "mtime"


# ---------------------------------------------------------------- 版本号

RE_REV = re.compile(r"\brev\.?\s*(\d+)", re.I)          # Valve List-Q250609 Rev.4
RE_PAREN = re.compile(r"\((\d{1,2})\)\s*$")             # PRICE Germany vs Italy(6)
RE_VN = re.compile(r"\bv(\d{1,2})\b", re.I)             # xxx v3
RE_YSUF = re.compile(r"[\s_\-]+(20\d{2})(?:[._\-](\d{1,2}))?\s*$")  # catalog_2024 / -2025.06


def find_version(path: Path) -> tuple[str, int]:
    """返回 (版本标识, 可比较序号)。序号用于同日期时的排序兜底。"""
    # 先剥掉「年份范围」，否则 "2019-2022" 结尾的 "-2022" 会被误当版本号
    stem = RANGE.sub(" ", path.stem)
    m = RE_REV.search(stem)
    if m:
        return f"Rev.{m.group(1)}", int(m.group(1))
    m = RE_PAREN.search(stem)
    if m:
        return f"v{m.group(1)}", int(m.group(1))
    m = RE_VN.search(stem)
    if m:
        return f"v{m.group(1)}", int(m.group(1))
    m = RE_YSUF.search(stem)
    if m:
        tag = m.group(1) + (f".{m.group(2)}" if m.group(2) else "")
        return tag, 0
    return "", 0


# ---------------------------------------------------------------- 文档家族

def normalize_stem(stem: str) -> str:
    """剥掉版本标记，得到"主干名"。不同版本 → 同一个主干。"""
    # 年份范围先整体抹掉：它是项目跨度，不该影响家族分组
    # （这样 "Reference List 2019-2022" 与 "Reference List 2023-2025" 会归为同一家族）
    s = RANGE.sub(" ", stem)
    s = RE_REV.sub("", s)
    s = RE_PAREN.sub("", s)
    s = RE_VN.sub("", s)
    s = RE_YSUF.sub("", s)
    s = re.sub(r"[\s_\-\.]+", " ", s).strip().lower()
    return s


def doc_family(path: Path, root: Path) -> str:
    """
    同一份资料的不同版本，应该落到同一个 family。

    例：
      product catalog_2024.pdf / product catalog.pdf  -> "product catalog"
      PRICE Germany vs Italy(6).xlsx / ...vs Italy.xlsx -> "price germany vs italy"
    """
    key = normalize_stem(path.stem)
    # 主干太短（如 "offer"、"list"）说明名字太泛，不足以区分，用目录兜底
    if len(key) < 5:
        dir_key = normalize_stem(path.parent.name)
        key = f"{dir_key}/{key}" if key else dir_key
    return key or "unknown"


# ---------------------------------------------------------------- 内容指纹（查重）

def fingerprint(path: Path, blk: int = 65536) -> str:
    """快速指纹 = 文件大小 + 首 64KB + 尾 64KB 的哈希（避免全量读大文件）。"""
    try:
        size = path.stat().st_size
        h = hashlib.md5(str(size).encode())
        with path.open("rb") as f:
            h.update(f.read(blk))
            if size > blk * 2:
                f.seek(-blk, os.SEEK_END)
                h.update(f.read(blk))
        return h.hexdigest()[:16]
    except OSError:
        return ""


# ---------------------------------------------------------------- 汇总

def version_meta(path: Path, root: Path) -> dict:
    """一份文件 → 一组时效元数据（不含 status，status 需要集合层面判定）。"""
    date, src = find_date(path)
    ver, rank = find_version(path)
    return {
        "doc_family": doc_family(path, root),
        "version": ver,
        "version_rank": rank,
        "doc_date": date,
        "doc_date_src": src,
        "effective_from": date,
        "effective_to": 0,          # 0 = 至今有效
    }


def _gap_days(a: int, b: int) -> int:
    try:
        d1 = datetime.strptime(str(a), "%Y%m%d")
        d2 = datetime.strptime(str(b), "%Y%m%d")
        return abs((d1 - d2).days)
    except ValueError:
        return 0


# 哪些日期来源算"可信"（可以据此自动判定版本取代）
TRUSTED_SRC = {"dir", "filename", "filename-year", "filename-range"}

DECISIONS_FILE = Path(__file__).resolve().parents[2] / "data" / "version_decisions.json"


def load_decisions(path: Path | None = None) -> dict:
    """
    读取**人工裁决**结果（由 scripts/build_review_sheet.py 生成的页面导出）。

    格式：
        {"decisions": {"<doc_family>": {"keep": "<被保留的 source>", "action": "keep_one"}}}

    action 取值：keep_one（保留指定那份）/ keep_all（都算有效）/ skip（不动）
    """
    p = Path(path) if path else DECISIONS_FILE
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        dec = d.get("decisions", {})
        return dec if isinstance(dec, dict) else {}
    except Exception:
        return {}


def assign_status(items: list[dict], gap_days: int = 30) -> list[dict]:
    """
    集合层面判定状态。三步走，**越往后越保守**：

    ① 内容完全相同（doc_fp 指纹一致）→ 除主副本外标 `duplicate`
       理由：同一份文件散落在多个目录非常常见，那是"重复"不是"新版本"。
       实测：`REFERENCE LIST ...pdf` 同时躺在 产品资料 / 报价单 / 工作home 三处。
       ⚠️ **本步是全局的（跨家族）** —— 因为同一份文件常被改名，
       例如 `产品资料/Ball Valve(3).pdf` 与 `报价单/xxx/Ball Valve(cast).pdf` 内容一致。

    ② 内容不同 + 双方日期来源都可信 + 日期相差 ≥ gap_days
       → 旧的标 `superseded`（这是明确的版本取代关系，如 2024 版目录 vs 2026 版）

    ③ 其余情况 → 全部保持 `active`，交给诊断报告列成「待裁决冲突」
       理由：**"哪一版作数"是业务决策，程序不擅自判死。**
       （比如两份价格表只差 10 天、或日期来自 mtime，都不能武断认定其中一份作废。）

    ④ **人工裁决优先** —— 如果存在 data/version_decisions.json（由裁决表页面导出），
       则按人的判断覆盖上面的自动结论：被选中的置 `active`，同族其余置 `rejected`；
       选「全部保留」则整族都置 `active`。人的判断高于程序。

    items 需含：source / doc_family / doc_date / version_rank / doc_date_src / doc_fp
    """
    fams: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        fams[it["doc_family"]].append(it)

    # ① 全局按内容指纹去重（**不限于同一家族**）
    #
    # 为什么必须全局？同一份文件在不同目录常被改名，家族就分开了：
    #   产品资料/Ball Valve(3).pdf
    #   报价单/0190045-业务员/Ball Valve(3).pdf
    #   报价单/483290626-某地区/Ball Valve(cast).pdf      ← 内容一模一样
    # 只在家族内去重的话，它们会被当成"不同的资料"全部 active，
    # 检索时同一份内容重复占掉多个名额，把真正有用的资料挤下去。
    decisions = load_decisions()
    keep_set = {d.get("keep", "") for d in decisions.values()
                if d.get("keep") and d.get("keep") != "__all__"}

    def _master_rank(it_: dict):
        """挑"主副本"：人工保留的优先 → 产品资料目录 → 路径短 → 字典序。"""
        s = it_["source"]
        return (0 if s in keep_set else 1,
                0 if s.startswith("产品资料/") else 1,
                len(s), s)

    # 先给每个文件一个默认状态（没有指纹的文件不参与去重，但也必须有 status）
    for it in items:
        it["status"] = "active"
        it.setdefault("dup_of", "")

    by_fp: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        fp = it.get("doc_fp") or ""
        if fp:
            by_fp[fp].append(it)
    for arr in by_fp.values():
        if len(arr) < 2:
            continue
        master = min(arr, key=_master_rank)
        for it in arr:
            if it is master:
                it["status"] = "active"
                it["dup_of"] = ""
            else:
                it["status"] = "duplicate"
                it["dup_of"] = master["source"]

    # ② / ③ 家族内版本取代（只在"内容不同 + 日期可信 + 差距明显"时才动手）
    for arr in fams.values():
        arr.sort(key=lambda x: (x["doc_date"], x["version_rank"]), reverse=True)
        live = [it for it in arr if it["status"] == "active"]
        if len(live) < 2:
            continue
        newest = live[0]
        for it in live[1:]:
            if (newest["doc_date_src"] in TRUSTED_SRC
                    and it["doc_date_src"] in TRUSTED_SRC
                    and _gap_days(it["doc_date"], newest["doc_date"]) >= gap_days):
                it["status"] = "superseded"

    # ④ 人工裁决优先 —— "哪一版作数"是业务决策，人的判断高于程序
    decisions = load_decisions()
    if decisions:
        for fam, dec in decisions.items():
            arr = fams.get(fam)
            if not arr:
                continue
            keep = dec.get("keep", "")
            action = dec.get("action", "keep_one")
            if action == "skip":
                continue
            if action == "keep_all" or keep == "__all__":
                # 恢复为 active，但**不动 duplicate** ——
                # 内容完全相同的副本保留两份没有意义，去重结论不必推翻。
                for it in arr:
                    if it["status"] != "duplicate":
                        it["status"] = "active"
                continue
            for it in arr:
                it["status"] = "active" if it["source"] == keep else "rejected"
    return items


def days_ago(date_int: int, today: int | None = None) -> int:
    """date_int(YYYYMMDD) 距今多少天。"""
    today = today or int(datetime.now().strftime("%Y%m%d"))
    try:
        d1 = datetime.strptime(str(date_int), "%Y%m%d")
        d2 = datetime.strptime(str(today), "%Y%m%d")
        return (d2 - d1).days
    except ValueError:
        return -1


# 需要写进向量库的时效字段（入库流程与回填脚本共用同一份定义）
META_KEYS = (
    "doc_family", "version", "version_rank",
    "doc_date", "doc_date_src",
    "effective_from", "effective_to",
    "status", "doc_fp", "dup_of",
)


def batch_meta(files: list[Path], root: Path) -> dict[str, dict]:
    """
    给一批文件批量生成时效元数据（含内容指纹与 status 判定）。

    返回 {相对路径(正斜杠): meta}。入库流程与回填脚本共用。

    ⚠️ status 是**集合层面**的判定（"谁是最新版本"要跟全库比），
    所以调用时应当把**全量文件**传进来，而不是只传本轮要处理的那批。
    """
    out: dict[str, dict] = {}
    items: list[dict] = []
    for p in files:
        try:
            rel = str(p.relative_to(root)).replace("\\", "/")
        except ValueError:
            rel = str(p).replace("\\", "/")
        m = version_meta(p, root)
        m["doc_fp"] = fingerprint(p)
        m["source"] = rel
        out[rel] = m          # 存引用，assign_status 改了这里也同步
        items.append(m)
    assign_status(items)
    return out


# ---------------------------------------------------------------- CLI 调试

if __name__ == "__main__":
    import sys

    root = Path(r"D:\RAG资料")
    args = sys.argv[1:]
    if not args:
        samples = [
            root / "报价单" / "20260831-某地区" / "product catalog.pdf",
            root / "产品资料" / "product catalog_2024.pdf",
            root / "工作home" / "PRICE Germany vs Italy(6).xlsx",
            root / "报价单" / "某地区该企业询价" / "Valve List-Q250609 Rev.4.xlsx",
        ]
        args = [str(p) for p in samples]

    print(f"{'文件':<46}{'日期':>9}{'来源':>12}{'版本':>8}  家族")
    print("-" * 100)
    for a in args:
        p = Path(a)
        if not p.exists():
            print(f"{p.name[:44]:<46}  （文件不存在，仅演示推断）")
        m = version_meta(p, root)
        print(f"{p.name[:44]:<46}{m['doc_date']:>9}{m['doc_date_src']:>12}"
              f"{m['version'] or '-':>8}  {m['doc_family']}")
