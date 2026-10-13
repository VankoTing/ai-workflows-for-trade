r"""
生成「工作过程看板」静态页面。

数据来源都是**真实读取**：
  · 资料目录（文件数/体积/类型分布）
  · 向量库 Chroma（切片数、密级分布、来源分布）
  · docs/lessons.md（复盘教训条数）

用法：
    python scripts/build_dashboard.py
输出：
    dashboard/index.html
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import CHROMA_DIR, CHUNK_OVERLAP, CHUNK_SIZE, DATA_ROOT, EMBED_MODEL, LLM_MODEL  # noqa: E402

PROJECT = Path(__file__).resolve().parents[1]
OUT_DIR = PROJECT / "dashboard"
OUT = OUT_DIR / "index.html"


# ---------------------------------------------------------------- 采集数据
def scan_data() -> dict:
    n = total = 0
    ext_n: Counter = Counter()
    ext_sz: Counter = Counter()
    tops: Counter = Counter()
    for dp, _dn, fn in os.walk(DATA_ROOT):
        for f in fn:
            p = os.path.join(dp, f)
            n += 1
            try:
                s = os.path.getsize(p)
            except OSError:
                s = 0
            total += s
            e = (os.path.splitext(f)[1].lower() or "(无)")
            ext_n[e] += 1
            ext_sz[e] += s
            rel = os.path.relpath(dp, DATA_ROOT)
            tops[rel.split(os.sep)[0]] += 1
    return {"files": n, "mb": total / 1024 / 1024, "ext_n": ext_n, "ext_sz": ext_sz, "tops": tops}


def scan_index() -> dict:
    try:
        import chromadb

        col = chromadb.PersistentClient(path=str(CHROMA_DIR)).get_collection("knowledge")
        total = col.count()
        sens: Counter = Counter()
        top: Counter = Counter()
        # 必须分页读：切片上万后一次性 get 会撞上 SQLite 的
        # "too many SQL variables" 上限（见 lessons.md）。
        off = 0
        while off < total:
            d = col.get(include=["metadatas"], limit=4000, offset=off)
            metas = d["metadatas"] or []
            if not metas:
                break
            for m in metas:
                sens[m.get("sensitivity", "?")] += 1
                top[m.get("top", "?")] += 1
            off += len(metas)
        return {"count": total, "sens": sens, "top": top, "ok": True}
    except Exception:
        return {"count": 0, "sens": Counter(), "top": Counter(), "ok": False}


def scan_age() -> dict:
    """读取时效诊断报告（由 scripts/age_report.py 生成）。"""
    f = PROJECT / "data" / "age_report.json"
    if not f.exists():
        return {"ok": False}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        d["ok"] = True
        return d
    except Exception:
        return {"ok": False}


def scan_eval() -> dict:
    """读取评测报告（由 scripts/run_eval.py 生成），并带上基线用于对比。"""
    f = PROJECT / "data" / "eval_report.json"
    base = PROJECT / "data" / "eval_baseline.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        out: dict = {"ok": True, **d.get("summary", {}), "checks": d.get("checks", {})}
        if base.exists():
            out["baseline"] = json.loads(base.read_text(encoding="utf-8")).get("summary", {})
        return out
    except Exception:
        return {"ok": False}


def scan_price() -> dict:
    """价格库规模（data/price_book.jsonl）。"""
    f = PROJECT / "data" / "price_book.jsonl"
    if not f.exists():
        return {"ok": False, "records": 0, "files": 0}
    n = 0
    srcs: set[str] = set()
    n_pdf = 0
    try:
        for line in f.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            n += 1
            r = json.loads(line)
            srcs.add(r.get("source", ""))
            if r.get("src_type") == "pdf":
                n_pdf += 1
        return {"ok": True, "records": n, "files": len(srcs), "pdf_records": n_pdf}
    except Exception:
        return {"ok": False, "records": 0, "files": 0}


# ------------------------------------------------- 脱敏（对外发布必用）
# ⚠️ 教训：看板一旦部署到公网就是**对外发布**。里面只要出现客户地区、业务员姓名、
#    公司名或资料路径，就等于泄露。必须在生成本身就脱敏，而不是靠人工检查。
#
# ⚠️ 更进一层：**这些名称的"词表"本身也是敏感信息** ——
#    所以外置到 data/mask_terms.txt（该目录不进仓库），代码里不出现任何具体名称。
MASK_TERMS_FILE = PROJECT / "data" / "mask_terms.txt"


def _load_mask_terms():
    """按类别加载脱敏词表；文件不存在时返回三个 None（只做路径脱敏）。"""
    groups: dict[str, list[str]] = {"client": [], "person": [], "company": []}
    try:
        if MASK_TERMS_FILE.exists():
            for line in MASK_TERMS_FILE.read_text(encoding="utf-8").splitlines():
                if "|" not in line:
                    continue
                kind, _, word = line.partition("|")
                kind, word = kind.strip(), word.strip()
                if kind in groups and word:
                    groups[kind].append(re.escape(word))
    except Exception:
        pass
    pats = []
    for k in ("client", "person", "company"):
        pats.append(re.compile("|".join(groups[k]), re.I) if groups[k] else None)
    return pats[0], pats[1], pats[2]


MASK_CLIENT, MASK_PERSON, MASK_COMPANY = _load_mask_terms()
MASK_PATH = re.compile(r"((?:产品资料|报价单|工作home|投标文件|采购合同|合同)/)"
                       r"[^\s`|、）)<>\"']+")


def mask(text: str) -> str:
    """
    把对外发布的内容脱敏：公司名 / 业务员 / 客户地区 / 资料路径。
    对整页 HTML 做一次纯文本替换是安全的 —— 替换的都是正文文字，不碰标签结构。
    """
    if not text:
        return text
    t = text
    if MASK_COMPANY:
        t = MASK_COMPANY.sub("公司", t)
    if MASK_PERSON:
        t = MASK_PERSON.sub("业务员", t)
    if MASK_CLIENT:
        t = MASK_CLIENT.sub("某地区", t)

    def _p(m: re.Match) -> str:
        parts = m.group(0).split("/")
        if len(parts) <= 2:
            return parts[0] + "/…"
        return f"{parts[0]}/…/{parts[-1]}"

    return MASK_PATH.sub(_p, t)


# ------------------------------------------------- 复盘教训：Markdown → HTML
def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(s: str) -> str:
    """行内格式：**粗体** / `代码` / <br>。"""
    s = _esc(s)
    s = s.replace("&lt;br&gt;", "<br>")
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return s


def _table(rows: list[str]) -> str:
    data = []
    for r in rows:
        cells = [c.strip() for c in r.strip().strip("|").split("|")]
        if cells and all(set(c) <= set("-: ") for c in cells):
            continue                      # 跳过 |---|---| 这行
        data.append(cells)
    if not data:
        return ""
    th = "".join(f"<th>{_inline(c)}</th>" for c in data[0])
    trs = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>"
                  for row in data[1:])
    return f"<table class='lstab'><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


def _md(body: str) -> str:
    """把教训正文的 Markdown 转成 HTML（支持表格 / 列表 / 段落 / 分隔线）。"""
    lines = body.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("|"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                buf.append(lines[i])
                i += 1
            out.append(_table(buf))
            continue
        if s.startswith("- "):
            buf = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                buf.append(lines[i].strip()[2:])
                i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in buf) + "</ul>")
            continue
        if s == "---":
            i += 1
            continue
        if s:
            out.append(f"<p>{_inline(s)}</p>")
        i += 1
    return "".join(out)


def scan_lessons() -> list[dict]:
    """解析 docs/lessons.md，返回每条教训的完整内容（编号 + 标题 + 正文 HTML）。"""
    f = PROJECT / "docs" / "lessons.md"
    if not f.exists():
        return []
    text = f.read_text(encoding="utf-8")
    out = []
    for blk in re.split(r"^##\s+", text, flags=re.M)[1:]:
        m = re.match(r"(L\d+)\s*·\s*(.+)", blk)
        if not m:
            continue
        code, title = m.group(1), m.group(2).strip()
        body = blk.split("\n", 1)[1] if "\n" in blk else ""
        out.append({"code": code, "title": title, "html": _md(body)})
    return out


# ---------------------------------------------------------------- 页面片段
STAGES = [
    ("①", "资料盘点", "2211 个文件 / 4.56 GB", "done"),
    ("②", "解析（文本层）", "pypdf / python-docx / openpyxl", "done"),
    ("③", "切片 Chunking", f"{CHUNK_SIZE} 字/块，重叠 {CHUNK_OVERLAP} 字", "done"),
    ("④", "向量化 Embedding", f"{EMBED_MODEL}（1024 维）", "done"),
    ("⑤", "入库 Vector DB", "Chroma + 元数据标签（密级/来源）", "done"),
    ("⑥", "OCR 兜底（逐页决策）", "RapidOCR + PyMuPDF", "done"),
    ("⑦", "OCR 接入入库流程", "所有 PDF 逐页解析，参数全部进库", "done"),
    ("⑧", "GPU 加速 OCR", "onnxruntime-gpu：9.05s → 4.41s / 页", "done"),
    ("⑨", "问答 RAG", f"{LLM_MODEL} 生成带引用答案（阶段一验收通过）", "done"),
    ("⑩", "多进程并行", "6 进程：74 页 515s → 285s", "done"),
    ("⑪", "权限过滤（三角色）", "元数据 where 过滤，检索前生效", "wip"),
    ("⑫", "全量入库（分批）", "跑通限时批处理 + 断点续跑 + 单文件预算", "done"),
    ("⑬", "全量入库完成", "1232 / 1235 个文件（余 3 个为空文件）", "done"),
    ("⑭", "版本与时效管理", "日期/版本/家族/状态 + 检索时间过滤", "done"),
    ("⑮", "质量衰减诊断", "过期 / 冲突 / 重复 / 缺口 四类扫描", "done"),
    ("⑯", "人工裁决闭环", "冲突裁决 → 应用 → 检索遵守（版本冲突归零）", "done"),
    ("⑰", "业务闭环 · 报价表", "客户询价单 → 参考报价表（67% 自动匹配）", "done"),
    ("⑱", "业务闭环 · 规格书", "参考报价表 → 技术规格书（参数带出处）", "done"),
    ("⑲", "评测与调优", "20 题黄金集 · 召回 54.5% → 72.7%", "done"),
    ("⑳", "报价数据扩充", "PDF 报价单入库：1179 条 / 76 个文件 / 240 个规格组合", "done"),
]

TIMELINE = [
    ("环境", "关闭代理解决 fake-ip", "Ollama 拒绝从 198.18.x.x 下载", "L01"),
    ("环境", "安装 Ollama + 拉模型", "qwen2.5:7b + bge-m3 就位", "—"),
    ("开发", "写解析 / 切片 / 入库 / 问答", "606 切片入库成功", "L08"),
    ("排查", "问球阀 → 模型答「未找到」", "顺藤摸瓜发现是入库问题", "L04"),
    ("排查", "检查源 PDF", "9708 字全是页眉页脚，参数一个没抽到", "L02 L03"),
    ("排查", "统计向量库", "重复噪音切片淹没知识（Gate Valve 134 块）", "L10"),
    ("修复", "关键词收紧 + 样板文字剔除", "命中数 10→2，噪音切片减少", "L03 L10"),
    ("修复", "引入 OCR（逐页决策）", "球阀参数 PN16/40、-20~180℃ 全部认出", "L11 L12"),
    ("修复", "OCR 接进入库流程", "Ball Valve(2) 19 块 → 55 块，参数终于入库", "L13"),
    ("验收", "重问球阀问题", "准确作答 + 引用来源，阶段一闭环打通", "L13"),
    ("优化", "GPU 加速（过了四道坎）", "单页推理 9.05s → 4.41s；静默回退是关键坑", "L14"),
    ("优化", "多进程并行（又过四道坎）", "74 页 515s → 285s；僵尸进程差点冤杀代码", "L15"),
]

NEXT_STEPS = [
    "价格精修：汇率折算 + 数量因子 + 排查「单价其实是总价」（演示性质，不必追求精确）",
    "接真实「客户 → 业务员」映射，落地三角色权限（需企业提供映射，当前为模拟）",
    "BM25 关键词混合检索 / Rerank，改善「没有型号的抽象问题」的召回",
    ".xls 老格式批量转 .xlsx（已评估：openpyxl 读不了 2003 格式，需先转换）",
]


def bar(label: str, pct: float, text: str, color: str) -> str:
    return f"""<div class="bar"><span class="bl">{label}</span>
      <div class="bt"><i style="width:{pct:.1f}%;background:{color}"></i></div>
      <span class="bv">{text}</span></div>"""


def build_html(data: dict, idx: dict, lessons: list, age: dict, ev: dict,
               pr: dict) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    steps = ""
    for no, name, desc, st in STAGES:
        cls = {"done": "ok", "wip": "wip", "todo": "todo"}[st]
        mark = {"done": "✓", "wip": "◐", "todo": "○"}[st]
        steps += f"""<div class="step {cls}"><span class="sm">{mark}</span>
          <div><div class="sn">{no} {name}</div><div class="sd">{desc}</div></div></div>"""

    tl = ""
    for tag, title, desc, lesson in TIMELINE:
        tl += f"""<div class="tl-item"><span class="tl-tag">{tag}</span>
          <div class="tl-body"><div class="tl-t">{title}</div><div class="tl-d">{desc}</div>
          {f'<span class="tl-l">{lesson}</span>' if lesson != "—" else ''}</div></div>"""

    less = ""
    for L in lessons:
        less += (f'<details class="lsd" open><summary><b>{L["code"]}</b> '
                 f'{_inline(L["title"])}</summary><div class="lsb">{L["html"]}</div></details>')

    ext_rows = ""
    for e, c in data["ext_n"].most_common(8):
        ext_rows += f"<tr><td>{e}</td><td>{c}</td><td>{data['ext_sz'][e]/1024/1024:.0f} MB</td></tr>"

    top_rows = ""
    for t, c in data["tops"].most_common(8):
        top_rows += f"<tr><td>{t}</td><td>{c}</td></tr>"

    sens_color = {"public": "#639922", "internal": "#BA7517",
                  "confidential": "#D85A30", "restricted": "#A32D2D"}
    sens_total = idx["count"] or 1
    sens_bars = ""
    for s in ["public", "internal", "confidential", "restricted"]:
        c = idx["sens"].get(s, 0)
        sens_bars += bar(s, 100 * c / sens_total, str(c), sens_color[s])

    nexts = "".join(f"<li>{x}</li>" for x in NEXT_STEPS)

    idx_rows = ""
    for t, c in idx["top"].most_common(8):
        idx_rows += f"<tr><td>{t}</td><td>{c}</td></tr>"

    # ---------- 时效健康（设计见 docs/versioning.md）----------
    if age.get("ok"):
        score = age.get("health_score", 0)
        scolor = "#16a34a" if score >= 80 else ("#d97706" if score >= 60 else "#dc2626")
        expired = age.get("expired", [])
        conflicts = age.get("conflicts", [])
        dups = age.get("duplicates", [])
        stale = age.get("stale_types", [])
        st_cnt = age.get("status", {})

        exp_rows = "".join(
            f"<tr><td>{e['age_days']} 天</td><td>{e['doc_date']}</td>"
            f"<td>{e['source'][:58]}</td></tr>" for e in expired[:6]) \
            or "<tr><td colspan='3'>无</td></tr>"

        cf_rows = ""
        for c in conflicts[:5]:
            dates = "、".join(str(m["doc_date"]) for m in c["members"][:3])
            cf_rows += (f"<tr><td>{c['family'][:38]}</td><td>{c['count']}</td>"
                        f"<td>{dates}</td></tr>")
        cf_rows = cf_rows or "<tr><td colspan='3'>无</td></tr>"

        dup_rows = "".join(
            f"<tr><td>{d['count']} 份</td><td>{d['keep'][:54]}</td></tr>"
            for d in dups[:5]) or "<tr><td colspan='2'>无</td></tr>"

        st_rows = "".join(
            f"<tr><td>{s['type']}</td><td>{s['latest'] or '—'}</td>"
            f"<td>{s['age_days']} 天</td></tr>" for s in stale) \
            or "<tr><td colspan='3'>无</td></tr>"

        age_html = f"""
  <h2>时效健康（版本 / 过期 / 冲突 / 重复）</h2>
  <div class="grid g4">
    <div class="card"><div class="ml">时效健康分</div>
      <div class="mv" style="color:{scolor}">{score}<span style="font-size:14px;color:#94a3b8"> / 100</span></div></div>
    <div class="card"><div class="ml">过期预警</div><div class="mv">{len(expired)}</div></div>
    <div class="card"><div class="ml">版本冲突</div><div class="mv">{len(conflicts)}</div></div>
    <div class="card"><div class="ml">重复副本组</div><div class="mv">{len(dups)}</div></div>
  </div>
  <div class="grid g2">
    <div class="card">
      <div class="ml" style="margin-bottom:8px">过期预警 Top 6（active 但已 > 365 天）</div>
      <table class="lstab"><thead><tr><th>天数</th><th>日期</th><th>文件</th></tr></thead>
      <tbody>{exp_rows}</tbody></table>
    </div>
    <div class="card">
      <div class="ml" style="margin-bottom:8px">版本冲突 Top 5（同一家族多份 active）</div>
      <table class="lstab"><thead><tr><th>家族</th><th>份</th><th>日期</th></tr></thead>
      <tbody>{cf_rows}</tbody></table>
    </div>
  </div>
  <div class="grid g2">
    <div class="card">
      <div class="ml" style="margin-bottom:8px">重复副本 Top 5（内容相同、散落多处）</div>
      <table class="lstab"><thead><tr><th>份数</th><th>主副本</th></tr></thead>
      <tbody>{dup_rows}</tbody></table>
    </div>
    <div class="card">
      <div class="ml" style="margin-bottom:8px">关键类型更新情况</div>
      <table class="lstab"><thead><tr><th>类型</th><th>最新</th><th>距今</th></tr></thead>
      <tbody>{st_rows}</tbody></table>
    </div>
  </div>
  <div class="note">版本状态：<b>active</b> {st_cnt.get('active', 0)}　·　
  <b>duplicate</b> {st_cnt.get('duplicate', 0)}（重复副本，检索自动排除）　·　
  <b>superseded</b> {st_cnt.get('superseded', 0)}（已被新版本取代）。<br>
  机制：每份资料自动打上 <b>日期 / 版本号 / 内容指纹</b>；检索默认只命中「当前有效」版本，
  用 <code>--as-of 日期</code> 可做时间旅行。详见 <code>docs/versioning.md</code>。</div>
"""
    else:
        age_html = ('<h2>时效健康</h2><div class="note">尚无诊断报告。'
                    '先运行 <code>python scripts/age_report.py</code>。</div>')

    # ---------- 评测质量 ----------
    if ev.get("ok"):
        base = ev.get("baseline") or {}

        def dv(key: str) -> str:
            a, b = base.get(key), ev.get(key)
            if a is None or b is None:
                return ""
            d = b - a
            if abs(d) < 0.005:
                return '<span style="color:#94a3b8">与基线持平</span>'
            col = "#16a34a" if d > 0 else "#dc2626"
            return f'<span style="color:{col}">{"↑" if d > 0 else "↓"} {abs(d)*100:.1f}pt vs 基线</span>'

        checks = ev.get("checks", {})
        chk_html = "　".join(f"{'✅' if v else '❌'} {k}" for k, v in checks.items()) or "—"

        eval_html = f"""
  <h2>评测质量（黄金集 {ev.get('cases', 0)} 题）</h2>
  <div class="grid g4">
    <div class="card"><div class="ml">文件召回率</div>
      <div class="mv">{ev.get('recall', 0)*100:.1f}%</div>
      <div class="ml" style="margin-top:4px">{dv('recall')}</div></div>
    <div class="card"><div class="ml">关键词命中</div>
      <div class="mv">{ev.get('keyrate', 0)*100:.1f}%</div>
      <div class="ml" style="margin-top:4px">{dv('keyrate')}</div></div>
    <div class="card"><div class="ml">系统底线检查</div>
      <div class="mv" style="font-size:15px;line-height:1.6">{chk_html}</div></div>
    <div class="card"><div class="ml">评测规模</div>
      <div class="mv">{ev.get('cases', 0)} 题</div>
      <div class="ml" style="margin-top:4px">top_k = {ev.get('top_k', '-')}</div></div>
  </div>
  <div class="note">
    <b>为什么要有评测集：</b>没有它，"检索变好了"只是感觉；有了它，改动前后能出 ↑/↓ 的数字。
    本轮把「精确型号匹配」从规格书模块抽成公共能力（<code>src/search/hybrid.py</code>）后，
    召回率 <b>54.5% → 72.7%</b>。<br>
    三项底线检查的作用：确保提升召回<b>没有</b>以牺牲版本隔离 / 权限隔离 / 时间过滤为代价。
    详见 <code>docs/lessons.md</code> L20。
  </div>
"""
    else:
        eval_html = ('<h2>评测质量</h2><div class="note">尚无评测报告。'
                     '先运行 <code>python scripts/run_eval.py --save-baseline</code>。</div>')

    # ---------- 数据说明：真实 / 模拟（用户明确要求：模拟自行操作处必须标注）----------
    pr_txt = f"{pr.get('records', 0):,} 条" if pr.get("ok") else "—"
    pr_files = f"{pr.get('files', 0)} 个" if pr.get("ok") else "—"
    pr_pdf = f"{pr.get('pdf_records', 0):,} 条" if pr.get("ok") else "—"

    note_html = f"""
  <h2>数据说明：哪些是真实的，哪些是为演示补足的</h2>
  <div class="grid g2">
    <div class="card" style="border-left:3px solid #16a34a">
      <div class="ml" style="margin-bottom:9px;color:#15803d">① 真实企业数据（未修改）</div>
      <ul class="next">
        <li>资料库 <b>{data['files']:,} 个文件 / {data['mb']/1024:.2f} GB</b> —— 真实企业资料，本机处理、未外传</li>
        <li>知识库 <b>{idx['count']:,} 个切片</b>（产品资料 / 报价单 / 合同 / 投标文件）</li>
        <li>历史报价 <b>{pr_txt}</b>，来自 <b>{pr_files}</b> 真实报价文件（PDF 报价贡献 {pr_pdf}）</li>
        <li>产品型号与技术参数（GKV 系列等）均取自真实产品资料</li>
      </ul>
    </div>
    <div class="card" style="border-left:3px solid #d97706">
      <div class="ml" style="margin-bottom:9px;color:#b45309">② 为演示补足 / 模拟的部分</div>
      <ul class="next">
        <li><b>「客户 → 业务员」权限映射</b>：用的是虚构账号（sales_a / sales_b）。
            真实映射需企业提供，当前只演示「检索前按元数据过滤」的机制本身</li>
        <li><b>评测集 20 题</b>：问题由我依据资料编写，<b>不是真实用户提问</b>；
            但每题标准答案均来自真实资料、逐条核对过</li>
        <li><b>技术规格书参数</b>：本地模型自动抽取，<b>未经技术部门复核</b>，对外前必须人工确认</li>
        <li><b>价格口径</b>：未做汇率折算（跨币种金额直接并列），也未做离群值清洗</li>
      </ul>
    </div>
  </div>
  <div class="note">
    <b>为什么单独说明这一块：</b>被问到「这是真实数据吗」时需要能一句话答清楚 ——
    数据是真的，但<b>权限映射、评测问题、规格书复核</b>这几处是为演示补足的。
    把边界写清楚，比含糊带过更可信。
  </div>
"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>企业知识库 RAG · 建设过程看板</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
background:#f6f8fb;color:#0f172a;font-size:14px;line-height:1.65}}
.wrap{{max-width:1000px;margin:0 auto;padding:0 22px}}
header{{background:#0f172a;color:#e2e8f0;padding:34px 0 28px;margin-bottom:26px}}
header h1{{font-size:23px;color:#fff;font-weight:500}}
header p{{color:#94a3b8;font-size:13px;margin-top:6px}}
h2{{font-size:16px;font-weight:500;margin:30px 0 14px;display:flex;align-items:center;gap:9px}}
h2:before{{content:"";width:4px;height:16px;background:#2563eb;border-radius:2px}}
.grid{{display:grid;gap:12px}}
.g4{{grid-template-columns:repeat(auto-fit,minmax(160px,1fr))}}
.g2{{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}}
.card{{background:#fff;border:1px solid #e6ebf2;border-radius:12px;padding:16px 18px}}
.ml{{font-size:12.5px;color:#64748b}}
.mv{{font-size:22px;font-weight:500;margin-top:2px}}
.step{{display:flex;gap:11px;align-items:flex-start;background:#fff;border:1px solid #e6ebf2;
border-radius:10px;padding:12px 14px}}
.step .sm{{width:20px;height:20px;border-radius:50%;display:flex;align-items:center;justify-content:center;
font-size:12px;flex:0 0 20px;margin-top:1px}}
.step.ok .sm{{background:#EAF3DE;color:#3B6D11}}
.step.wip .sm{{background:#FAEEDA;color:#854F0B}}
.step.todo .sm{{background:#f1f5f9;color:#94a3b8}}
.step .sn{{font-size:13.5px;font-weight:500}}
.step .sd{{font-size:12.5px;color:#64748b}}
.tl-item{{display:flex;gap:12px;padding:10px 0;border-bottom:1px solid #eef2f7}}
.tl-item:last-child{{border-bottom:none}}
.tl-tag{{flex:0 0 42px;height:22px;border-radius:20px;background:#eef2f7;color:#475569;font-size:12px;
display:flex;align-items:center;justify-content:center}}
.tl-t{{font-size:13.5px;font-weight:500}}
.tl-d{{font-size:12.5px;color:#64748b}}
.tl-l{{display:inline-block;margin-top:4px;font-size:11.5px;background:#E6F1FB;color:#185FA5;
padding:1px 8px;border-radius:20px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid #eef2f7}}
th{{color:#64748b;font-weight:500;font-size:12.5px;background:#f8fafc}}
.bar{{display:flex;align-items:center;gap:10px;margin-bottom:8px}}
.bl{{width:96px;font-size:12.5px;color:#334155}}
.bt{{flex:1;height:7px;background:#f1f5f9;border-radius:20px;overflow:hidden}}
.bt i{{display:block;height:7px}}
.bv{{width:44px;text-align:right;font-size:12px;color:#64748b}}
ul{{padding-left:18px}} li{{margin-bottom:6px;font-size:13.5px}}
.next li{{color:#334155}}
footer{{color:#94a3b8;font-size:12.5px;padding:30px 0 50px;text-align:center}}
.note{{font-size:12.5px;color:#64748b;background:#f8fafc;border-left:3px solid #cbd5e1;
padding:8px 14px;border-radius:0 8px 8px 0;margin-top:12px}}
.lsd{{background:#fff;border:1px solid #e6ebf2;border-radius:10px;padding:11px 15px;margin-bottom:9px}}
.lsd summary{{cursor:pointer;font-size:13.5px;color:#0f172a;list-style:none;font-weight:500}}
.lsd summary::-webkit-details-marker{{display:none}}
.lsd summary:before{{content:"▸";color:#94a3b8;margin-right:7px;display:inline-block;transition:transform .15s}}
.lsd[open] summary:before{{transform:rotate(90deg)}}
.lsb{{margin-top:10px;padding-left:17px;border-left:2px solid #e6ebf2}}
.lsb p{{font-size:13px;color:#334155;margin:7px 0}}
.lsb ul{{margin:6px 0}}
.lstab{{width:100%;border-collapse:collapse;font-size:12.5px;margin:9px 0}}
.lstab th{{background:#f8fafc;color:#64748b;font-weight:500;text-align:left;padding:6px 9px;
border-bottom:1px solid #eef2f7;white-space:nowrap;width:1%}}
.lstab td{{padding:6px 9px;border-bottom:1px solid #eef2f7;color:#334155;vertical-align:top}}
code{{background:#f1f5f9;padding:1px 5px;border-radius:4px;font-size:12px;
font-family:Consolas,Monaco,monospace;color:#B03A2E}}
</style></head><body>
<header><div class="wrap">
  <h1>企业知识库 + RAG 系统 · 建设过程看板</h1>
  <p>资料规模、流水线进度、排查历程与复盘教训 —— 数据每生成一次自动刷新　|　更新于 {ts}</p>
</div></header>
<div class="wrap">

  <h2>总览</h2>
  <div class="grid g4">
    <div class="card"><div class="ml">资料文件</div><div class="mv">{data['files']:,}</div></div>
    <div class="card"><div class="ml">资料体积</div><div class="mv">{data['mb']/1024:.2f} GB</div></div>
    <div class="card"><div class="ml">已入库切片</div><div class="mv">{idx['count']:,}</div></div>
    <div class="card"><div class="ml">复盘教训</div><div class="mv">{len(lessons)} 条</div></div>
  </div>

  <h2>流水线进度</h2>
  <div class="grid g2">{steps}</div>
  <div class="note">✓ 已完成　◐ 进行中　○ 待办　·　标 <b>⑦</b> 的是当前正在做的事</div>

  <h2>排查历程（这一段是真过程）</h2>
  <div class="card">{tl}</div>

  <h2>复盘教训（{len(lessons)} 条 · 点标题可折叠）</h2>
  <div>{less}</div>

  <h2>向量库现状</h2>
  <div class="grid g2">
    <div class="card">
      <div class="ml" style="margin-bottom:10px">按密级分布（{idx['count']} 个切片）</div>
      {sens_bars}
    </div>
    <div class="card">
      <div class="ml" style="margin-bottom:8px">按来源目录</div>
      <table><thead><tr><th>目录</th><th>切片数</th></tr></thead><tbody>{idx_rows}</tbody></table>
    </div>
  </div>

  {age_html}

  {eval_html}

  <h2>资料构成</h2>
  <div class="grid g2">
    <div class="card">
      <div class="ml" style="margin-bottom:8px">按文件类型</div>
      <table><thead><tr><th>类型</th><th>数量</th><th>体积</th></tr></thead><tbody>{ext_rows}</tbody></table>
    </div>
    <div class="card">
      <div class="ml" style="margin-bottom:8px">按顶层目录</div>
      <table><thead><tr><th>目录</th><th>文件数</th></tr></thead><tbody>{top_rows}</tbody></table>
    </div>
  </div>

  <h2>下一步</h2>
  <div class="card"><ul class="next">{nexts}</ul></div>

  {note_html}

  <footer>机密资料仅在本机处理，本页面不含任何客户名称 / 报价 / 合同内容。</footer>
</div></body></html>"""

    # 对外发布前统一脱敏（公司名 / 业务员 / 客户地区 / 资料路径）
    return mask(html)


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    data = scan_data()
    idx = scan_index()
    lessons = scan_lessons()
    age = scan_age()
    ev = scan_eval()
    pr = scan_price()
    OUT.write_text(build_html(data, idx, lessons, age, ev, pr), encoding="utf-8")
    print(f"看板已生成：{OUT}")
    print(f"  资料 {data['files']} 文件 / {data['mb']/1024:.2f} GB")
    print(f"  向量库 {idx['count']} 切片（{'读取成功' if idx['ok'] else '未读取到'}）")
    print(f"  复盘教训 {len(lessons)} 条")


if __name__ == "__main__":
    main()
