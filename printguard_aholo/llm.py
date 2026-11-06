"""大模型客户端：ModelScope API-Inference（OpenAI 兼容 /v1/chat/completions）。

只做三件事：发请求、解析 tool_calls、把错误翻译成人能看懂的提示。
鉴权 token 由部署方通过 PRINTGUARD_LLM_TOKEN（或 MODELSCOPE_API_TOKEN）提供，
代码不内置任何密钥。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import requests

import time

from .config import (LLM_BASE_URL, LLM_MAX_ATTEMPTS, LLM_MODEL, LLM_RETRY_BACKOFF_SEC,
                     LLM_TOKEN, LLM_TIMEOUT_SEC)

logger = logging.getLogger("printguard.llm")


class LLMError(RuntimeError):
    """大模型调用失败（网络 / 鉴权 / 配额 / 返回结构异常）。"""


@dataclass
class ToolCall:
    """一次工具调用请求（OpenAI tool_calls 结构）。"""

    id: str
    name: str
    arguments: dict

    @classmethod
    def from_raw(cls, raw: dict) -> "ToolCall":
        function = raw.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {"_raw": function.get("arguments")}
        if not isinstance(arguments, dict):
            arguments = {"_raw": arguments}
        return cls(id=str(raw.get("id") or ""), name=str(function.get("name") or ""),
                   arguments=arguments)


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall]
    raw: dict


def require_token() -> str:
    if not LLM_TOKEN:
        raise LLMError(
            "未配置大模型 token：请在创空间环境变量里设置 "
            "PRINTGUARD_LLM_TOKEN（ModelScope API-Inference token）")
    return LLM_TOKEN


def _parse(resp) -> ChatResult:
    """解析 chat/completions 响应为 ChatResult。"""
    try:
        payload = resp.json()
    except ValueError as exc:
        raise LLMError(f"大模型返回非 JSON：{resp.text[:200]}") from exc

    choices = payload.get("choices") or []
    if not choices:
        raise LLMError(f"大模型返回缺少 choices：{str(payload)[:200]}")
    message = (choices[0] or {}).get("message") or {}
    tool_calls = [ToolCall.from_raw(item)
                  for item in (message.get("tool_calls") or [])]
    return ChatResult(content=str(message.get("content") or "").strip(),
                      tool_calls=tool_calls, raw=payload)


def chat(messages: list[dict], tools: list[dict] | None = None,
         *, model: str | None = None, temperature: float = 0.3,
         timeout: float | None = None) -> ChatResult:
    """调用 chat/completions，返回文本 + 工具调用。tools 为空则不启用函数调用。"""
    token = require_token()
    body: dict = {
        "model": model or LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    last_error = "未知"
    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json=body, timeout=timeout or LLM_TIMEOUT_SEC)
        except requests.RequestException as exc:
            last_error = f"网络异常 {type(exc).__name__}: {exc}"
        else:
            if resp.status_code in (401, 403):
                raise LLMError(f"大模型鉴权失败（HTTP {resp.status_code}）："
                               "请检查 token 与模型权限")
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
            elif resp.status_code >= 400:
                raise LLMError(f"大模型返回 HTTP {resp.status_code}：{resp.text[:200]}")
            else:
                try:
                    return _parse(resp)
                except LLMError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    last_error = f"响应解析失败 {type(exc).__name__}: {exc}"
        if attempt < LLM_MAX_ATTEMPTS:
            delay = LLM_RETRY_BACKOFF_SEC * attempt
            logger.warning("大模型调用第 %d 次失败（%s），%.0fs 后重试",
                           attempt, last_error, delay)
            time.sleep(delay)
    raise LLMError(f"大模型调用失败（重试 {LLM_MAX_ATTEMPTS} 次）：{last_error}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise LLMError(f"大模型返回非 JSON：{resp.text[:200]}") from exc

    choices = payload.get("choices") or []
    if not choices:
        raise LLMError(f"大模型返回缺少 choices：{str(payload)[:200]}")
    message = (choices[0] or {}).get("message") or {}
    tool_calls = [ToolCall.from_raw(item)
                  for item in (message.get("tool_calls") or [])]
    return ChatResult(content=str(message.get("content") or "").strip(),
                      tool_calls=tool_calls, raw=payload)


def smoke() -> dict:
    """探活：不带 tools 问一句，判断 token/模型是否可用（前端设置页用）。"""
    result = chat([{"role": "user", "content": "用一句话说明你能做什么。"}])
    return {"model": LLM_MODEL, "reply": result.content[:200]}
