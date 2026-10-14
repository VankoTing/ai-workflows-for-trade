"""AI 结构化抽取：把各种格式的询价资料，抽成统一的 12 个标准字段。

这是整条流水线的核心。客户发来的东西格式千奇百怪（Excel / Word / PDF / 图纸 / 铭牌照片），
只有模型能把它们收敛成同一套字段——这一步不能省，也不能用规则替代。

输出直接构造 exporter 的 InquiryTable，字段定义与参照格式严格对齐。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

from exporter import BiCell, ExtraColumn, InquiryRow, InquiryTable

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
VISION_MODEL = "deepseek-v4-flash-vision-exp"

SYSTEM_PROMPT = """你是一名资深工业阀门外贸技术支持工程师，负责把客户发来的阀门询价资料，
整理成公司统一的询价对照表。

【目标字段】每个阀门一行，必须抽成以下 12 列：
1.  no                  序号，沿用原资料序号；没有就按出现顺序编号
2.  valve_type          阀门类型，如 Ball Valve/球阀、Gate Valve/闸阀、Globe Valve/截止阀、
                        Check Valve/止回阀、Butterfly Valve/蝶阀、Needle Valve/针型阀、Plug Valve/旋塞阀
3.  size                口径，如 {"en": "6\\"", "zh": "6英寸 (DN150)"}
4.  rating              压力等级，如 {"en": "150#", "zh": "Class 150"}、{"en": "3000#", "zh": "Class 3000"}
5.  end_connection      端部连接，如 RF (Raised Face)/凸面法兰、RTJ/环形槽面、
                        NPT Threaded/NPT 螺纹连接、BW/对焊、Threaded Ends/螺纹连接
6.  port                通径型式，FP (Full Port)/全通径、RP (Reduced Port)/缩径；不适用填 "-"
7.  body_material       阀体材质，如 {"en": "CS, ASTM A216 GR.WPB", "zh": "碳钢，ASTM A216 GR.WPB"}
8.  trim                内件（球体/阀杆/阀座），如 {"en": "316 SS Ball, Stem & Seat Rings",
                        "zh": "316不锈钢 球体、阀杆、阀座"}
9.  design_standard     设计标准，如 "API 6D; ASME B16.34"（标准号保持原样，不要翻译，只填 en）
10. special_requirements 特殊要求，如防火、密封、操作方式等；没有就填 "Not specified/未注明"
11. qty                 数量，纯数字
12. unit                单位，通常是 {"en": "EA", "zh": "台"}

【双语要求】
- 除 no / qty / design_standard 外，每个字段都要拆成 en 和 zh 两份。
- 资料里只有中文的，补出规范英文；只有英文的，补出中文。
- 英文用国际阀门贸易的通用表述；中文用行业习惯说法。
- ★ 两行必须是不同内容、互为补充，绝不允许两行写同一句话。典型正确写法：
      rating          {"en": "150#", "zh": "Class 150"}
      rating          {"en": "3000#", "zh": "Class 3000"}
      size            {"en": "4\\"", "zh": "4英寸 (DN100)"}
      size            {"en": "1/2\\"", "zh": "1/2英寸 (DN15)"}
      end_connection  {"en": "RF (Raised Face)", "zh": "凸面法兰"}
      port            {"en": "FP (Full Port)", "zh": "全通径"}
  典型错误（会被打回）：
      {"en": "Class 150", "zh": "Class 150"}   ← 两行重复
      {"en": "DN100", "zh": "DN100 (4英寸)"}   ← 英文行不该带 DN
- size 的 en 只放英寸值（如 4"），DN 值放在 zh 行；原文只给 DN 没给英寸时，en 写 "DN100"、
  zh 写 "DN100"，任其自然合并成一行即可。

【缺失与不适用】
- 原资料未注明的：{"en": "Not specified", "zh": "未注明"}
- 该字段对这类产品不适用的：{"en": "-", "zh": "-"}
- 绝不编造。拿不准的一律写进 notes 请客户确认。

【技术审查 notes】同时列出需要客户澄清的问题，每条中英对照，重点看：
- 序号跳号、重复行
- 信息缺失：口径、压力等级、材质、数量未注明
- 标准引用错误：例如 ASTM A216 铸钢牌号是 WCA/WCB/WCC，而 "WPB" 属于 ASTM A234 锻制管件；
  ASTM A350 只覆盖锻件，铸造不锈钢应为 A351
- 材质与工况冲突：例如 NBR/丁腈衬里用于高温介质
- 前后矛盾：同一份资料里数据不一致
- 行业惯例提醒：该产品通常执行什么标准、有哪些默认配置需要确认

【extra_columns —— 缺失必要参数的补充列】
审阅资料后，找出「阀门询价中必要、但这份资料没有完整提供、且上面 12 个标准列没有覆盖」的参数，
把它们补成表格的额外列。判定要同时满足：
- 属于报价必需、或会严重影响报价准确度的技术参数
- 上面 12 个标准列里没有
- 这份资料缺失它，或只有部分产品写了（例如有的阀门标了结构长度、有的没标）

常见候选（只在确实缺失时才加）：
Face-to-Face (FF) / 结构长度、Working Fluid / 工作介质、Flange Standard / 法兰标准、
Operation / 操作方式、Face Finish / 密封面粗糙度、Painting / 涂装要求、
Inspection & Test / 检验与试验、Third-party Certification / 第三方认证

硬性要求：
- 最多 4 列，宁缺毋滥；确实没有缺失就返回空数组 []
- key 用小写蛇形英文，如 face_to_face
- 每行的对应值放在 rows[].extra 里，键就是该列的 key。
  某行确实没有这个参数时给 {"en": "", "zh": ""}，系统会自动填 Not provided / 未提供；
  你不要自己去写"未提供"，更不要编造数值。

【输出】只输出 JSON，不要任何解释文字，不要 markdown 代码块：
{
  "ref": "项目编号，如 691-2610-26",
  "country": "项目国家，如 Egypt 埃及",
  "rows": [
    {"no": "1",
     "valve_type": {"en": "Ball Valve", "zh": "球阀"},
     "size": {"en": "6\\"", "zh": "6英寸 (DN150)"},
     "rating": {"en": "150#", "zh": "Class 150"},
     "end_connection": {"en": "RF (Raised Face)", "zh": "凸面法兰"},
     "port": {"en": "FP (Full Port)", "zh": "全通径"},
     "body_material": {"en": "CS, ASTM A216 GR.WPB", "zh": "碳钢，ASTM A216 GR.WPB"},
     "trim": {"en": "316 SS Ball, Stem & Seat Rings", "zh": "316不锈钢 球体、阀杆、阀座"},
     "design_standard": {"en": "API 6D; ASME B16.34", "zh": ""},
     "special_requirements": {"en": "Fire safe per API RP-6FA", "zh": "API RP-6FA 防火"},
     "qty": "5",
     "unit": {"en": "EA", "zh": "台"},
     "extra": {"face_to_face": {"en": "229 mm", "zh": "229 mm"}}}
  ],
  "extra_columns": [
    {"key": "face_to_face", "en": "Face-to-Face (FF)", "zh": "结构长度"}
  ],
  "notes": [{"en": "...", "zh": "..."}]
}"""

FIELDS = [
    "valve_type", "size", "rating", "end_connection", "port",
    "body_material", "trim", "design_standard", "special_requirements", "unit",
]


@dataclass
class ExtractResult:
    table: InquiryTable | None = None
    term_pairs: dict[str, str] = field(default_factory=dict)
    error: str = ""
    raw: str = ""

    @property
    def ok(self) -> bool:
        return self.table is not None and not self.error


def _bi(value) -> BiCell:
    if isinstance(value, dict):
        return BiCell(en=str(value.get("en", "") or ""), zh=str(value.get("zh", "") or ""))
    if value is None:
        return BiCell()
    text = str(value).strip()
    return BiCell(en=text)


class Extractor:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None, timeout: float = 300.0) -> None:
        self.api_key = (api_key or os.getenv("DEEPSEEK_API_KEY", "")).strip()
        self.model = (model or os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL)).strip()
        self.base_url = (base_url or os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def extract(self, doc_text: str, hint: str = "") -> ExtractResult:
        if not self.enabled:
            return ExtractResult(
                error="未配置 DEEPSEEK_API_KEY，无法进行结构化抽取。"
                      "请在 .env 里填入 Key，或使用 --from-json 走离线导出。"
            )

        user = ["以下是客户发来的阀门询价资料：\n", doc_text]
        if hint:
            user.append(f"\n\n【已知信息】\n{hint}")

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(user)},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "stream": False,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPStatusError as exc:
            return ExtractResult(error=f"调用模型失败 HTTP {exc.response.status_code}：{exc.response.text[:300]}")
        except Exception as exc:  # noqa: BLE001
            return ExtractResult(error=f"调用模型失败：{exc}")

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            return ExtractResult(error=f"模型返回结构异常：{exc}")

        return self._parse(content)

    @staticmethod
    def _parse(content: str) -> ExtractResult:
        text = (content or "").strip()
        fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                return ExtractResult(error="模型返回的不是合法 JSON。", raw=content)
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return ExtractResult(error="模型返回的不是合法 JSON。", raw=content)

        table = InquiryTable(
            ref=str(data.get("ref", "") or "").strip(),
            country=str(data.get("country", "") or "").strip(),
        )

        for item in data.get("rows") or []:
            if not isinstance(item, dict):
                continue
            row = InquiryRow(no=str(item.get("no", "") or ""), qty=str(item.get("qty", "") or ""))
            for fname in FIELDS:
                setattr(row, fname, _bi(item.get(fname)))
            extra = item.get("extra")
            if isinstance(extra, dict):
                row.extra = {
                    str(k).strip(): _bi(v) for k, v in extra.items() if str(k).strip()
                }
            table.rows.append(row)

        seen_keys: set[str] = set()
        for col in data.get("extra_columns") or []:
            if not isinstance(col, dict) or len(table.extra_columns) >= 4:
                break
            key = str(col.get("key", "") or "").strip()
            en = str(col.get("en", "") or "").strip()
            zh = str(col.get("zh", "") or "").strip()
            if not key or not (en or zh) or key in seen_keys:
                continue
            seen_keys.add(key)
            table.extra_columns.append(ExtraColumn(key=key, en=en, zh=zh))

        for n in data.get("notes") or []:
            if isinstance(n, dict):
                table.notes.append(_bi(n))
            else:
                table.notes.append(BiCell(en=str(n)))

        # 顺手把本次的中英对照收进术语库
        pairs: dict[str, str] = {}
        for row in table.rows:
            for fname in FIELDS:
                cell: BiCell = getattr(row, fname)
                zh, en = cell.zh.strip(), cell.en.strip()
                if zh and en and en != "-" and zh != "-" and len(zh) <= 12 and len(en) <= 40:
                    pairs.setdefault(zh, en)

        return ExtractResult(table=table, term_pairs=pairs, raw=content)
