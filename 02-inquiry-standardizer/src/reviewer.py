"""AI 技术审查：调用 DeepSeek 完成术语补译 + 阀门技术审查。

这是整条流水线里唯一需要大模型的一步。
前面「读文件」「查术语表」都是纯程序，只有这里必须靠模型的专业推理。

两个职责：
    1. 补译 —— 术语库里没覆盖到的中文说法，交给模型翻译
    2. 审查 —— 找出询价资料里缺失、矛盾、或不符合标准的点，给出中英对照澄清备注

没有配置 API Key 时不会报错，会静默跳过这一步并给出提示，方便先跑通流程。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

SYSTEM_PROMPT = """你是一名资深工业阀门外贸技术支持工程师，熟悉 API、ASME、ASTM、BS、DIN、EN、ISO、MSS SP 等标准体系，
长期负责把客户的询价资料整理成可对外报价的规范文档。

对你收到的询价资料，完成两件事：

【一】术语补译
把资料中出现的中文术语，翻译成国际阀门贸易中规范的英文表述。
若同一个中文有多种常见英文写法，选外贸报价中最通用的那一种。

【二】技术审查
逐项检查资料，指出以下问题并给出可直接发给客户的澄清说明（中英对照）：
- 信息缺失：口径 DN、压力等级 Class、数量、材质、连接形式、结构长度等未注明的项目
- 标准引用错误：例如把铸造不锈钢写成 ASTM A350（A350 只覆盖锻件，铸造应为 A351），
  或标准号与材质/工艺不匹配
- 材质与工况冲突：例如丁腈橡胶(NBR)衬里用于高温介质、碳钢用于强腐蚀介质
- 前后矛盾：同一份资料中不同位置的数据不一致
- 通常做法提醒：按行业惯例该产品一般执行哪个标准、有哪些默认配置需要客户确认

【输出要求】
只输出 JSON，不要任何解释性文字、不要 markdown 代码块。格式如下：
{
  "translations": {"中文术语": "English term"},
  "notes": [
    {"topic": "简短主题", "zh": "中文说明", "en": "对应的英文说明"}
  ],
  "missing_info": ["缺失项1", "缺失项2"]
}

【硬性约束】
- 绝不编造原文没有的信息。无法判断的，写进 missing_info 请客户确认。
- 标准号必须准确，拿不准就不要写。
- notes 里每条都要中英对照，英文要能直接发给国外客户。
- 如果没有发现任何问题，notes 返回空数组，不要硬凑。"""


@dataclass
class ReviewResult:
    translations: dict[str, str] = field(default_factory=dict)
    notes: list[dict[str, str]] = field(default_factory=list)
    missing_info: list[str] = field(default_factory=list)
    skipped: bool = False
    error: str = ""
    raw: str = ""

    @property
    def ok(self) -> bool:
        return not self.skipped and not self.error


class Reviewer:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 180.0,
    ) -> None:
        self.api_key = (api_key or os.getenv("DEEPSEEK_API_KEY", "")).strip()
        self.model = (model or os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL)).strip()
        self.base_url = (base_url or os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def review(self, doc_text: str, known_terms: dict[str, str] | None = None) -> ReviewResult:
        if not self.enabled:
            return ReviewResult(
                skipped=True,
                error="未配置 DEEPSEEK_API_KEY，已跳过 AI 审查（术语补译与技术审查均未执行）。",
            )

        user_parts = ["以下是客户发来的阀门询价资料：\n", doc_text]
        if known_terms:
            sample = list(known_terms.items())[:120]
            lines = "\n".join(f"{zh} = {en}" for zh, en in sample)
            user_parts.append(
                "\n\n【已有术语表（仅供参考，不要重复输出这些）】\n" + lines
            )

        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(user_parts)},
            ],
            "temperature": 0.2,
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
            detail = exc.response.text[:300]
            return ReviewResult(error=f"调用模型失败 HTTP {exc.response.status_code}：{detail}")
        except Exception as exc:  # noqa: BLE001
            return ReviewResult(error=f"调用模型失败：{exc}")

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            return ReviewResult(error=f"模型返回结构异常：{exc}")

        return self._parse(content)

    @staticmethod
    def _parse(content: str) -> ReviewResult:
        text = (content or "").strip()
        # 容错：模型偶尔会裹上 ```json
        fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                try:
                    data = json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    return ReviewResult(error="模型返回的不是合法 JSON。", raw=content)
            else:
                return ReviewResult(error="模型返回的不是合法 JSON。", raw=content)

        translations = {
            str(k).strip(): str(v).strip()
            for k, v in (data.get("translations") or {}).items()
            if str(k).strip() and str(v).strip()
        }
        notes: list[dict[str, str]] = []
        for item in data.get("notes") or []:
            if isinstance(item, dict):
                notes.append(
                    {
                        "topic": str(item.get("topic", "")).strip(),
                        "zh": str(item.get("zh", "")).strip(),
                        "en": str(item.get("en", "")).strip(),
                    }
                )
        missing = [str(x).strip() for x in (data.get("missing_info") or []) if str(x).strip()]

        return ReviewResult(
            translations=translations,
            notes=notes,
            missing_info=missing,
            raw=content,
        )
