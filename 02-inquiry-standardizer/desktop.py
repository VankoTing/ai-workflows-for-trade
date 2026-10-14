"""阀门询价整理 · 桌面版

双击运行后会在本机起一个小服务，并自动打开浏览器作为操作界面。
**文件全部在本机处理**，只有要交给模型分析的文字会经过网络。

打包成 exe 用：
    pyinstaller --clean desktop.spec
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

# --------------------------------------------------------------------------
# 路径：打包后 exe 所在目录 / 未打包时的项目目录
# --------------------------------------------------------------------------

FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    APP_DIR = Path(sys.executable).resolve().parent
    RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
else:
    APP_DIR = Path(__file__).resolve().parent
    RES_DIR = APP_DIR

SRC_DIR = (RES_DIR / "src") if FROZEN else (APP_DIR / "src")
WEB_DIR = (RES_DIR / "web") if FROZEN else (APP_DIR / "web")
GLOSSARY_SRC = RES_DIR / "glossary"
GLOSSARY_DIR = APP_DIR / "glossary"
WORK_DIR = APP_DIR / "工作区"
INPUT_DIR = WORK_DIR / "输入"
OUTPUT_DIR = WORK_DIR / "输出"

for d in (WORK_DIR, INPUT_DIR, OUTPUT_DIR, GLOSSARY_DIR):
    d.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(SRC_DIR))

# 术语库要可写（AI 补译会往里加），所以放在 exe 同目录而不是打包资源里
os.environ["VALVE_GLOSSARY_DIR"] = str(GLOSSARY_DIR)
if GLOSSARY_SRC.exists() and not (GLOSSARY_DIR / "terms.json").exists():
    for f in GLOSSARY_SRC.glob("*"):
        try:
            (GLOSSARY_DIR / f.name).write_bytes(f.read_bytes())
        except OSError:
            pass

# 这里显式导入一次，是为了让 PyInstaller 静态分析能顺着找到 src/ 下的模块，
# 进而把 openpyxl / python-docx / pypdf 这些第三方依赖一起打包进去。
# 放在「术语库环境变量」之后导入 —— terms.py 在模块加载时就要读那个变量。
try:
    import pipeline  # noqa: F401
except Exception:  # noqa: BLE001,S110
    pass

# --------------------------------------------------------------------------
# 配置（API Key）
# --------------------------------------------------------------------------

CONFIG_DIR = Path(os.getenv("APPDATA") or str(APP_DIR)) / "ValveInquiry"
CONFIG_FILE = CONFIG_DIR / "config.json"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    # 打包时同目录若带了 .env，也认
    env_file = APP_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DEEPSEEK_API_KEY="):
                return {"api_key": line.split("=", 1)[1].strip()}
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_config() -> None:
    cfg = load_config()
    key = (cfg.get("api_key") or "").strip()
    if key:
        os.environ["DEEPSEEK_API_KEY"] = key


apply_config()

app = FastAPI(title="阀门询价整理")


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------

def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/")
async def index() -> HTMLResponse:
    page = WEB_DIR / "index.html"
    if not page.exists():
        return HTMLResponse("<h1>界面文件缺失</h1>", status_code=500)
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/api/status")
async def status() -> JSONResponse:
    cfg = load_config()
    return JSONResponse({
        "has_key": bool((cfg.get("api_key") or "").strip()),
        "app_dir": str(APP_DIR),
        "output_dir": str(OUTPUT_DIR),
    })


@app.post("/api/setup")
async def setup(api_key: str = Form(...)) -> JSONResponse:
    key = api_key.strip()
    if not key.startswith("sk-"):
        return JSONResponse({"ok": False, "msg": "Key 格式不对，应该以 sk- 开头"}, status_code=400)
    cfg = load_config()
    cfg["api_key"] = key
    save_config(cfg)
    os.environ["DEEPSEEK_API_KEY"] = key
    return JSONResponse({"ok": True})


@app.post("/api/process")
async def process(file: UploadFile = File(...), hint: str = Form("")):
    async def gen():
        cfg = load_config()
        if not (cfg.get("api_key") or "").strip():
            yield sse("error", {"text": "还没有设置 API Key，请先点右上角「设置」填写。"})
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = Path(file.filename or "input.xlsx").name
        src = INPUT_DIR / f"{stamp}_{safe_name}"
        try:
            src.write_bytes(await file.read())
        except OSError as exc:
            yield sse("error", {"text": f"保存文件失败：{exc}"})
            return
        yield sse("stage", {"text": f"已接收文件：{safe_name}", "pct": 15})

        import pipeline  # noqa: PLC0415

        out_path = OUTPUT_DIR / f"{Path(safe_name).stem}_中英对照_{stamp}.xlsx"
        yield sse("stage", {"text": "正在调用 AI 抽取标准字段与技术审查…（通常 20–60 秒）", "pct": 35})
        try:
            result = await pipeline.run([src], out_path, hint=hint)
        except Exception as exc:  # noqa: BLE001
            yield sse("error", {"text": f"处理出错：{exc}"})
            return

        if not result:
            yield sse("error", {"text": "处理失败。请确认文件能正常打开，或 API Key 是否有效。"})
            return

        yield sse("stage", {"text": "正在生成表格…", "pct": 90})
        yield sse("done", {
            "text": f"完成：{result.name}",
            "file": result.name,
            "out_dir": str(result.parent),
        })

    return StreamingResponse(
        gen(),
        media_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.get("/api/download/{name}")
async def download(name: str) -> FileResponse:
    safe = Path(name).name
    path = OUTPUT_DIR / safe
    if not path.exists():
        return JSONResponse({"msg": "文件不存在"}, status_code=404)
    return FileResponse(path, filename=safe, media_type="application/octet-stream")


@app.post("/api/open-folder")
async def open_folder() -> JSONResponse:
    """在资源管理器里打开输出目录。"""
    try:
        if sys.platform == "win32":
            os.startfile(str(OUTPUT_DIR))  # noqa: S606
        elif sys.platform == "darwin":
            os.system(f'open "{OUTPUT_DIR}"')
        else:
            os.system(f'xdg-open "{OUTPUT_DIR}"')
        return JSONResponse({"ok": True})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "msg": str(exc)})


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    # 固定端口便于调试：set VALVE_PORT=8765
    port = int(os.getenv("VALVE_PORT") or free_port())
    url = f"http://127.0.0.1:{port}/"
    print("=" * 56)
    print("  阀门询价整理 已启动")
    print(f"  界面地址：{url}")
    print(f"  输出目录：{OUTPUT_DIR}")
    print("  关掉这个窗口即可退出程序")
    print("=" * 56)

    if os.getenv("VALVE_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
