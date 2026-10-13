r"""
全局配置。

⚠️ 本项目不复制任何机密资料，只通过路径引用 D:\RAG资料（只读）。
"""
from pathlib import Path

# ---------- 路径 ----------
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 机密资料根目录（只读，绝不复制进本项目）
DATA_ROOT = Path(r"D:\RAG资料")

# 解析产物 / 向量库（均被 .gitignore 挡在版本控制外）
DATA_DIR = PROJECT_ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
CHROMA_DIR = DATA_DIR / "chroma"
# OCR 结果缓存：OCR 很慢（约 9 秒/页），同一份文件不重复识别。
# 缓存文件名 = 文件路径+大小+修改时间 的哈希 → 文件一变，缓存自动失效。
OCR_CACHE_DIR = DATA_DIR / "ocr_cache"

# ---------- 切片参数 ----------
# 每个切片的目标字数。太大 → 检索不准；太小 → 丢上下文。
# 中文按"字"算，500 字约等于一段完整语义。
CHUNK_SIZE = 500
# 相邻切片的重叠字数，防止一句话被从中间切断导致语义丢失。
CHUNK_OVERLAP = 80

# ---------- 模型 ----------
LLM_MODEL = "qwen2.5:7b"     # 生成模型
EMBED_MODEL = "bge-m3"       # 向量模型
TOP_K = 5                    # 每次检索取回几段
