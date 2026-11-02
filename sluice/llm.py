"""LLM 客户端抽象：OpenAI 兼容 Chat Completions（纯标准库实现）+ 测试用 Mock。

内部统一使用「归一化消息格式」：
    assistant: {"role": "assistant", "content": str|None,
                "tool_calls": [{"id": str, "name": str, "arguments": dict}]}
    tool 结果: {"role": "tool", "tool_call_id": str, "content": str}
发往 API 前再转换为 OpenAI 原始格式。
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable


class LLMError(RuntimeError):
    """LLM 调用失败。"""


@dataclass
class LLMConfig:
    """一个模型端点的配置。judge / worker 可以各用一份。"""

    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.3
    json_mode: bool = False  # 是否使用 response_format={"type": "json_object"}
    timeout: int = 180
    extra_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, prefix: str = "SLUICE") -> "LLMConfig":
        """读取环境变量；prefix 可为 SLUICE_JUDGE / SLUICE_WORKER 以分别配置。"""
        def get(name: str, default: str = "") -> str:
            return os.environ.get(f"{prefix}_{name}") or os.environ.get(f"SLUICE_{name}") or default

        return cls(
            base_url=get("BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            api_key=get("API_KEY"),
            model=get("MODEL", "gpt-4o-mini"),
            temperature=float(get("TEMPERATURE", "0.3") or 0.3),
            json_mode=(get("JSON_MODE", "").lower() in ("1", "true", "yes")),
            timeout=int(get("TIMEOUT", "180") or 180),
        )


def to_api_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把内部归一化消息转换为 OpenAI API 格式。"""
    api: list[dict[str, Any]] = []
    for m in messages:
        if m.get("tool_calls"):
            api.append({
                "role": "assistant",
                "content": m.get("content") or "",
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                        },
                    }
                    for tc in m["tool_calls"]
                ],
            })
        else:
            api.append({k: v for k, v in m.items() if k in ("role", "content", "tool_call_id", "name")})
    return api


class LLMClient:
    """OpenAI 兼容端点的最小客户端（urllib，无第三方依赖）。"""

    def __init__(self, config: LLMConfig):
        self.config = config
        # token 用量累计（评测用：成本核算）
        self.usage: dict[str, int] = {"prompt": 0, "completion": 0, "calls": 0}

    def chat(self, messages: list[dict[str, Any]],
             tools: list[dict[str, Any]] | None = None,
             json_mode: bool | None = None,
             retries: int = 4) -> dict[str, Any]:
        """返回归一化的 assistant 消息。网络抖动（断连/超时/5xx）时指数退避重试。"""
        last: LLMError | None = None
        for attempt in range(retries):
            try:
                return self._chat_once(messages, tools, json_mode)
            except LLMError as e:
                last = e
                retriable = ("请求失败" in str(e) or "HTTP 5" in str(e)
                             or "HTTP 429" in str(e))
                if attempt < retries - 1 and retriable:
                    time.sleep(min(1.5 * 2 ** attempt, 10))
                elif not retriable:
                    break  # 4xx 等不可重试错误立即抛出
        raise last  # type: ignore[misc]

    def _chat_once(self, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None,
                   json_mode: bool | None) -> dict[str, Any]:
        cfg = self.config
        body: dict[str, Any] = {
            "model": cfg.model,
            "messages": to_api_messages(messages),
            "temperature": cfg.temperature,
        }
        if tools:
            body["tools"] = tools
        if json_mode if json_mode is not None else cfg.json_mode:
            body["response_format"] = {"type": "json_object"}

        req = urllib.request.Request(
            url=f"{cfg.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg.api_key}",
                **cfg.extra_headers,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:  # noqa: BLE001
            snippet = e.read().decode("utf-8", "replace")[:500]
            raise LLMError(f"LLM HTTP {e.code}: {snippet}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                ConnectionError, http.client.RemoteDisconnected) as e:
            raise LLMError(f"LLM 请求失败: {e}") from e

        try:
            msg = payload["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"LLM 响应结构异常: {str(payload)[:500]}") from e
        # 累计 token 用量（网关若返回 usage 则统计，用于成本核算）
        usage = payload.get("usage") or {}
        if isinstance(usage, dict):
            self.usage["prompt"] += int(usage.get("prompt_tokens") or 0)
            self.usage["completion"] += int(usage.get("completion_tokens") or 0)
            self.usage["calls"] += 1
        return self._normalize(msg)

    @staticmethod
    def _normalize(msg: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"role": "assistant", "content": msg.get("content")}
        calls = []
        for tc in msg.get("tool_calls") or []:
            raw_args = tc.get("function", {}).get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (json.JSONDecodeError, TypeError):
                args = {"_raw": raw_args}
            calls.append({"id": tc.get("id", f"call_{len(calls)}"),
                          "name": tc.get("function", {}).get("name", ""),
                          "arguments": args})
        if calls:
            out["tool_calls"] = calls
        return out


# ---------------------------------------------------------------- Mock

MockHandler = Callable[[list[dict[str, Any]], list[dict[str, Any]] | None], dict[str, Any]]


class MockLLM:
    """测试用 LLM：把每次调用转交给 handler(messages, tools)，handler 返回归一化消息。

    handler 可通过 messages[0]["content"]（系统提示词）区分当前是裁决者还是执行者。
    """

    def __init__(self, handler: MockHandler):
        self.handler = handler
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages, tools=None, json_mode=None) -> dict[str, Any]:
        self.calls.append(messages)
        reply = self.handler(messages, tools)
        assert reply.get("role") == "assistant", "Mock 必须返回 assistant 消息"
        return reply
