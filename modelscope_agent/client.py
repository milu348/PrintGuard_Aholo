"""大模型客户端：ModelScope API-Inference（OpenAI 兼容 /v1/chat/completions）。

独立包 ``modelscope_agent`` 的协议层：只做三件事——发请求、解析 tool_calls、
把错误翻译成人能读的提示。鉴权 token 由部署方通过 PRINTGUARD_LLM_TOKEN
（或 MODELSCOPE_API_TOKEN）提供，代码不内置任何密钥。

依赖方向：本包（演示层）→ printguard_aholo.config.Settings（读配置）；
printguard_aholo 核心不 import 本包——演示层整体可删除。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import requests

from printguard_aholo.config import Settings

logger = logging.getLogger("modelscope_agent.client")


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


@dataclass
class LLMClient:
    """chat/completions 客户端。字段来自 Settings，可整体替换便于测试。"""

    base_url: str
    model: str
    token: str
    timeout_sec: float = 120.0
    max_attempts: int = 4
    retry_backoff_sec: float = 8.0

    @classmethod
    def from_settings(cls, settings: Settings) -> "LLMClient":
        return cls(base_url=settings.llm_base_url, model=settings.llm_model,
                   token=settings.llm_token,
                   timeout_sec=settings.llm_timeout_sec,
                   max_attempts=settings.llm_max_attempts,
                   retry_backoff_sec=settings.llm_retry_backoff_sec)

    def _require_token(self) -> str:
        if not self.token:
            raise LLMError(
                "未配置大模型 token：请在创空间环境变量里设置 "
                "PRINTGUARD_LLM_TOKEN（ModelScope API-Inference token）")
        return self.token

    def _parse(self, resp) -> ChatResult:
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

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             *, model: str | None = None, temperature: float = 0.3,
             timeout: float | None = None) -> ChatResult:
        """调用 chat/completions，返回文本 + 工具调用。tools 为空则不启用函数调用。"""
        token = self._require_token()
        body: dict = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        last_error = "未知"
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"},
                    json=body, timeout=timeout or self.timeout_sec)
            except requests.RequestException as exc:
                last_error = f"网络异常 {type(exc).__name__}: {exc}"
            else:
                if resp.status_code in (401, 403):
                    raise LLMError(f"大模型鉴权失败（HTTP {resp.status_code}）："
                                   "请检查 token 与模型权限")
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}"
                elif resp.status_code >= 400:
                    raise LLMError(
                        f"大模型返回 HTTP {resp.status_code}：{resp.text[:200]}")
                else:
                    try:
                        return self._parse(resp)
                    except LLMError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        last_error = f"响应解析失败 {type(exc).__name__}: {exc}"
            if attempt < self.max_attempts:
                delay = self.retry_backoff_sec * attempt
                logger.warning("大模型调用第 %d 次失败（%s），%.0fs 后重试",
                               attempt, last_error, delay)
                time.sleep(delay)
        raise LLMError(
            f"大模型调用失败（重试 {self.max_attempts} 次）：{last_error}")
