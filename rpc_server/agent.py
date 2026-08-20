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
    "You are a FreeCAD embedded AI. You help the user build and modify 3D CAD "
    "models by calling the provided tools. Type ids and object names are "
    "case-sensitive (e.g. 'PartDesign::Body', 'App::VarSet'). When you finish, briefly tell the user "
    "what you did.\n"
    "The deliverable is a PARAMETRIC document the user can edit afterwards: "
    "PartDesign bodies with constrained sketches and dimensions driven by a VarSet.\n"
    "Prefer using the typed tools directly:\n"
    "  * create_document: create or open a document\n"
    "  * create_varset: create an App::VarSet and initialize dimension variables in one step\n"
    "  * create_body: create a PartDesign::Body\n"
    "  * create_sketch: add a sketch attached to a body origin plane (XY_Plane, XZ_Plane, YZ_Plane)\n"
    "  * sketch_circle / sketch_polygon: add fully-constrained geometry. Concentric circles on the same sketch pad into a hollow ring/tube in one step!\n"
    "  * pad / pocket / polar_pattern: create 3D solid features or circular patterns\n"
    "  * create_sphere: create solid sphere features (e.g. for bearing balls at PitchDia/2, then polar_pattern around Z)\n"
    "  * create_cylinder: create solid cylinder features (e.g. for pins, rollers, shafts)\n"
    "When using execute_code, use the built-in global helpers (readyPart/create_body, createSketch, addCircle, createPad, createPocket, createSphere, createCylinder, createPolar, readyVars/create_varset, addVar). "
    "Do NOT hand-write raw FreeCAD C++ property bindings or OpenCASCADE imports."
)


SELECT_SYSTEM = (
    "You choose which tools another assistant will be given for a FreeCAD task. "
    "Reply with nothing but the tool names, separated by commas. Choose every "
    "tool the task plausibly needs, including the ones needed to set up for it — "
    "a task that pads a sketch also needs to create the document, body and sketch. "
    "Choosing a few too many is much better than missing one."
)

# Sent whatever the selection says. A turn that cannot create a document or fall
# back to running Python is stuck no matter which specialised tool was picked.
ALWAYS_SELECTED = ("list_documents", "create_document", "execute_code")

# Identical consecutive failures that mean the model is not going to get there
# on its own. A step ceiling stops a slow build as readily as a stuck one; what
# is worth stopping is the absence of progress, and the same tool answering with
# the same error three times running is what that looks like.
STUCK_REPEATS = 3


def _summary(tool: dict, limit: int = 90) -> str:
    """First sentence of a description, for the selection catalogue."""
    text = " ".join((tool.get("description") or "").split())
    return text.split(". ")[0][:limit]


def select_tools(provider, user_text: str, tools: list = None) -> list:
    """Ask the model which tools this request needs, and return just those.

    The full schema is ~6,700 tokens and is re-sent on every step of the turn;
    the name-and-summary catalogue this asks against is ~870, and is sent once.
    A six-step turn goes from ~40,000 tokens to ~9,000, and the subset still fits
    a context that the whole schema does not — which is the real point, because
    it means a small local model gets `pad` and `sketch_polygon` rather than
    whichever fixed profile happened to fit.

    Never returns nothing: an unparseable answer falls back to the full set, on
    the grounds that a slow turn beats a turn that cannot do the job.
    """
    tools = describe_tools() if tools is None else tools
    catalogue = "\n".join(
        f"{t['name']}: {_summary(t)}" for t in sorted(tools, key=lambda t: t["name"])
    )
    prompt = f"Tools:\n{catalogue}\n\nTask:\n{user_text}\n\nTool names:"
    try:
        reply = provider.complete(SELECT_SYSTEM, [{"role": "user", "content": prompt}], [])
    except ProviderError:
        return tools

    # Substring matching rather than parsing a list: models answer this with
    # bullets, prose, or JSON depending on the day, and every one of those still
    # contains the names.
    answered = (reply.text or "")
    chosen = {t["name"] for t in tools if t["name"] in answered}
    chosen |= {t["name"] for t in tools if t["name"] in ALWAYS_SELECTED}
    picked = [t for t in tools if t["name"] in chosen]
    return picked if len(picked) > len(ALWAYS_SELECTED) else tools


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
        # ``tools`` is what gets sent; ``all_tools`` is the pool it was chosen
        # from, kept so a per-turn selection narrows from the configured profile
        # rather than from whatever the last turn happened to settle on.
        self.all_tools = describe_tools() if tools is None else tools
        self.tools = list(self.all_tools)
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
        last_failure, repeats = None, 0

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

                # Arguments are deliberately not part of the signature: varying
                # them while the error stays identical is still not progress.
                signature = (tc.name, content[:200]) if is_error else None
                repeats = repeats + 1 if signature and signature == last_failure else int(bool(signature))
                last_failure = signature
                if repeats >= STUCK_REPEATS:
                    yield {"type": "error", "message": (
                        f"{tc.name} failed {repeats} times with the same error, so "
                        f"this is not progressing: {content[:200]}"
                    )}
                    return

        yield {"type": "error", "message": (
            f"stopped after {self.max_steps} steps. Raise 'ai_max_steps' in the "
            "settings file if the task genuinely needs more, or ask for one part "
            "of it at a time."
        )}


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

    # select_tools: the answer format is whatever the model felt like, so the
    # parse has to survive prose, bullets and JSON, and never return nothing.
    catalogue = [
        {"name": n, "description": f"does {n}.", "parameters": {"type": "object", "properties": {}}}
        for n in ("create_document", "create_body", "create_sketch", "pad",
                  "execute_code", "list_documents", "run_fem_analysis", "search_wiki")
    ]

    class _Says:
        def __init__(self, text):
            self.text = text

        def complete(self, system, messages, tools):
            assert tools == [], "the selection call must not send the schema it is choosing from"
            return AssistantReply(text=self.text, stop="end")

    for answer in ("create_body, create_sketch, pad",
                   "- create_body\n- create_sketch\n- pad\n",
                   '{"tools": ["create_body", "create_sketch", "pad"]}',
                   "You'll want create_body, then create_sketch, then pad."):
        picked = {t["name"] for t in select_tools(_Says(answer), "make a plate", catalogue)}
        assert {"create_body", "create_sketch", "pad"} <= picked, (answer, picked)
        assert set(ALWAYS_SELECTED) <= picked, (answer, picked)
        assert "run_fem_analysis" not in picked, (answer, picked)

    # Nothing recognisable, or a dead provider: fall back to everything rather
    # than to a turn that cannot act.
    assert select_tools(_Says("I am not sure."), "x", catalogue) == catalogue

    class _Dead:
        def complete(self, *a):
            raise ProviderError("down")

    assert select_tools(_Dead(), "x", catalogue) == catalogue

    # Stuck detection: the same tool failing the same way is not progress, and
    # the run should say so rather than spend the whole step budget on it.
    boom = [AssistantReply(tool_calls=[ToolCall(f"t{i}", "pad", {"n": i})], stop="tools")
            for i in range(10)]

    def always_fails(name, args):
        raise ValueError("Sketch 'X' is over-constrained")

    events = list(AgentSession(_Script(boom), always_fails, tools=tool_defs).run("go"))
    errors = [e for e in events if e["type"] == "error"]
    assert errors and "not progressing" in errors[-1]["message"], errors
    # Stopped at the repeat threshold, not at max_steps.
    assert len([e for e in events if e["type"] == "tool_call"]) == STUCK_REPEATS, events

    # Varying the arguments while the error stays the same is still not progress.
    varied = [AssistantReply(tool_calls=[ToolCall(f"v{i}", "pad", {"n": i * 99})], stop="tools")
              for i in range(10)]
    events = list(AgentSession(_Script(varied), always_fails, tools=tool_defs).run("go"))
    assert any("not progressing" in e.get("message", "") for e in events), events

    # A failure that changes each time is someone making progress; let it run.
    def different_each_time(name, args):
        raise ValueError(f"distinct failure {args['n']}")

    events = list(AgentSession(_Script(boom), different_each_time, tools=tool_defs,
                               max_steps=4).run("go"))
    assert "stopped after 4 steps" in events[-1]["message"], events[-1]

    print("agent demo: all checks passed")
