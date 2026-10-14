"""术语库：把阀门询价里的固定说法做中英互查。

数据来源（两个文件，前者自动生成，后者人工维护）：
    data/terms.json       从历史成品自动提取 + AI 补译，程序可覆盖
    data/terms_extra.csv  人工增补，优先级永远最高（格式：中文,英文）

核心能力：
    tb = TermBase()
    tb.translate_cell("全通径球阀 DN100")   -> ("FP Ball Valve DN100", [])
    第二个返回值是术语库没覆盖到的中文片段，交给大模型兜底。

选词策略：**出现次数多的优先**，其次优先没有标点的、再优先短的。
这样能避免 "DIN 912 M16 × 30, Carbon Steel" 这类误提取的长词条抢占 "CS"。

翻译策略：**最长匹配贪心**，避免 "球阀" 抢在 "全通径球阀" 前面。
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path

# 中文串里允许出现常见中文标点，否则 "中线式，衬里阀体" 会被逗号切成两段，整条术语就匹配不上了
CJK_BODY = r"[\u4e00-\u9fff，、；：]"
CJK_RUN = re.compile(CJK_BODY + "+")
NON_CJK_RUN = re.compile(f"[^{CJK_BODY[1:-1]}]+")
CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")
TOKEN_RE = re.compile(f"{CJK_BODY}+|[^{CJK_BODY[1:-1]}]+")
PAREN_EN = re.compile(r"[（(]\s*([A-Za-z][A-Za-z0-9 .\-/]{0,20}?)\s*[）)]")
PUNCT = re.compile(r"[(),/;×·]")
MAX_EN_LEN = 30

# 术语库属于项目资产，要随代码入库，所以不放 data/ ——
# data/ 是「本地业务数据」，约定不入库（见 .gitignore）
_ROOT = Path(__file__).resolve().parent.parent
GLOSSARY_DIR = _ROOT / "glossary"
DEFAULT_PATH = GLOSSARY_DIR / "terms.json"
EXTRA_PATH = GLOSSARY_DIR / "terms_extra.csv"

EXTRA_WEIGHT = 10_000


def dedup_tail(en: str, raw: str) -> str:
    """原文里的 (RF) 这类英文标注，如果译文末尾已经带了，就别重复。"""
    for m in PAREN_EN.finditer(raw or ""):
        token = m.group(1).strip()
        if not token:
            continue
        en = re.sub(
            r"\s*[（(]" + re.escape(token) + r"[）)]\s*$", "", en, flags=re.IGNORECASE
        )
    return en


class TermBase:
    def __init__(self, path: str | Path | None = None,
                 extra_path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_PATH
        self.extra_path = Path(extra_path) if extra_path else EXTRA_PATH
        self.zh2en: dict[str, list[str]] = {}
        self.en2zh: dict[str, list[str]] = {}
        self._counts: dict[tuple[str, str], int] = {}
        self.max_zh_len = 1
        self.load()

    # ---------------------------------------------------------------- 加载

    def load(self) -> None:
        self.zh2en.clear()
        self.en2zh.clear()
        self._counts.clear()

        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            for item in payload.get("terms", []):
                zh = str(item.get("zh", "")).strip()
                en = str(item.get("en", "")).strip()
                if not zh or not en or len(en) > MAX_EN_LEN or CJK_CHAR.search(en):
                    continue
                try:
                    count = int(item.get("count", 0) or 0)
                except (TypeError, ValueError):
                    count = 0
                self._add(zh, en, count)

        self._load_extra()
        self._resort()
        self.max_zh_len = max((len(k) for k in self.zh2en), default=1)

    def _load_extra(self) -> None:
        """人工增补表，优先级最高。"""
        if not self.extra_path.exists():
            return
        try:
            with self.extra_path.open(encoding="utf-8-sig", newline="") as fh:
                for row in csv.reader(fh):
                    if len(row) < 2:
                        continue
                    zh, en = row[0].strip(), row[1].strip()
                    if not zh or not en or zh == "中文":
                        continue
                    self._add(zh, en, EXTRA_WEIGHT)
        except OSError:
            pass

    def _add(self, zh: str, en: str, count: int = 0) -> None:
        key = (zh, en)
        self._counts[key] = max(self._counts.get(key, 0), count)
        langs = self.zh2en.setdefault(zh, [])
        if en not in langs:
            langs.append(en)
        backs = self.en2zh.setdefault(en.lower(), [])
        if zh not in backs:
            backs.append(zh)

    def _resort(self) -> None:
        for zh, langs in self.zh2en.items():
            langs.sort(
                key=lambda en, zh=zh: (
                    -self._counts.get((zh, en), 0),
                    len(PUNCT.findall(en)),
                    len(en),
                    en,
                )
            )

    # ---------------------------------------------------------------- 保存

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path else self.path
        terms = [
            {"en": en, "zh": zh, "count": self._counts.get((zh, en), 0), "sources": ["manual"]}
            for zh, langs in sorted(self.zh2en.items())
            for en in langs
        ]
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "term_count": len(terms),
            "terms": terms,
        }
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def merge(self, pairs: dict[str, str], save: bool = True) -> int:
        """把模型补充的新术语并进术语库。"""
        added = 0
        for zh, en in (pairs or {}).items():
            zh, en = str(zh).strip(), str(en).strip()
            if not zh or not en or len(en) > MAX_EN_LEN:
                continue
            if zh in self.zh2en and en in self.zh2en[zh]:
                continue
            self._add(zh, en, 0)
            added += 1
        if added:
            self._resort()
            self.max_zh_len = max(len(k) for k in self.zh2en)
            if save:
                self.save()
        return added

    # ---------------------------------------------------------------- 查询

    def lookup_zh(self, zh: str) -> list[str]:
        return self.zh2en.get(zh.strip(), [])

    def lookup_en(self, en: str) -> list[str]:
        return self.en2zh.get(en.strip().lower(), [])

    def primary_en(self, zh: str) -> str | None:
        langs = self.lookup_zh(zh)
        return langs[0] if langs else None

    def __len__(self) -> int:
        return len(self.zh2en)

    # ---------------------------------------------------------------- 翻译

    def _greedy(self, run: str) -> tuple[str, list[str]]:
        """对一段连续中文做最长匹配切分。返回 (拼接结果, 未命中片段)。"""
        out: list[str] = []
        misses: list[str] = []
        i, n = 0, len(run)
        while i < n:
            hit_len = 0
            for length in range(min(n - i, self.max_zh_len), 0, -1):
                if run[i : i + length] in self.zh2en:
                    hit_len = length
                    break
            if hit_len:
                out.append(self.zh2en[run[i : i + hit_len]][0])
                i += hit_len
                continue

            j = i + 1
            while j < n:
                if any(
                    run[j : j + L] in self.zh2en
                    for L in range(1, min(n - j, self.max_zh_len) + 1)
                ):
                    break
                j += 1
            misses.append(run[i:j])
            out.append(run[i:j])
            i = j
        return " ".join(out), misses

    def translate_cell(self, text: str) -> tuple[str, list[str]]:
        """把单元格里的中文术语替换成英文，返回 (英文版文本, 未命中的中文片段)。"""
        if not text or not CJK_CHAR.search(text):
            return text or "", []

        misses: list[str] = []
        parts: list[tuple[str, str]] = []
        for tok in TOKEN_RE.findall(text):
            if CJK_RUN.fullmatch(tok):
                en, miss = self._greedy(tok)
                misses.extend(miss)
                parts.append(("en", en))
            else:
                parts.append(("raw", tok))

        buf = ""
        for _, seg in parts:
            if not seg:
                continue
            if (
                buf
                and not buf.endswith((" ", "(", "（", "/", "-", "[", "【"))
                and not seg.startswith(
                    (" ", ")", "）", ",", "，", ".", "。", "/", "-", ";", "；", ":", "：", "]")
                )
                and re.match(r"[A-Za-z0-9]", seg)
            ):
                buf += " "
            buf += seg
        return dedup_tail(re.sub(r"\s+", " ", buf).strip(), text), misses
