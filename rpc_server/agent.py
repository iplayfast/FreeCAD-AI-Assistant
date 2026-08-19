"""The agent loop: provider + tool registry, driven to completion.

Provider-and-FreeCAD-agnostic. It calls a provider for one assistant turn,
runs any requested tool calls through an injected ``run_tool`` callback, feeds
the results back, and repeats until the model stops asking for tools. The
console runs this on a worker thread (network I/O off the GUI thread) and wires
``run_tool`` to ``dispatch_to_gui(lambda: call_tool(...))`` so tools execute on
the GUI thread. Tests pass a fake provider and fake ``run_tool``.

``run`` is a generator of event dicts so the console can render progress live.
"""

import json
from typing import Any, Callable

from rpc_server.providers.base import Message, ProviderError
from rpc_server.tools import describe_tools

DEFAULT_SYSTEM = (
    "You are a FreeCAD embedded AI. You help the user build and "
    "modify 3D CAD models by calling the provided tools. Prefer the typed tools; "
    "use execute_code only for what the tools don't cover. Type ids and object "
    "names are case-sensitive (e.g. 'Part::Box'). When you finish, briefly tell "
    "the user what you did.\n"
    "The user needs to be able to modify the parts, so build parts as PartDesign "
    "bodies, not as Part booleans. execute_code has readyPart, createSketch, "
    "createPad, createPocket and createPolar already in scope for exactly this "
    "— read that tool's description before writing geometry code. Reach for "
    "Part primitives only for throwaway shapes or when PartDesign genuinely "
    "cannot express the result."
)


def _tool_content(result: Any) -> str:
    """Serialise a tool result for the model. Images aren't stringified into the
    context — a placeholder goes to the model; the console shows the real image."""
    if isinstance(result, dict) and str(result.get("mime_type", "")).startswith("image/") \
            and "data" in result:
        return "[image displayed to the user]"
    return json.dumps(result, default=str)


class AgentSession:
    def __init__(
        self,
        provider,
        run_tool: Callable[[str, dict], Any],
        tools: list[dict] | None = None,
        system: str = DEFAULT_SYSTEM,
        max_steps: int = 25,
    ):
        self.provider = provider
        self._run_tool = run_tool
        self.tools = describe_tools() if tools is None else tools
        self.system = system
        self.max_steps = max_steps
        self.messages: list[Message] = []

    def run(self, user_text: str):
        """Drive one user turn to completion, yielding event dicts.

        Events: {"type": "assistant", "text"}, {"type": "tool_call", id/name/arguments},
        {"type": "tool_result", id/name/result/content/is_error},
        {"type": "done", "stop"}, {"type": "error", "message"}.
        """
        self.messages.append({"role": "user", "content": user_text})

        for _ in range(self.max_steps):
            try:
                reply = self.provider.complete(self.system, self.messages, self.tools)
            except ProviderError as e:
                yield {"type": "error", "message": str(e)}
                return

            self.messages.append(
                {"role": "assistant", "text": reply.text, "tool_calls": reply.tool_calls}
            )
            if reply.text:
                yield {"type": "assistant", "text": reply.text}

            if reply.stop != "tools" or not reply.tool_calls:
                yield {"type": "done", "stop": reply.stop}
                return

            for tc in reply.tool_calls:
                yield {"type": "tool_call", "id": tc.id, "name": tc.name, "arguments": tc.arguments}
                try:
                    result = self._run_tool(tc.name, tc.arguments)
                    content, is_error = _tool_content(result), False
                except Exception as e:
                    result = f"{type(e).__name__}: {e}"
                    content, is_error = result, True
                self.messages.append({
                    "role": "tool", "tool_call_id": tc.id, "name": tc.name,
                    "content": content, "is_error": is_error,
                })
                yield {
                    "type": "tool_result", "id": tc.id, "name": tc.name,
                    "result": result, "content": content, "is_error": is_error,
                }

        yield {"type": "error", "message": f"stopped after {self.max_steps} steps"}


def demo() -> None:
    """Offline self-check of the loop (fake provider + fake run_tool, no network).

    Run from the addon root:
      python3 -c "from rpc_server.agent import demo; demo()"
    """
    from rpc_server.providers.base import AssistantReply, ToolCall

    class _Script:
        def __init__(self, replies):
            self._replies = list(replies)
        def complete(self, system, messages, tools):
            return self._replies.pop(0)

    tool_defs = [{"name": "create_document", "description": "",
                  "parameters": {"type": "object", "properties": {}}}]

    # Happy path: one tool call, then a final text turn.
    executed = []
    replies = [
        AssistantReply(tool_calls=[ToolCall("t1", "create_document", {"name": "D"})], stop="tools"),
        AssistantReply(text="Created D.", stop="end"),
    ]
    s = AgentSession(_Script(replies), lambda n, a: executed.append((n, a)) or {"document_name": a["name"]},
                     tools=tool_defs)
    kinds = [e["type"] for e in s.run("make D")]
    assert executed == [("create_document", {"name": "D"})], executed
    assert kinds == ["tool_call", "tool_result", "assistant", "done"], kinds
    # tool result was fed back to the model
    assert any(m["role"] == "tool" for m in s.messages), s.messages

    # Error path: a raising tool becomes an is_error result, loop continues.
    def _boom(name, args):
        raise ValueError("boom")
    replies2 = [
        AssistantReply(tool_calls=[ToolCall("t2", "x", {})], stop="tools"),
        AssistantReply(text="ok", stop="end"),
    ]
    s2 = AgentSession(_Script(replies2), _boom, tools=[])
    evs = list(s2.run("go"))
    tr = next(e for e in evs if e["type"] == "tool_result")
    assert tr["is_error"] and "boom" in tr["content"], tr

    # Provider failure surfaces as an error event.
    class _Fail:
        def complete(self, *a):
            raise ProviderError("no key")
    ev = list(AgentSession(_Fail(), lambda n, a: None, tools=[]).run("hi"))
    assert ev == [{"type": "error", "message": "no key"}], ev

    # Image results aren't stringified into the model context.
    assert _tool_content({"mime_type": "image/png", "data": "AAAA" * 99}) == "[image displayed to the user]"

    print("agent demo: all checks passed")
