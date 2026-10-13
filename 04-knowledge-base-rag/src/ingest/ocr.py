r"""
逐页 OCR 解析器 —— 治「混合型 PDF」这个坑（见 docs/lessons.md L11）。

核心思想：**逐页决策，而不是逐文件决策。**

同一个 PDF 里，往往：
  · 前几页是公司简介 → 有真实文字层，直接抽就行；
  · 后面几页是参数表 → 是图片，文本层只有页眉噪音，必须 OCR。

所以每一页都先问一句："这页自带的文字有用吗？"
  · 有用 → 直接用（快、准）
  · 没用 → 渲染成图 → OCR（慢，但能把图里的字抠出来）

用法（安装 OCR 依赖后可跑）：
    python src/ingest/ocr.py --file "D:/RAG资料/产品资料/Ball Valve(2).pdf"
    python src/ingest/ocr.py --dir "D:/RAG资料/产品资料" --limit 3 --dpi 200
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import OCR_CACHE_DIR  # noqa: E402
from src.ingest.parse import effective_stats  # noqa: E402

# OCR 引擎是"重"对象（要加载模型），全局只创建一次
_OCR = None
_USING_GPU = None   # 记录本次到底用了 GPU 还是 CPU（供日志显示）


def cuda_available() -> bool:
    """检测 onnxruntime 能不能用 CUDA。"""
    try:
        import onnxruntime as ort
        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


def preload_cuda_libs() -> None:
    """
    把 pip 装的 CUDA 运行库（nvidia-cudnn-cu12 / cublas 等）注册进 DLL 搜索路径。

    好处：**不用装几个 G 的 CUDA Toolkit**，pip 包就够。

    为什么这么麻烦？onnxruntime 在 Windows 上加载 cuDNN 时，只认"已注册的 DLL 目录"
    和系统 PATH。不注册就会出现：
        Could not locate cudnn_engines_tensor_ir64_9.dll
        → CUDNN_BACKEND_API_FAILED → **悄悄回退 CPU**（GPU 白装了）。

    踩坑记录见 docs/lessons.md L14：
      onnxruntime-gpu 的版本必须和 CUDA 版本对上（1.31 要 CUDA 13，1.22 要 CUDA 12）。
    """
    import ctypes

    # 找到 pip 的 nvidia/*/bin（虚拟环境里的 CUDA 运行库）
    bins: list[str] = []
    try:
        for p in sys.path:
            if p.endswith("site-packages"):
                bins = sorted(glob.glob(os.path.join(p, "nvidia", "*", "bin")))
                break
    except Exception:
        pass

    # ① 注册 DLL 目录
    for d in bins:
        try:
            os.add_dll_directory(d)
        except Exception:
            pass
    # ② 同时塞进 PATH（有些加载器只看 PATH）
    if bins:
        os.environ["PATH"] = os.pathsep.join(bins) + os.pathsep + os.environ.get("PATH", "")
    # ③ 显式预加载：先底层运算库（cublas/cufft/curand/cudart），再 cuDNN 系列
    for d in bins:
        files = sorted(glob.glob(os.path.join(d, "*.dll")))
        low = lambda f: "cudnn" in os.path.basename(f).lower()
        for fp in [f for f in files if not low(f)] + [f for f in files if low(f)]:
            try:
                ctypes.WinDLL(fp)
            except Exception:
                pass

    # ④ 最后让 onnxruntime 自己再认一遍
    try:
        import onnxruntime as ort
        if hasattr(ort, "preload_dlls"):
            ort.preload_dlls()
    except Exception:
        pass


def limit_ort_threads(n: int = 1) -> None:
    """
    限制**每个进程**里各种计算库的线程数。

    为什么必须做？多进程并行时，**每个进程默认都会吃满所有 CPU 线程** ——
    6 个进程 × 12 线程 = 72 个线程抢 12 个核心，光上下文切换就把收益吃光了
    （实测：不限制时 6 进程跑 74 页要 515 秒，几乎等于单进程，白并行）。

    而且不只是 onnxruntime：**数学库（OpenBLAS / MKL / OpenMP）也在开线程**，
    它们各自都要为线程分配缓冲区，6 个进程叠起来能把虚拟内存直接榨干
    （实测报 `OpenBLAS error: Memory allocation still failed`）。

    RapidOCR 内部写的是 `from onnxruntime import SessionOptions`（直接引入的名字），
    改不了它，所以把 utils 模块里的 SessionOptions 换成一个"预设好线程数的工厂"。
    """
    # ① 先掐住数学库的线程数（必须在 numpy/onnxruntime 之前设，才生效）
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
              "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        os.environ[k] = str(n)

    # ② 再掐 onnxruntime 自己的 session 线程数
    try:
        import rapidocr_onnxruntime.utils as ru
        if getattr(ru, "_wb_threads_limited", False):
            return
        _orig = ru.SessionOptions

        def _factory(*a, **kw):
            so = _orig(*a, **kw)
            so.intra_op_num_threads = n
            so.inter_op_num_threads = 1
            return so

        ru.SessionOptions = _factory
        ru._wb_threads_limited = True
    except Exception:
        pass


# ⚠️ 图表/照片型页面的性能陷阱：
# 工程图纸、财务报表、产品照片这类页面，OCR 的"检测阶段"会把**噪点也当成文字框**，
# 一页能检出成百上千个候选框，然后逐个送去识别 → 卡死式地慢
# （实测：4 个 worker 各烧 800 秒 CPU，一个小文件都出不来）。
# 限制候选框数量，让这类页面快速通过。正常文档只有几十个框，完全不受影响。
OCR_LIMITS = dict(det_max_candidates=300)


def get_ocr(use_gpu: bool = True):
    """
    懒加载 RapidOCR（第一次调用才加载模型，约几秒）。

    ⚠️ RapidOCR 开 GPU 有个坑：只传 `det_use_cuda=True` 会崩（它内部要读
    `model_path`，见 utils.py 的 update_det_params），**必须连模型路径一起传**。
    """
    global _OCR, _USING_GPU
    if _OCR is not None:
        return _OCR

    import rapidocr_onnxruntime
    from rapidocr_onnxruntime import RapidOCR

    md = os.path.join(os.path.dirname(rapidocr_onnxruntime.__file__), "models")
    det_model = os.path.join(md, "ch_PP-OCRv3_det_infer.onnx")

    if use_gpu and cuda_available():
        preload_cuda_libs()
        try:
            _OCR = RapidOCR(
                det_use_cuda=True,
                det_model_path=det_model,
                cls_use_cuda=True,
                cls_model_path=os.path.join(md, "ch_ppocr_mobile_v2.0_cls_infer.onnx"),
                rec_use_cuda=True,
                rec_model_path=os.path.join(md, "ch_PP-OCRv3_rec_infer.onnx"),
                **OCR_LIMITS,
            )
            _USING_GPU = True
            return _OCR
        except Exception:
            _OCR = None   # 掉回 CPU

    # ⚠️ 只要动了 Det 的参数，就**必须同时给 det_model_path** ——
    # RapidOCR 内部会无条件去读 det_dict['model_path']，不给就 KeyError（见 utils.py 的 update_det_params）。
    _OCR = RapidOCR(det_model_path=det_model, **OCR_LIMITS)
    _USING_GPU = False
    return _OCR


def using_gpu() -> bool:
    """本次 OCR 实际跑在 GPU 还是 CPU。"""
    return bool(_USING_GPU)


def page_text_usable(text: str) -> bool:
    """
    判断"这一页自带的文本层是否有用"。

    标准（与 parse.py 一致）：至少命中 1 个规格关键词，且去重后内容不太短。
    不达标就认为这页是"图片页"，需要 OCR。
    """
    uniq_len, hits = effective_stats(text)
    return hits >= 1 and uniq_len >= 120


def render_page_image(page, dpi: int = 200, max_pixels: int = 8_000_000):
    """
    把 PDF 的一页渲染成 numpy 图像（BGR），直接喂给 OCR。

    dpi 是"清晰度"：
      · 太低（<150）→ 小字认不出来，OCR 出错；
      · 太高（>300）→ 又慢又占内存。
      200 是精度和速度的常用平衡点。

    ⚠️ 优化一：**不要编码成 PNG**。
    原来写的是 `pix.tobytes("png")`，PNG 压缩很吃 CPU，而图像只是喂给 OCR
    "看一眼"，根本不需要压缩成文件。实测换成直接传数组快 **17%**。

    ⚠️ 优化二：**限制最大像素数**。
    有些图纸页在 200 dpi 下能到 8609×6300（**5400 万像素、单张 160MB**），
    多进程跑起来直接把内存挤爆（实测 `MemoryError`）。
    而且 OCR 检测阶段本来就会缩图，渲染得过大纯属浪费。
    """
    import fitz
    import numpy as np

    zoom = dpi / 72.0
    r = page.rect
    if r.width * r.height * zoom * zoom > max_pixels:
        zoom = (max_pixels / (r.width * r.height)) ** 0.5
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))

    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:          # RGBA → BGR
        arr = arr[:, :, :3][:, :, ::-1]
    elif pix.n == 3:        # RGB → BGR
        arr = arr[:, :, ::-1]
    return np.ascontiguousarray(arr)


def ocr_image(img, use_gpu: bool = True) -> str:
    """把图片（numpy 数组或字节流）交给 OCR，返回识别出的文字。"""
    ocr = get_ocr(use_gpu)
    result, _elapse = ocr(img)
    if not result:
        return ""
    # result 每项结构：[边框坐标, 识别文字, 置信度]
    return "\n".join(line[1] for line in result if len(line) > 1 and line[1])


def _cache_path(path: Path, force_ocr: bool) -> Path:
    """缓存文件 = 文件路径 + 大小 + 修改时间 + 是否强制OCR 的哈希。"""
    st = path.stat()
    key = f"{path}|{st.st_size}|{int(st.st_mtime)}|{int(force_ocr)}"
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    return OCR_CACHE_DIR / f"{path.stem[:24]}_{h}.json"


def load_cache(path: Path, force_ocr: bool = False) -> dict | None:
    cp = _cache_path(path, force_ocr)
    if cp.exists():
        try:
            return json.loads(cp.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def save_cache(path: Path, force_ocr: bool, data: dict) -> None:
    try:
        cp = _cache_path(path, force_ocr)
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def parse_pdf_pages(path: Path, dpi: int = 200, force_ocr: bool = False,
                    verbose: bool = True, use_cache: bool = True,
                    use_gpu: bool = True, time_budget: float = 180.0) -> dict:
    """
    逐页解析一个 PDF。

    返回：{"text": 全文, "pages": [...], "n_ocr": 用了几页 OCR, "pages_total": 总页数}

    带缓存（use_cache=True）：OCR 很慢，同一份文件第二次调用直接读缓存。

    time_budget：**单文件时间预算（秒）**。工程图纸、照片这类页面会让 OCR 陷入
    超长计算（一页能检出上千个"假文字框"，逐个识别）。超预算就放弃剩余页，
    已识别的部分照常入库 —— **宁可少要一点内容，也不能让一个文件拖死整个任务**。
    """
    if use_cache:
        cached = load_cache(path, force_ocr)
        if cached is not None:
            if verbose:
                print("    （命中 OCR 缓存，跳过识别）")
            return cached

    import fitz  # PyMuPDF

    doc = fitz.open(str(path))
    parts: list[str] = []
    page_info: list[dict] = []
    n_ocr = 0
    truncated = False
    t_start = time.time()

    for i, page in enumerate(doc, 1):
        if time_budget and (time.time() - t_start) > time_budget:
            truncated = True
            if verbose:
                print(f"    ⏱ 超过 {time_budget:.0f} 秒预算，放弃剩余 {len(doc) - i + 1} 页")
            break

        native = page.get_text() or ""
        if not force_ocr and page_text_usable(native):
            text, how = native, "text"
        else:
            text, how = ocr_image(render_page_image(page, dpi), use_gpu), "ocr"
            n_ocr += 1
        if text.strip():
            parts.append(text.strip())
        page_info.append({"page": i, "how": how, "chars": len(text)})
        if verbose:
            print(f"    第 {i:>3} 页  {how:<4}  {len(text):>6} 字")

    doc.close()
    result = {
        "text": "\n".join(parts),
        "pages": page_info,
        "n_ocr": n_ocr,
        "pages_total": len(page_info),
        "truncated": truncated,
    }
    if use_cache:
        save_cache(path, force_ocr, result)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="逐页 OCR 解析 PDF")
    ap.add_argument("--file", help="单个 PDF 文件")
    ap.add_argument("--dir", help="目录（配合 --limit）")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--force-ocr", action="store_true", help="忽略文本层，全部走 OCR")
    ap.add_argument("--cpu", action="store_true", help="强制用 CPU（默认自动用 GPU）")
    ap.add_argument("--show", type=int, default=400, help="打印前 N 个字符预览")
    args = ap.parse_args()

    files: list[Path] = []
    if args.file:
        files = [Path(args.file)]
    elif args.dir:
        files = [p for p in sorted(Path(args.dir).rglob("*.pdf"))][: args.limit]
    else:
        print("请指定 --file 或 --dir")
        return

    for f in files:
        print("=" * 66)
        print(f"文件：{f.name}")
        print("=" * 66)
        t0 = time.time()
        r = parse_pdf_pages(f, dpi=args.dpi, force_ocr=args.force_ocr,
                            use_gpu=not args.cpu)
        dt = time.time() - t0

        uniq_len, hits = effective_stats(r["text"])
        dev = "GPU" if using_gpu() else "CPU"
        print(f"\n  总页数 {r['pages_total']}｜其中 OCR {r['n_ocr']} 页"
              f"｜设备 {dev}｜耗时 {dt:.1f} 秒")
        print(f"  抽出 {len(r['text'])} 字（去重后 {uniq_len}）"
              f"｜规格关键词命中 {hits} 个")
        print(f"\n  ---- 文本预览 ----")
        print("  " + r["text"][: args.show].replace("\n", "\n  "))
        print()


if __name__ == "__main__":
    main()
