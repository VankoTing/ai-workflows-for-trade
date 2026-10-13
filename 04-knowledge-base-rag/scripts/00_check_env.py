"""
环境自检 —— 开工前先跑这个，确认环境齐了。

用法：
    python scripts/00_check_env.py
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys

OK = "[ OK ]"
NO = "[MISS]"
WARN = "[WARN]"

DATA_DIR = r"D:\RAG资料"

PACKAGES = {
    "ollama": "ollama",
    "chromadb": "chromadb",
    "pypdf": "pypdf",
    "docx": "python-docx",
    "openpyxl": "openpyxl",
    "pandas": "pandas",
    "yaml": "pyyaml",
}


def line(title: str) -> None:
    print("\n" + "=" * 58)
    print(title)
    print("=" * 58)


def check_python() -> None:
    line("1. Python")
    v = sys.version_info
    print(f"  版本 : {sys.version.split()[0]}   ({'OK' if v >= (3, 10) else '建议 3.10+'})")
    print(f"  解释器: {sys.executable}")


def check_packages() -> None:
    line("2. Python 依赖")
    missing = []
    for mod, pipname in PACKAGES.items():
        try:
            importlib.import_module(mod)
            print(f"  {OK}  {pipname}")
        except Exception:
            missing.append(pipname)
            print(f"  {NO}  {pipname}")
    if missing:
        print("\n  缺依赖，执行：pip install -r requirements.txt")
    return missing


def check_ollama() -> None:
    line("3. Ollama（本地模型运行时）")
    exe = shutil.which("ollama")
    if not exe:
        print(f"  {NO}  未安装 Ollama")
        print("       下载：https://ollama.com/download/windows")
        return
    print(f"  {OK}  已安装：{exe}")
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=20)
        print("  已下载模型：")
        for ln in (out.stdout or "").strip().splitlines():
            print("    " + ln)
    except Exception as e:
        print(f"  {WARN}  无法列出模型：{e}")


def check_gpu() -> None:
    line("4. GPU")
    smi = shutil.which("nvidia-smi")
    if not smi:
        print(f"  {NO}  未检测到 NVIDIA 显卡（将只能用 CPU，速度会慢很多）")
        return
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=20,
        )
        print(f"  {OK}  " + (out.stdout or "").strip())
    except Exception as e:
        print(f"  {WARN}  nvidia-smi 调用失败：{e}")


def check_data() -> None:
    line("5. 资料目录")
    if os.path.isdir(DATA_DIR):
        n, total = 0, 0
        for dp, _, fn in os.walk(DATA_DIR):
            for f in fn:
                n += 1
                try:
                    total += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        print(f"  {OK}  {DATA_DIR}")
        print(f"       {n} 个文件 / {total / 1024 / 1024:.0f} MB")
        print("       ⚠️  该目录为机密资料，禁止复制进本项目、禁止上传")
    else:
        print(f"  {NO}  找不到 {DATA_DIR}")


def main() -> None:
    print("RAG 系统 · 环境自检")
    check_python()
    missing = check_packages()
    check_ollama()
    check_gpu()
    check_data()

    line("结论")
    if missing:
        print("  ⏳ 还差 Python 依赖，先跑：pip install -r requirements.txt")
    elif not shutil.which("ollama"):
        print("  ⏳ 还差 Ollama（需你手动安装），装完再跑一次本脚本")
    else:
        print("  ✅ 环境齐了，可以开始阶段一")


if __name__ == "__main__":
    main()
