"""
生成「版本冲突裁决表」—— 一份可以在浏览器里点选的本地页面。

背景（docs/versioning.md 第五节）：
    "哪一版作数" 是**业务决策**，程序不擅自判死。
    自动判定只处理"铁证"情形（内容不同 + 日期可信 + 相差 ≥30 天），
    其余冲突全部列出来，交给业务确认。

用法：
    python scripts/build_review_sheet.py
输出：
    dashboard/review.html

    ⚠️ 此页面含真实客户路径，**只在本机使用，不要外传/不要部署到公网**。
    在浏览器里逐条确认后，点「导出裁决结果」得到 version_decisions.json，
    放到 data/ 目录即可被系统读取并遵守。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from src.config import DATA_ROOT  # noqa: E402

REPORT = PROJECT / "data" / "age_report.json"
OUT = PROJECT / "dashboard" / "review.html"

TRUSTED = {"dir", "filename", "filename-year", "filename-range"}


def enrich(members: list[dict]) -> list[dict]:
    """补上文件大小/系统时间，方便人判断。"""
    root = Path(DATA_ROOT)
    out = []
    for m in members:
        p = root / m["source"]
        size_kb, mtime = 0.0, ""
        try:
            st = p.stat()
            size_kb = round(st.st_size / 1024, 1)
            mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
        except OSError:
            pass
        out.append({
            "source": m["source"],
            "name": Path(m["source"]).name,
            "dir": str(Path(m["source"]).parent).replace("\\", "/"),
            "doc_date": m["doc_date"],
            "src": m["src"],
            "size_kb": size_kb,
            "mtime": mtime,
        })
    return out


def classify(members: list[dict]) -> tuple[str, str]:
    """
    返回 (优先级, 系统建议保留的 source)。

    优先级：
      high   成员里至少一份日期来源可信（目录名/文件名）→ 最可能是真版本关系
      medium 全是 mtime，但有明显日期差
      low    日期几乎相同 → 只能靠人看内容
    """
    srt = sorted(members, key=lambda x: (-x["doc_date"], x["source"]))
    suggest = srt[0]["source"] if srt else ""

    if any(m["src"] in TRUSTED for m in members):
        return "high", suggest
    dates = {m["doc_date"] for m in members}
    if len(dates) > 1 and (max(dates) - min(dates)) > 0:
        return "medium", suggest
    return "low", suggest


def main() -> None:
    if not REPORT.exists():
        print(f"缺少 {REPORT}，先运行：python scripts/age_report.py")
        return
    rep = json.loads(REPORT.read_text(encoding="utf-8"))

    fams = []
    for c in rep.get("conflicts", []):
        members = enrich(c["members"])
        prio, suggest = classify(members)
        members.sort(key=lambda x: (-x["doc_date"], x["source"]))
        fams.append({
            "family": c["family"],
            "count": c["count"],
            "priority": prio,
            "suggest": suggest,
            "members": members,
        })

    order = {"high": 0, "medium": 1, "low": 2}
    fams.sort(key=lambda x: (order[x["priority"]], -x["count"], x["family"]))

    stat = {
        "total": len(fams),
        "high": sum(1 for f in fams if f["priority"] == "high"),
        "medium": sum(1 for f in fams if f["priority"] == "medium"),
        "low": sum(1 for f in fams if f["priority"] == "low"),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    data = json.dumps({"stat": stat, "fams": fams}, ensure_ascii=False)
    html = TEMPLATE.replace("__DATA__", data.replace("</", "<\\/"))

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"裁决表已生成：{OUT}")
    print(f"  共 {stat['total']} 个家族"
          f"（高优先 {stat['high']} / 中 {stat['medium']} / 低 {stat['low']}）")
    print("  用浏览器打开它，逐条确认后点「导出裁决结果」")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>版本冲突裁决表</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
background:#f6f8fb;color:#0f172a;font-size:14px;line-height:1.6}
header{background:#0f172a;color:#e2e8f0;padding:26px 0 22px}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px}
header h1{font-size:21px;color:#fff;font-weight:500}
header p{color:#94a3b8;font-size:13px;margin-top:5px}
.toolbar{position:sticky;top:0;z-index:9;background:#fff;border-bottom:1px solid #e6ebf2;
padding:12px 0;margin-bottom:18px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
button{font:inherit;font-size:13px;padding:7px 14px;border-radius:8px;border:1px solid #cbd5e1;
background:#fff;color:#334155;cursor:pointer}
button:hover{background:#f1f5f9}
button.primary{background:#2563eb;border-color:#2563eb;color:#fff}
button.primary:hover{background:#1d4ed8}
.stat{color:#64748b;font-size:12.5px;margin-left:auto}
.fam{background:#fff;border:1px solid #e6ebf2;border-radius:10px;padding:14px 16px;margin-bottom:12px}
.fam.hide{display:none}
.fh{display:flex;align-items:center;gap:9px;margin-bottom:4px}
.fh b{font-size:14.5px;font-weight:500}
.tag{font-size:11px;padding:1px 7px;border-radius:5px;border:1px solid}
.t-high{color:#b45309;border-color:#fcd34d;background:#fffbeb}
.t-medium{color:#0369a1;border-color:#7dd3fc;background:#f0f9ff}
.t-low{color:#64748b;border-color:#cbd5e1;background:#f8fafc}
.fmeta{color:#94a3b8;font-size:12px;margin-bottom:9px}
.row{display:flex;gap:10px;align-items:flex-start;padding:7px 9px;border-radius:8px;
border:1px solid #eef2f7;margin-bottom:6px;cursor:pointer}
.row:hover{background:#f8fafc}
.row.sel{background:#eff6ff;border-color:#bfdbfe}
.row input{margin-top:4px;flex:0 0 auto}
.rmain{flex:1;min-width:0}
.rline1{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.rdate{font-family:Consolas,Monaco,monospace;font-size:12.5px;color:#0f172a;font-weight:600}
.rsrc{font-size:11px;color:#94a3b8}
.rsug{font-size:11px;color:#2563eb;border:1px solid #bfdbfe;background:#eff6ff;
padding:0 6px;border-radius:5px}
.rpath{font-size:12px;color:#475569;word-break:break-all;margin-top:2px}
.rsub{font-size:11.5px;color:#94a3b8;margin-top:1px}
.special{background:#fafafa}
#out{white-space:pre-wrap;font-size:11.5px;font-family:Consolas,Monaco,monospace;
background:#0f172a;color:#a5f3fc;padding:12px;border-radius:8px;margin-top:14px;display:none;
max-height:340px;overflow:auto}
footer{color:#94a3b8;font-size:12px;padding:24px 0 46px;text-align:center}
</style></head><body>
<header><div class="wrap">
  <h1>版本冲突裁决表</h1>
  <p>同一份资料出现了多个版本，需要确认「哪一份作数」。系统已预选建议项，你只需确认或修改。
     本页面只在本机使用，含真实客户路径，<b>请勿外传</b>。</p>
</div></header>
<div class="wrap">
  <div class="toolbar">
    <button class="primary" id="adopt">全部采纳系统建议</button>
    <button id="fHigh">只看高优先</button>
    <button id="fAll">看全部</button>
    <button id="export">导出裁决结果</button>
    <span class="stat" id="stat"></span>
  </div>
  <div id="list"></div>
  <pre id="out"></pre>
  <footer>确认完成后点「导出裁决结果」，把下载到的 <b>version_decisions.json</b>
   放到 <b>D:\rag-system\data\</b> 目录，系统即会遵守你的裁决。</footer>
</div>
<script>
const DATA = __DATA__;
const list = document.getElementById('list');

function render(onlyHigh){
  list.innerHTML = '';
  DATA.fams.forEach((f, i) => {
    if (onlyHigh && f.priority !== 'high') return;
    const d = document.createElement('div');
    d.className = 'fam';
    d.dataset.family = f.family;
    const tname = {high:'高优先', medium:'中', low:'低'}[f.priority];
    let rows = '';
    f.members.forEach(m => {
      const sug = (m.source === f.suggest) ? '<span class="rsug">系统建议</span>' : '';
      rows += `<label class="row" data-v="${encodeURIComponent(m.source)}">
        <input type="radio" name="f${i}" value="${encodeURIComponent(m.source)}"
               data-action="keep_one"${m.source === f.suggest ? ' checked' : ''}>
        <span class="rmain">
          <span class="rline1"><span class="rdate">${m.doc_date}</span>
            <span class="rsrc">日期来源:${m.src}</span>${sug}</span>
          <span class="rpath">${m.source}</span>
          <span class="rsub">${m.size_kb} KB　系统时间 ${m.mtime}</span>
        </span></label>`;
    });
    rows += `<label class="row special"><input type="radio" name="f${i}" value="__all__"
        data-action="keep_all"><span class="rmain">全部保留（它们各自都有用，不是版本关系）</span></label>`;
    rows += `<label class="row special"><input type="radio" name="f${i}" value="__skip__"
        data-action="skip"><span class="rmain">先跳过（暂不裁决，保持现状）</span></label>`;

    d.innerHTML = `<div class="fh"><b>${f.family}</b>
        <span class="tag t-${f.priority}">${tname}</span>
        <span class="rsub">共 ${f.count} 份</span></div>
      <div class="fmeta">请选择应该作数的那一份：</div>${rows}`;
    list.appendChild(d);
  });
  bindRows();
  updateStat();
}

function bindRows(){
  document.querySelectorAll('.row').forEach(r=>{
    r.addEventListener('click', ()=>{
      const inp = r.querySelector('input');
      if(inp){ inp.checked = true; }
      const fam = r.closest('.fam');
      fam.querySelectorAll('.row').forEach(x=>x.classList.remove('sel'));
      r.classList.add('sel');
      setTimeout(updateStat, 0);
    });
  });
  document.querySelectorAll('.fam').forEach(f=>{
    const c = f.querySelector('.row.sel');
    if(!c){
      const chk = f.querySelector('input:checked');
      if(chk) chk.closest('.row').classList.add('sel');
    }
  });
}

function collect(){
  const out = {};
  document.querySelectorAll('.fam').forEach(f=>{
    const chk = f.querySelector('input[type=radio]:checked');
    if(!chk) return;
    const v = decodeURIComponent(chk.value);
    if(v === '__skip__') return;
    out[f.dataset.family] = {keep: (v === '__all__' ? '__all__' : v),
                             action: chk.dataset.action};
  });
  return out;
}

function updateStat(){
  const n = Object.keys(collect()).length;
  document.getElementById('stat').textContent = `已裁决 ${n} / ${DATA.fams.length}`;
}

document.getElementById('adopt').onclick = ()=>{
  document.querySelectorAll('.fam').forEach(f=>{
    const fam = DATA.fams.find(x=>x.family === f.dataset.family);
    if(!fam) return;
    const labels = f.querySelectorAll('.row');
    labels.forEach(l=>{
      const inp = l.querySelector('input');
      if(inp && decodeURIComponent(inp.value) === fam.suggest){
        inp.checked = true;
        labels.forEach(x=>x.classList.remove('sel'));
        l.classList.add('sel');
      }
    });
  });
  updateStat();
};
document.getElementById('fHigh').onclick = ()=>render(true);
document.getElementById('fAll').onclick = ()=>render(false);
document.getElementById('export').onclick = ()=>{
  const out = {generated:new Date().toLocaleString('sv').slice(0,16), decisions:collect()};
  const txt = JSON.stringify(out, null, 1);
  const blob = new Blob([txt], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'version_decisions.json';
  a.click();
  const pre = document.getElementById('out');
  pre.style.display = 'block';
  pre.textContent = txt;
  updateStat();
};

render(false);
</script>
</body></html>
"""


if __name__ == "__main__":
    main()
