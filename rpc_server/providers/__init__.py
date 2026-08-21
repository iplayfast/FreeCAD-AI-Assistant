"""AI provider layer: a Provider protocol with two implementations.

  * AnthropicProvider   — native Claude Messages API
  * OpenAICompatProvider — OpenAI, Ollama, Gemini-compat, and anything else that
    speaks OpenAI /chat/completions (base_url + model + key)

``make_provider(config)`` builds one from saved settings. Providers are
FreeCAD-free and transport is stdlib urllib, so this package unit-tests offline
(``demo`` below) and adds no dependency to FreeCAD's embedded Python.
"""

import os

from rpc_server.providers.anthropic import AnthropicProvider
from rpc_server.providers.base import (
    AssistantReply,
    Message,
    ProviderError,
    ToolCall,
    get_json,
    post_json,
    set_wire_logger,
)
from rpc_server.providers.openai_compat import OpenAICompatProvider

__all__ = [
    "AnthropicProvider",
    "OpenAICompatProvider",
    "AssistantReply",
    "Message",
    "ProviderError",
    "ToolCall",
    "get_json",
    "make_provider",
    "set_wire_logger",
]


def make_provider(config: dict):
    """Build a provider from settings: {provider, model, base_url, api_key, timeout}."""
    kind = config.get("provider", "anthropic")
    timeout = float(config.get("timeout") or DEFAULT_TIMEOUT)
    if kind == "anthropic":
        key = config.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        return AnthropicProvider(
            api_key=key, model=config.get("model") or "claude-opus-5", timeout=timeout,
        )
    if kind == "openai_compat":
        key = config.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
        return OpenAICompatProvider(
            base_url=config.get("base_url", ""),
            model=config.get("model", ""),
            api_key=key,
            timeout=timeout,
        )
    raise ProviderError(f"unknown provider: {kind!r}")


def demo() -> None:
    """Offline self-check of the message translations (no network).

    Run from the addon root:
      python3 -c "from rpc_server.providers import demo; demo()"
    """
    from rpc_server.providers.anthropic import _from_anthropic, _to_anthropic
    from rpc_server.providers.openai_compat import _from_openai, _to_openai

    convo: list[Message] = [
        {"role": "user", "content": "make a box"},
        {"role": "assistant", "text": "sure",
         "tool_calls": [ToolCall("t1", "create_object", {"x": 1})]},
        {"role": "tool", "tool_call_id": "t1", "name": "create_object",
         "content": '{"object_name": "Box"}', "is_error": False},
        {"role": "user", "content": "now bigger"},
    ]

    # Anthropic: assistant carries a tool_use block; the tool result becomes its
    # own user message of tool_result blocks; the trailing user turn passes through.
    a = _to_anthropic(convo)
    assert a[1]["role"] == "assistant"
    assert any(b["type"] == "tool_use" for b in a[1]["content"]), a[1]
    assert a[2]["role"] == "user" and a[2]["content"][0]["type"] == "tool_result", a[2]
    assert a[3] == {"role": "user", "content": "now bigger"}, a[3]

    r = _from_anthropic({
        "content": [{"type": "text", "text": "hi"},
                    {"type": "tool_use", "id": "x", "name": "f", "input": {"a": 1}}],
        "stop_reason": "tool_use",
    })
    assert r.text == "hi" and r.stop == "tools" and r.tool_calls[0].name == "f", r

    # OpenAI-compat: system first, assistant tool_calls, tool result by id.
    o = _to_openai("SYS", convo)
    assert o[0] == {"role": "system", "content": "SYS"}, o[0]
    assert o[2]["role"] == "assistant" and o[2]["tool_calls"][0]["function"]["name"] == "create_object"
    assert o[3]["role"] == "tool" and o[3]["tool_call_id"] == "t1", o[3]

    r2 = _from_openai({"choices": [{"message": {
        "content": "done",
        "tool_calls": [{"id": "y", "function": {"name": "g", "arguments": '{"b": 2}'}}],
    }, "finish_reason": "tool_calls"}]})
    assert r2.tool_calls[0].name == "g" and r2.tool_calls[0].arguments == {"b": 2}
    assert r2.stop == "tools", r2

    # Some small local models (seen with Ollama + granite4.1:3b) never fill
    # tool_calls, and echo the call as JSON text in content instead — that
    # must still run as a tool call, not leak to the user as a raw JSON reply.
    r3 = _from_openai({"choices": [{"message": {
        "content": '{"name": "execute_code", "arguments": {"code": "1+1"}}',
    }, "finish_reason": "stop"}]})
    assert r3.stop == "tools", r3
    assert r3.tool_calls[0].name == "execute_code", r3
    assert r3.tool_calls[0].arguments == {"code": "1+1"}, r3
    assert r3.text == "", r3

    # Fenced JSON is unwrapped the same way.
    r4 = _from_openai({"choices": [{"message": {
        "content": '```json\n{"name": "list_documents", "arguments": {}}\n```',
    }, "finish_reason": "stop"}]})
    assert r4.tool_calls[0].name == "list_documents", r4

    # Ordinary prose is never mistaken for a tool call.
    r5 = _from_openai({"choices": [{"message": {"content": "Created the box."}}]})
    assert r5.stop == "end" and r5.tool_calls == [], r5

    # Wire tap: installed, called with both directions, and never able to break
    # the request it observes.
    from rpc_server.providers import base as _base

    seen = []
    set_wire_logger(lambda d, u, b: seen.append((d, u, b)))
    _base._tap("request", "http://x", {"model": "m"})
    _base._tap("response", "http://x", {"ok": True})
    assert [d for d, _, _ in seen] == ["request", "response"], seen

    def _boom(direction, url, body):
        raise RuntimeError("logger exploded")

    set_wire_logger(_boom)
    _base._tap("request", "http://x", {})   # must not raise
    set_wire_logger(None)
    before = len(seen)
    _base._tap("request", "http://x", {})
    assert len(seen) == before, "cleared logger must not be called"

    # Both timeouts must arrive as ProviderError. A read timeout raises bare
    # TimeoutError, which is not a URLError, so it used to escape _send entirely
    # and reach the user as "TimeoutError: timed out".
    import urllib.error
    import urllib.request

    from rpc_server.providers.base import _send

    real_urlopen = urllib.request.urlopen
    try:
        for boom in (TimeoutError("timed out"),
                     urllib.error.URLError(TimeoutError("timed out"))):
            urllib.request.urlopen = lambda *a, _b=boom, **k: (_ for _ in ()).throw(_b)
            try:
                _send(urllib.request.Request("http://x"), "http://x", 12)
            except ProviderError as e:
                assert "within 12s" in str(e), e
                assert "ai_timeout" in str(e), e
            else:
                raise AssertionError(f"{boom!r} did not become a ProviderError")
    finally:
        urllib.request.urlopen = real_urlopen

    print("providers demo: all checks passed")
