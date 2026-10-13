r"""
知识库 · 本地控制面板

为什么做这个？用户不熟悉命令行、也不想学 VS Code。
双击桌面的「知识库控制面板.bat」→ 浏览器自动打开 → 点按钮 → 实时看输出。

安全说明：
  · 只在 127.0.0.1 上监听，局域网/外网都访问不到；
  · 只执行本文件里写死的几条命令，不接受任意命令；
  · 机密资料始终在本机处理，不上传。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
PANEL_DIR = Path(__file__).resolve().parent
PY = sys.executable
DOC_DIR = r"D:/RAG资料/产品资料"
BOARD = PROJECT / "dashboard" / "index.html"

PORT = 8777

# ---------------------------------------------------------------- 任务清单
# 只有这里列出来的命令能被触发，不接收任意命令（安全考虑）
TASKS: dict[str, dict] = {
    "env": {
        "name": "环境自检",
        "desc": "检查 Python / GPU / Ollama / 资料目录",
        "args": ["scripts/00_check_env.py"],
    },
    "parse": {
        "name": "解析预览",
        "desc": "看前 20 个文件能不能抽出文字、哪些需要 OCR",
        "args": ["src/ingest/parse.py", "--dir", DOC_DIR, "--limit", "20"],
    },
    "ocr": {
        "name": "OCR 单文件测试",
        "desc": "拿球阀 PDF 试一下，看能认出多少参数",
        "args": ["src/ingest/ocr.py", "--file", DOC_DIR + "/Ball Valve(2).pdf", "--show", "300"],
    },
    "index_small": {
        "name": "入库 · 小样本（8 个文件）",
        "desc": "安全试跑，验证流程通不通",
        "args": ["src/ingest/build_index.py", "--dir", DOC_DIR, "--limit", "8", "--workers", "6"],
    },
    "index_all": {
        "name": "入库 · 全量（增量续跑）",
        "desc": "自动跳过已入库文件；中断后重跑即可续上",
        "warn": True,
        "args": ["src/ingest/build_index.py", "--dir", DOC_DIR, "--workers", "4"],
    },
    "age": {
        "name": "时效诊断（版本 / 过期 / 冲突）",
        "desc": "扫描过期文档、版本冲突、重复副本，刷新健康分",
        "args": ["scripts/age_report.py", "--top", "10"],
    },
    "backfill": {
        "name": "回填 / 应用裁决",
        "desc": "补日期版本状态；若存在裁决文件则按你的裁决刷新",
        "args": ["scripts/backfill_time_meta.py"],
    },
    "review": {
        "name": "生成版本裁决表",
        "desc": "生成可点选的冲突裁决页面（dashboard/review.html）",
        "args": ["scripts/build_review_sheet.py"],
    },
    "eval": {
        "name": "跑评测（20 题）",
        "desc": "检索召回 + 3 项系统底线检查，自动与基线对比出涨跌",
        "args": ["scripts/run_eval.py", "--show-fail"],
    },
    "quote": {
        "name": "生成参考报价表",
        "desc": "读客户询价单 → 匹配历史报价（文件名可在下面指令里改）",
        "args": ["src/quote/make_quote.py",
                 "报价单/26050910-该企业-某地区/Q260509-10_Vanko.xlsx"],
    },
    "spec": {
        "name": "生成技术规格书",
        "desc": "按参考报价表里的型号，从产品资料里抽参数",
        "args": ["src/quote/make_spec.py", "--prices",
                 "data/参考报价_Q260509-10_Vanko.xlsx"],
    },
    "ask": {
        "name": "知识库问答",
        "desc": "在下面输入问题；默认只检索「当前有效」的版本",
        "input": True,
        "args": ["src/search/ask.py"],
    },
    "dashboard": {
        "name": "重新生成看板",
        "desc": "重新读取项目数据，刷新进度看板",
        "args": ["scripts/build_dashboard.py"],
    },
    "open_board": {
        "name": "打开进度看板",
        "desc": "查看流水线进度与复盘教训",
        "special": "board",
    },
    "open_folder": {
        "name": "打开项目文件夹",
        "desc": "用资源管理器打开 D:\rag-system",
        "special": "folder",
    },
}

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
MAX_OUT = 300_000


def run_job(job_id: str, args: list[str]) -> None:
    """在后台线程跑任务，把输出累积到 JOBS[job_id]['out']。"""
    try:
        proc = subprocess.Popen(
            [PY] + args, cwd=str(PROJECT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except Exception as e:
        with LOCK:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["out"] += f"\n[启动失败] {type(e).__name__}: {e}\n"
        return

    assert proc.stdout is not None
    for line in proc.stdout:
        with LOCK:
            j = JOBS.get(job_id)
            if not j:
                break
            j["out"] += line
            if len(j["out"]) > MAX_OUT:
                j["out"] = "…（前面输出已省略）…\n" + j["out"][-MAX_OUT // 2:]
    proc.wait()
    with LOCK:
        if job_id in JOBS:
            JOBS[job_id]["status"] = "done" if proc.returncode == 0 else "failed"
            JOBS[job_id]["code"] = proc.returncode
            JOBS[job_id]["ended"] = time.time()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a) -> None:   # 别把日志刷到控制台
        pass

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            f = PANEL_DIR / "index.html"
            self._send(200, f.read_bytes() if f.exists() else b"panel missing")
        elif path == "/board":
            self._send(200, BOARD.read_bytes() if BOARD.exists() else b"board missing")
        elif path == "/api/tasks":
            self._json(TASKS)
        elif path == "/api/jobs":
            with LOCK:
                self._json({k: dict(v) for k, v in JOBS.items()})
        else:
            self._send(404, b"not found")

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else "{}"
        try:
            req = json.loads(raw)
        except Exception:
            req = {}

        path = self.path.split("?")[0]
        if path == "/api/clear":
            with LOCK:
                JOBS.clear()
            self._json({"ok": True})
            return

        if path != "/api/run":
            self._send(404, b"not found")
            return

        tid = req.get("task")
        t = TASKS.get(str(tid))
        if not t:
            self._json({"error": "unknown task"}, 400)
            return

        # 特殊任务：只做本地动作，不跑命令
        sp = t.get("special")
        if sp == "folder":
            try:
                os.startfile(str(PROJECT))          # noqa: S606
            except Exception as e:
                self._json({"error": str(e)}, 500)
                return
            self._json({"ok": True, "note": "已打开项目文件夹"})
            return
        if sp == "board":
            webbrowser.open(f"http://127.0.0.1:{PORT}/board")
            self._json({"ok": True, "note": "已在浏览器打开看板"})
            return

        args = list(t["args"])
        if t.get("input"):
            q = (req.get("question") or "").strip()
            if not q:
                q = "球阀的工作压力和温度范围是多少？适用哪些标准？"
            args.append(q)

        jid = uuid.uuid4().hex[:8]
        with LOCK:
            JOBS[jid] = {"task": tid, "name": t["name"], "out": "",
                         "status": "running", "started": time.time()}
        threading.Thread(target=run_job, args=(jid, args), daemon=True).start()
        self._json({"job": jid})


def main() -> None:
    global PORT
    for p in range(PORT, PORT + 12):          # 端口被占就顺延
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            PORT = p
            break
        except OSError:
            continue
    else:
        print("找不到可用端口，请关闭其他程序后重试")
        return

    url = f"http://127.0.0.1:{PORT}/"
    print("=" * 56)
    print("  知识库 · 本地控制面板已启动")
    print(f"  地址：{url}")
    print("  浏览器会自动打开；关掉这个窗口即停止服务。")
    print("=" * 56)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
