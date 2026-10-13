r"""
限时跑全量入库 —— 到点自动停，能跑多少算多少。

为什么要这个脚本？
  · 全量 1307 个文件跑不完，需要"到点就收手"；
  · 子进程是多进程池，直接杀父进程会留下**僵尸子进程**（之前吃过亏），
    所以必须用 taskkill /T 杀整棵进程树；
  · 中断不会丢进度：OCR 结果有缓存、向量库是增量的，下次重跑接着来。

用法：
    python scripts/run_timed.py --minutes 120 --workers 6
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
PY = sys.executable
LOG = PROJECT / "data" / "full_index_run.log"


def kill_tree(pid: int) -> None:
    """杀掉整棵进程树（含多进程池里的子进程），避免僵尸残留。"""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=30)
    except Exception as e:
        print(f"  [警告] 杀进程树失败：{e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=120, help="最多跑多少分钟")
    ap.add_argument("--workers", type=int, default=6, help="并行进程数")
    ap.add_argument("--dir", default=r"D:/RAG资料", help="要入库的目录")
    args = ap.parse_args()

    LOG.parent.mkdir(parents=True, exist_ok=True)
    log = open(LOG, "w", encoding="utf-8", buffering=1)

    cmd = [PY, "src/ingest/build_index.py",
           "--dir", args.dir, "--workers", str(args.workers)]
    # ⚠️ 故意不加 --rebuild：保住已经入库的部分，中断可续

    limit = args.minutes * 60
    print(f"限时全量入库｜最多 {args.minutes} 分钟｜{args.workers} 进程")
    print(f"目录：{args.dir}")
    print(f"日志：{LOG}")
    print("=" * 62)
    log.write(f"# 限时全量入库  start={time.strftime('%Y-%m-%d %H:%M:%S')} "
              f"limit={args.minutes}min workers={args.workers}\n")

    t0 = time.time()
    # ⚠️ PYTHONUNBUFFERED=1 必须加：子进程的 stdout 接的是管道（不是终端），
    # Python 会默认切成"块缓冲"，日志要攒够好几 KB 才落盘。
    # 表现就是"跑了十几分钟日志一动不动"，极容易被误判成卡死（真实踩过）。
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        cmd, cwd=str(PROJECT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
                log.write(line)
            except Exception:
                pass

    threading.Thread(target=pump, daemon=True).start()

    timed_out = False
    while proc.poll() is None:
        time.sleep(5)
        if time.time() - t0 > limit:
            timed_out = True
            print(f"\n⏰ 已到 {args.minutes} 分钟上限，正在停止…（进度已保存）")
            kill_tree(proc.pid)
            time.sleep(3)
            kill_tree(proc.pid)          # 再杀一次，确保子进程没漏网
            break

    try:
        proc.wait(timeout=30)
    except Exception:
        kill_tree(proc.pid)

    dt = time.time() - t0
    tail = (f"\n# 结束 {'(到点停止)' if timed_out else '(正常跑完)'} "
            f"用时 {dt/60:.1f} 分钟  exit={proc.returncode}\n")
    log.write(tail)
    print(tail)
    print(f"耗时 {dt/60:.1f} 分钟")


if __name__ == "__main__":
    main()
