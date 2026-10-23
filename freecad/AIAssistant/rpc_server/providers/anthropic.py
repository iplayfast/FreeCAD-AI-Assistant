# SPDX-License-Identifier: LGPL-2.1-or-later
"""Native Anthropic Messages API adapter.

Wire contract per the Anthropic Messages API: POST /v1/messages with
x-api-key + anthropic-version headers; tools use ``input_schema``; the response
``content`` is a list of blocks (text / tool_use); ``stop_reason`` is
end_turn / tool_use / refusal / max_tokens. No temperature/top_p/top_k and no
budget_tokens (rejected on current models).

# ponytail: thinking omitted for now (adaptive thinking needs thinking blocks
# echoed back unchanged across tool turns — add when we carry them in Message).
"""

from typing import Any

from .base import (
    AssistantReply,
    Message,
    ProviderError,
    ToolCall,
    DEFAULT_TIMEOUT,
    post_json,
)

_URL = "https://api.anthropic.com/v1/messages"
_VERSION = "2023-06-01"
_DEFAULT_MODEL = "claude-opus-5"
_MAX_TOKENS = 16000


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str = _DEFAULT_MODEL,
                 timeout: float = DEFAULT_TIMEOUT):
        if not api_key:
            raise ProviderError("Anthropic API key not set.")
        self._key = api_key
        self.model = model
        self._timeout = timeout

    def complete(self, system: str, messages: list[Message], tools: list[dict]) -> AssistantReply:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": _MAX_TOKENS,
            "messages": _to_anthropic(messages),
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                for t in tools
            ]
        headers = {"x-api-key": self._key, "anthropic-version": _VERSION}
        resp = post_json(_URL, headers, payload, timeout=self._timeout)
        return _from_anthropic(resp)


def _to_anthropic(messages: list[Message]) -> list[dict]:
    """Translate neutral messages to Anthropic's content-block format.

    Consecutive ``tool`` results are merged into a single user message of
    tool_result blocks, as the API requires.
    """
    out: list[dict] = []
    pending: list[dict] = []

    def flush() -> None:
        if pending:
            out.append({"role": "user", "content": list(pending)})
            pending.clear()

    for m in messages:
        role = m["role"]
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": m["tool_call_id"],
                "content": m["content"],
            }
            if m.get("is_error"):
                block["is_error"] = True
            pending.append(block)
            continue
        flush()
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            content: list[dict] = []
            if m.get("text"):
                content.append({"type": "text", "text": m["text"]})
            for tc in m.get("tool_calls", []):
                content.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                )
            out.append({"role": "assistant", "content": content})
    flush()
    return out


def _from_anthropic(resp: dict) -> AssistantReply:
    blocks = resp.get("content", [])
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    tool_calls = [
        ToolCall(id=b["id"], name=b["name"], arguments=b.get("input", {}))
        for b in blocks
        if b.get("type") == "tool_use"
    ]
    reason = resp.get("stop_reason")
    stop = {"tool_use": "tools", "refusal": "refusal", "max_tokens": "length"}.get(reason, "end")
    return AssistantReply(text=text, tool_calls=tool_calls, stop=stop)
