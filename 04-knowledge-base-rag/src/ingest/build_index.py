r"""
入库流水线：解析 → 切片 → 向量化 → 存入 Chroma（带元数据）。

用法：
    python src/ingest/build_index.py --dir "D:/RAG资料/产品资料" --limit 30
    python src/ingest/build_index.py --dir "D:/RAG资料/产品资料" --rebuild

关键点：**每个切片入库时都打上元数据标签**（来源 / 密级 / 负责人 / 项目）。
这些标签就是后面「权限过滤」的依据 —— 检索时先按标签筛，看不到的资料根本不会参与检索。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# ⚠️ chromadb / ollama 故意**不在这里 import**：
# 多进程时子进程会重新执行本模块的顶层代码，把这两个重库带进每一个子进程 ——
# 6 个进程就能把内存吃光（实测 MemoryError / 卡死不动）。
# 改成"用到时才 import"，子进程只背负真正需要的部分。
from src.config import CHROMA_DIR, DATA_ROOT, EMBED_MODEL  # noqa: E402
from src.ingest.chunk import chunk_text  # noqa: E402
from src.ingest.parse import parse_file, strip_boilerplate, SUPPORTED  # noqa: E402
from src.ingest.ocr import limit_ort_threads, parse_pdf_pages, using_gpu  # noqa: E402
from src.ingest.versioning import META_KEYS, batch_meta  # noqa: E402

COLLECTION = "knowledge"
EMBED_BATCH = 16   # 一次送多少段去向量化

# 目录名 → 密级。这是"数据分级"，权限系统的地基。
SENSITIVITY_RULES = {
    "产品资料": "public",
    "产品照片": "public",
    "报价单": "confidential",
    "投标文件": "confidential",
    "采购合同": "restricted",
    "工作home": "internal",
}
DEFAULT_SENSITIVITY = "internal"


def infer_meta(path: Path, scan_root: Path) -> dict:
    """
    根据文件所在目录推断元数据（阶段三会接入真实的人员映射表）。

    注意：密级要按「资料根目录 DATA_ROOT」的相对路径判断，
    不能用本次扫描的目录 —— 否则单扫一个子目录时认不出它属于哪一类。
    """
    try:
        rel = path.relative_to(DATA_ROOT)
    except ValueError:
        rel = path.relative_to(scan_root)
    parts = rel.parts
    top = parts[0] if parts else ""
    # 项目名：报价单/20260812-MC-某地区/xx.pdf 里的 "20260812-MC-某地区"
    # 若文件直接躺在顶层目录下，parts 只有 [顶层, 文件名]，没有项目层
    project = parts[1] if len(parts) >= 3 else ""
    return {
        "source": str(rel).replace("\\", "/"),
        "top": top,
        "project": project or top,
        "sensitivity": SENSITIVITY_RULES.get(top, DEFAULT_SENSITIVITY),
        "owner": "unassigned",   # 阶段三：客户 → 业务员的映射表填这里
        "kind": path.suffix.lower().lstrip("."),
    }


def embed_texts(texts: list[str]) -> list[list[float]]:
    """调用 bge-m3 把文本批量变成向量。"""
    import ollama   # 延迟 import（见文件头说明）

    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i : i + EMBED_BATCH]
        r = ollama.embed(model=EMBED_MODEL, input=batch)
        out.extend(r["embeddings"])
    return out


def get_collection(rebuild: bool):
    import chromadb   # 延迟 import（见文件头说明）

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    if rebuild:
        try:
            client.delete_collection(COLLECTION)
            print("（已删除旧集合，重新建库）")
        except Exception:
            pass
    try:
        return client.get_or_create_collection(
            name=COLLECTION, metadata={"hnsw:space": "cosine"}
        )
    except Exception:
        return client.get_or_create_collection(name=COLLECTION)


def extract_one(path_str: str, dpi: int, use_gpu: bool, no_ocr: bool,
                threads: int = 0) -> dict:
    """
    处理**一个**文件：解析（必要时 OCR）→ 清洗 → 切片。

    为什么只做到"切片"就返回？因为要让它在**子进程**里跑。
    向量化（调 ollama）和写向量库留给主进程 —— 避免多个进程同时写库打架。

    ⚠️ 必须是模块级函数：Windows 用 spawn 方式起子进程，函数得能被 pickle。

    返回：
      {"path", "chunks", "meta", "tag", "n_ocr"}   成功
      {"path", "error"}                            失败
      {"path", "skip"}                             跳过（无有效内容）
    """
    if threads > 0:
        # 多进程时必须限线程，否则各进程互相抢 CPU（见 ocr.limit_ort_threads）
        limit_ort_threads(threads)

    f = Path(path_str)
    try:
        if f.suffix.lower() == ".pdf" and not no_ocr:
            # PDF 一律逐页解析：文本页直接抽、图片页 OCR（见 lessons.md L11/L13）
            r = parse_pdf_pages(f, dpi=dpi, verbose=False, use_gpu=use_gpu)
            text = r["text"]
            n_ocr = r["n_ocr"]
            tag = f"⟳ OCR {r['n_ocr']}/{r['pages_total']} 页" if r["n_ocr"] else "· 文本"
        else:
            parsed = parse_file(f)
            if parsed.get("error"):
                return {"path": path_str, "error": parsed["error"]}
            text = parsed["text"]
            n_ocr = 0
            tag = "· 文本"
            if parsed["needs_ocr"] or len(text) < 50:
                reason = "扫描件需OCR（已禁用）" if parsed["needs_ocr"] else "文本过少"
                return {"path": path_str, "skip": reason}
    except Exception as e:
        return {"path": path_str, "error": f"{type(e).__name__}: {e}"}

    if len(text) < 50:
        return {"path": path_str, "skip": "无有效内容"}

    # 密级按「资料根目录」判断，不用扫描目录 —— 否则单扫子目录时认不出类别
    meta = infer_meta(f, DATA_ROOT)
    # 去掉每页重复的页眉页脚，否则噪音切片会淹没知识（见 lessons.md L10）
    chunks = chunk_text(strip_boilerplate(text))
    if not chunks:
        return {"path": path_str, "skip": "切不出内容"}
    return {"path": path_str, "chunks": chunks, "meta": meta, "tag": tag, "n_ocr": n_ocr}


def main() -> None:
    ap = argparse.ArgumentParser(description="把资料入库到向量库")
    ap.add_argument("--dir", default=str(DATA_ROOT), help="要入库的目录")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个文件（0=全部）")
    ap.add_argument("--rebuild", action="store_true", help="清空重建")
    ap.add_argument("--no-ocr", action="store_true",
                    help="禁用 OCR（跑得快，但图片型 PDF 会被跳过）")
    ap.add_argument("--dpi", type=int, default=200, help="OCR 渲染清晰度（默认 200）")
    ap.add_argument("--cpu", action="store_true", help="OCR 强制用 CPU（默认自动用 GPU）")
    ap.add_argument("--workers", type=int, default=1,
                    help="并行进程数；>1 时 OCR 走 CPU（多进程抢同一块显卡反而更慢）")
    ap.add_argument("--gpu", action="store_true",
                    help="配合 --workers 使用：让每个进程也用 GPU（显卡够强时更快）")
    args = ap.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"目录不存在：{root}")
        return

    # 扫描待处理文件。
    # ⚠️ 排除 macOS 自动生成的元数据垃圾（以 ._ 开头，如 "._xxx.pdf"）——
    #    它们不是真文件、打不开，只会让统计里多出一堆"失败"。
    files = [p for p in sorted(root.rglob("*"))
             if p.is_file()
             and p.suffix.lower() in SUPPORTED
             and not p.name.startswith("._")
             and not p.name.startswith("~$")]
    # 全量快照：版本/时效判定要"跟全库比"，不能用被 --limit 截断后的列表
    all_files = list(files)
    if args.limit:
        files = files[: args.limit]
    print(f"入库目录：{root}")

    col = get_collection(args.rebuild)

    # 断点续跑：已经在库里的文件直接跳过（加了 --rebuild 就不跳）。
    # 这样任务被中断后重跑，不会把完成过的几千个切片再算一遍。
    if not args.rebuild:
        try:
            # 分页读：切片上万后一次性 get 会撞 SQLite 的变量个数上限（见 lessons.md L18）
            done: set[str] = set()
            _total = col.count()
            _off = 0
            while _off < _total:
                d = col.get(include=["metadatas"], limit=4000, offset=_off)
                got = d["ids"]
                if not got:
                    break
                for m in d["metadatas"]:
                    done.add(m.get("source"))
                _off += len(got)

            def _src(p: Path) -> str:
                try:
                    return str(p.relative_to(DATA_ROOT)).replace("\\", "/")
                except ValueError:
                    return str(p).replace("\\", "/")

            if done:
                before = len(files)
                files = [f for f in files if _src(f) not in done]
                print(f"跳过已入库：{before - len(files)} 个文件")
        except Exception as e:
            print(f"（读取已入库清单失败，忽略：{type(e).__name__}）")

    # ---- 版本与时效元数据（设计见 docs/versioning.md）----
    # 必须在**全量文件**上判定：谁是最新版本、哪些是重复副本。
    # 这些信息从文件路径即可推断，不需要读内容，所以成本很低。
    t_meta = time.time()
    time_map = batch_meta(all_files, root)
    n_dup = sum(1 for v in time_map.values() if v.get("status") == "duplicate")
    n_sup = sum(1 for v in time_map.values() if v.get("status") == "superseded")
    print(f"版本与时效推断：{len(time_map)} 个文件"
          f"（重复副本 {n_dup} / 已被取代 {n_sup}，耗时 {time.time()-t_meta:.1f}s）")

    print(f"待处理文件：{len(files)} 个\n")
    t0 = time.time()
    n_doc = n_chunk = n_skip = n_err = 0
    n_ocr_files = n_ocr_pages = 0

    # 默认：多进程走 CPU（几块 CUDA 上下文抢同一张卡可能互相拖慢）。
    # 加 --gpu 则允许"多进程 + GPU"，显卡够强时吞吐更高。
    use_gpu = (not args.cpu) and (args.workers <= 1 or args.gpu)

    def handle(idx: int, res: dict) -> None:
        """把 extract_one 的结果落库（主进程串行做，避免并发写库冲突）。"""
        nonlocal n_doc, n_chunk, n_skip, n_err, n_ocr_files, n_ocr_pages
        name = Path(res["path"]).name
        if "error" in res:
            n_err += 1
            print(f"  [{idx}/{len(files)}] ✗ {name[:34]}  {res['error'][:44]}")
            return
        if "skip" in res:
            n_skip += 1
            print(f"  [{idx}/{len(files)}] ⤫ {name[:34]}  跳过（{res['skip']}）")
            return
        chunks, meta, tag = res["chunks"], res["meta"], res["tag"]

        # 合并版本/时效元数据（日期、版本号、状态、内容指纹）
        tm = time_map.get(meta.get("source", ""))
        if tm:
            for k in META_KEYS:
                if k in tm:
                    meta[k] = tm[k]
        meta.setdefault("dup_of", "")

        if res["n_ocr"]:
            n_ocr_files += 1
            n_ocr_pages += res["n_ocr"]
        vecs = embed_texts(chunks)
        ids = [f"{meta['source']}::{i}" for i in range(len(chunks))]
        metas = [dict(meta, chunk_index=i) for i in range(len(chunks))]
        col.add(ids=ids, documents=chunks, embeddings=vecs, metadatas=metas)
        n_doc += 1
        n_chunk += len(chunks)
        print(f"  [{idx}/{len(files)}] ✓ {name[:30]:<32} {len(chunks):>3} 块  [{meta['sensitivity']}] {tag}")

    if args.workers > 1:
        # 多进程并行：解析 + OCR 是 CPU 密集，并行收益最大。
        # 向量化与写库仍在主进程串行 —— 那里不是瓶颈。
        from concurrent.futures import ProcessPoolExecutor, as_completed

        # ⚠️ 关键：**给主进程和 ollama 留出核心**。
        # worker 把 CPU 全吃完，会出现"worker 飞快产出、主进程饿死、日志一动不动"
        # 的诡异状态（实测踩到：OCR 缓存暴涨 175 个，入库日志 11 分钟没动）。
        usable = max(1, (os.cpu_count() or 4) - 2)
        tpv = max(1, usable // args.workers)
        # ⚠️ 必须在起子进程**之前**设好这些环境变量 —— 子进程会继承它们。
        # 等进了子进程再设就晚了：OpenBLAS 在 import 那一刻就按 CPU 核心数建好线程池，
        # 6 个进程叠起来会把虚拟内存榨干（实测 OpenBLAS memory allocation failed）。
        for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                  "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            os.environ[k] = str(tpv)
        print(f"并行进程数：{args.workers}（OCR 走 CPU，避免抢显卡）"
              f"｜每进程限 {tpv} 线程（共 {os.cpu_count()} 线程）\n")
        done = 0
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(extract_one, str(f), args.dpi, use_gpu,
                              args.no_ocr, tpv) for f in files]
            for fut in as_completed(futs):
                done += 1
                handle(done, fut.result())
    else:
        for idx, f in enumerate(files, 1):
            handle(idx, extract_one(str(f), args.dpi, use_gpu, args.no_ocr))

    dt = time.time() - t0
    print("\n" + "=" * 62)
    print(f"完成：{n_doc} 个文件入库 / {n_chunk} 个切片 / 跳过 {n_skip} / 失败 {n_err}")
    if n_ocr_files:
        dev = "GPU" if using_gpu() else "CPU"
        print(f"其中 OCR 抢救：{n_ocr_files} 个文件 / {n_ocr_pages} 页（设备 {dev}）")
    print(f"耗时 {dt:.1f} 秒，向量库现有 {col.count()} 个切片")
    print(f"向量库位置：{CHROMA_DIR}")


if __name__ == "__main__":
    main()
