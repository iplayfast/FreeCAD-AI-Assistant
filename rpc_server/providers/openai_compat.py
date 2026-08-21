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
    DEFAULT_TIMEOUT,
    post_json,
)


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(self, base_url: str, model: str, api_key: str = "",
                 timeout: float = DEFAULT_TIMEOUT):
        if not base_url:
            raise ProviderError("OpenAI-compatible provider needs a base_url.")
        if not model:
            raise ProviderError("OpenAI-compatible provider needs a model.")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._key = api_key
        self._timeout = timeout

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
        resp = post_json(f"{self.base_url}/chat/completions", headers, payload,
                         timeout=self._timeout)
        return _from_openai(resp)


def _to_openai(system: str, messages: list[Message]) -> list[dict]:
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m.get("content") or ""})
        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("text") or ""}
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
                {"role": "tool", "tool_call_id": m.get("tool_call_id") or "", "content": m.get("content") or ""}
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

    if not tool_calls:
        recovered = _recover_tool_call(text)
        if recovered:
            tool_calls, text = [recovered], ""

    stop = "tools" if tool_calls else "end"
    return AssistantReply(text=text, tool_calls=tool_calls, stop=stop)


def _recover_tool_call(text: str) -> ToolCall | None:
    """A tool call the model wrote as JSON text instead of ``tool_calls``.

    Some small/local models (seen with Ollama + granite4.1:3b) never populate
    the structured ``tool_calls`` field even when clearly attempting a call —
    they just echo ``{"name": ..., "arguments": {...}}`` as the message body.
    Recovered here so the call still runs instead of leaking as a raw JSON
    blob the user has to read as if it were the model's reply.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").removeprefix("json").strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    name = obj.get("name")
    arguments = obj.get("arguments", {})
    if not isinstance(name, str) or not name or not isinstance(arguments, dict):
        return None
    return ToolCall(id="recovered-0", name=name, arguments=arguments)
