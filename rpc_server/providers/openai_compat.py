"""OpenAI Chat Completions adapter.

One adapter for every backend that speaks the OpenAI /chat/completions format:
OpenAI itself, Ollama (base_url http://localhost:11434/v1), Gemini's OpenAI-compat
endpoint, OpenRouter, LM Studio, vLLM, and so on. Provider choice is just a
base_url + model + key.
"""

import json
from typing import Any

from rpc_server.providers.base import (
    AssistantReply,
    Message,
    ProviderError,
    ToolCall,
    post_json,
)


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(self, base_url: str, model: str, api_key: str = ""):
        if not base_url:
            raise ProviderError("OpenAI-compatible provider needs a base_url.")
        if not model:
            raise ProviderError("OpenAI-compatible provider needs a model.")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._key = api_key

    def complete(self, system: str, messages: list[Message], tools: list[dict]) -> AssistantReply:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _to_openai(system, messages),
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["parameters"],
                    },
                }
                for t in tools
            ]
        headers = {}
        if self._key:  # Ollama and other local servers accept requests with no key
            headers["Authorization"] = f"Bearer {self._key}"
        resp = post_json(f"{self.base_url}/chat/completions", headers, payload)
        return _from_openai(resp)


def _to_openai(system: str, messages: list[Message]) -> list[dict]:
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("text") or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in m["tool_calls"]
                ]
            out.append(msg)
        elif role == "tool":
            out.append(
                {"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]}
            )
    return out


def _from_openai(resp: dict) -> AssistantReply:
    try:
        message = resp["choices"][0]["message"]
    except (KeyError, IndexError) as e:
        raise ProviderError(f"unexpected chat-completions response: {e}") from None
    text = message.get("content") or ""
    tool_calls = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        tool_calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args))
    stop = "tools" if tool_calls else "end"
    return AssistantReply(text=text, tool_calls=tool_calls, stop=stop)
