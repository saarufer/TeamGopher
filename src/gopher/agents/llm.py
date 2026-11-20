"""LLM 适配器（provider-agnostic）。默认 Anthropic Claude；无凭证时可用 offline 规则基线。

LLM 只用于解释、假设生成与证据审查；交易、现金与审计由确定性代码负责（§15 方向 E）。
"""

from __future__ import annotations

import json
import re
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    name: str

    def structured(self, system: str, user: str, schema: type[T], *, model: str | None = None) -> T: ...

    def research_with_web(self, system: str, user: str, *, max_uses: int = 5) -> tuple[str, set[str]]: ...


def _strict_schema(schema: dict) -> dict:
    """结构化输出要求每个 object 都 additionalProperties=false 且列出全部 required。"""
    defs = schema.get("$defs", {})

    def fix(node):
        if isinstance(node, dict):
            node.pop("title", None)
            node.pop("default", None)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for k in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
                node.pop(k, None)
            for v in node.values():
                fix(v)
        elif isinstance(node, list):
            for v in node:
                fix(v)

    fix(schema)
    for d in defs.values():
        fix(d)
    return schema


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, model: str = "claude-opus-5", effort: str = "high", max_tokens: int = 16000):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover
            raise LLMError("需要安装可选依赖：uv sync --extra llm") from e
        self._anthropic = anthropic
        self.client = anthropic.Anthropic()  # 凭证：ANTHROPIC_API_KEY 或 `ant auth login` 的 profile
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    def _create(self, **kwargs):
        a = self._anthropic
        try:
            with self.client.beta.messages.stream(
                max_tokens=self.max_tokens,
                thinking={"type": "adaptive"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                **kwargs,
            ) as stream:
                resp = stream.get_final_message()
        except a.AuthenticationError as e:
            raise LLMError("Anthropic 认证失败：设置 ANTHROPIC_API_KEY 或运行 `ant auth login`") from e
        except a.RateLimitError as e:
            raise LLMError("Anthropic 速率限制，请稍后重试") from e
        except a.APIStatusError as e:
            raise LLMError(f"Anthropic API 错误 {e.status_code}: {e.message}") from e
        except a.APIConnectionError as e:
            raise LLMError(f"无法连接 Anthropic API: {e}") from e
        if resp.stop_reason == "refusal":
            raise LLMError("模型拒绝了该请求（refusal）")
        if resp.stop_reason == "max_tokens":
            raise LLMError("输出被 max_tokens 截断")
        return resp

    def structured(self, system: str, user: str, schema: type[T], *, model: str | None = None) -> T:
        js = _strict_schema(schema.model_json_schema())
        resp = self._create(
            model=model or self.model,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": js}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            return schema.model_validate_json(text)
        except ValidationError as e:
            raise LLMError(f"LLM 输出不符合 {schema.__name__} schema: {e}") from e

    def research_with_web(self, system: str, user: str, *, max_uses: int = 5) -> tuple[str, set[str]]:
        """使用 web_search 服务器工具；返回最终文本与本次真实检索到的 URL 集合（用于防编造来源）。"""
        messages = [{"role": "user", "content": user}]
        urls: set[str] = set()
        texts: list[str] = []
        for _ in range(5):  # pause_turn 续跑
            resp = self._create(
                model=self.model, system=system, messages=messages,
                tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}],
                output_config={"effort": self.effort},
            )
            for b in resp.content:
                if b.type == "web_search_tool_result" and isinstance(b.content, list):
                    urls.update(r.url for r in b.content if getattr(r, "type", "") == "web_search_result")
                elif b.type == "text":
                    texts.append(b.text)
            if resp.stop_reason != "pause_turn":
                break
            messages = [*messages, {"role": "assistant", "content": resp.content}]
        return "\n".join(texts), urls


def extract_json_block(text: str) -> dict:
    m = re.findall(r"```json\s*(.*?)```", text, flags=re.S)
    raw = m[-1] if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise LLMError(f"无法解析 JSON: {e}") from e


def make_llm(cfg: dict) -> LLM | None:
    llm_cfg = cfg.get("llm", {})
    if llm_cfg.get("provider", "anthropic") == "offline":
        return None
    return AnthropicLLM(model=llm_cfg.get("model", "claude-opus-5"), effort=llm_cfg.get("effort", "high"),
                        max_tokens=int(llm_cfg.get("max_tokens", 16000)))
