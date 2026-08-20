"""The tool registry: the single source of truth for FreeCAD operations.

Each operation is defined exactly once here — name, description, JSON-Schema
parameters, and a handler — and every "face" consumes this same registry:

  * the in-FreeCAD AI console (phase 1) calls ``call_tool`` directly, and
  * the MCP / RPC server (phase 2) exposes the same set via ``describe_tools``.

Because the description lives next to the behaviour, the two faces cannot drift
apart the way the old split addon/MCP-package layout did.

This module is deliberately FreeCAD-free: it is pure registry plumbing so it can
be introspected and unit-tested without a running FreeCAD (see ``demo`` below).
The actual operations register themselves from ``tool_handlers`` at addon
startup.

Threading contract: a handler runs synchronously and assumes it is already on
the thread where FreeCAD document mutation is legal (the GUI thread). The caller
(console agent loop or RPC dispatch) is responsible for marshalling via
``dispatch_to_gui``. Handlers return a JSON-serialisable value or raise.
"""

import contextlib
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema (an object schema) for the arguments
    handler: Callable[..., Any]
    category: str = "core"


_REGISTRY: dict[str, Tool] = {}

# The whole schema is ~27,000 characters and is re-sent on every step of every
# turn. That is a fifth of a 32k context before anything is said, and more than
# the entire window of a local server started with -c 2048 — which is not a
# tidiness problem, it is why small local models cannot use this addon at all.
# Profiles let a session advertise a subset. Nothing here restricts what can be
# *called*: call_tool always sees the full registry, so a tool that is not
# advertised still works if something asks for it by name.

# Which category a tool lands in follows the module that defines it, so a new
# tool is grouped correctly without being listed anywhere. The exception is the
# document/object module, which is half essentials and half occasional
# machinery; naming the occasional half is less churn than annotating the rest,
# and anything new there defaults to "core", i.e. visible in every profile.
_MODULE_CATEGORIES = {
    "tool_handlers": "core",
    "partdesign_handlers": "partdesign",
    "inspect_handlers": "inspect",
    "bim_handlers": "bim",
}

# Category by module is right except where a tool's module is an accident of
# where it was written. The reference lookups are defined next to the BIM tools
# but answer questions about all of FreeCAD, and a session that cannot look up
# the scripted API is exactly the session that guesses at it.
_CATEGORY_OVERRIDES = {
    "lookup_freecad_op": "core",
    "search_wiki": "core",
}

_EXTRAS = {
    "create_text_label", "get_active_screenshot", "get_dependency_graph",
    "export_file", "execute_code_async", "run_fem_analysis",
    "insert_part_from_library", "reload_document", "read_file", "get_parts_list",
}

# None means "no filter". A profile always includes core: a session that cannot
# create or save a document is not a working session.
PROFILES: dict[str, set | None] = {
    "Everything": None,
    "Core only": {"core"},
    "PartDesign": {"core", "partdesign"},
    "PartDesign + inspection": {"core", "partdesign", "inspect"},
    "BIM": {"core", "bim"},
    "Core + extras": {"core", "extras"},
}


def register(name: str, description: str, parameters: dict[str, Any]) -> Callable:
    """Decorator that registers ``fn`` as the handler for tool ``name``.

    ``parameters`` is a JSON-Schema object (``{"type": "object", ...}``); its
    ``properties`` keys must match the handler's keyword arguments.
    """
    if parameters.get("type") != "object" or "properties" not in parameters:
        raise ValueError(
            f"tool '{name}': parameters must be a JSON-Schema object with 'properties'."
        )

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in _REGISTRY:
            raise ValueError(f"tool '{name}' is already registered.")
        module = (fn.__module__ or "").rsplit(".", 1)[-1]
        category = _CATEGORY_OVERRIDES.get(name) or (
            "extras" if name in _EXTRAS else _MODULE_CATEGORIES.get(module, "core"))
        _REGISTRY[name] = Tool(name, description, parameters, fn, category)
        return fn

    return deco


def load_handlers() -> None:
    """Import the handler modules so their ``@register`` decorators run.

    Called from ``describe_tools`` and ``call_tool`` rather than at module
    import, because those modules need FreeCAD and this one deliberately does
    not. Every consumer (RPC bridge, console agent) reaches the registry through
    one of those two functions, so registering here is what keeps the faces from
    seeing different tool sets — which is exactly what happened when each caller
    did its own imports.
    """
    # A plain import is already idempotent (sys.modules caches it), and it
    # PROPAGATES. Swallowing the failure here loses a whole category of tools
    # with no error anywhere, which is the drift this function exists to stop.
    from rpc_server import (  # noqa: F401  (import for side effect: registration)
        bim_handlers,
        inspect_handlers,
        partdesign_handlers,
        tool_handlers,
    )


def all_tools() -> list[Tool]:
    return list(_REGISTRY.values())


def get_tool(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def describe_tools(profile: str = None) -> list[dict[str, Any]]:
    """The wire-facing description consumed by AI providers and the MCP server.

    Shape (``name``/``description``/``parameters``) is intentionally the common
    denominator across the Anthropic tool spec, the OpenAI function/tool spec,
    and MCP's ``inputSchema`` — each adapter maps this into its own envelope.

    ``profile`` names a subset from ``PROFILES``; omitted, or unknown, means the
    whole registry. Unknown falls back to everything rather than raising, because
    a stale profile in a settings file should cost prompt size, not a dead panel.
    """
    load_handlers()
    allowed = PROFILES.get(profile, None) if profile else None
    return [
        {"name": t.name, "description": t.description, "parameters": t.parameters}
        for t in _REGISTRY.values()
        if allowed is None or t.category in allowed
    ]


@contextlib.contextmanager
def transaction(name: str):
    """Group everything inside into one undo step, rolled back if it raises.

    ``setActiveTransaction`` is lazy — it only opens a real transaction once a
    document is actually modified — so wrapping read-only tools costs nothing
    and leaves no empty entries in the undo stack.

    Imported lazily so the registry stays usable (and ``demo`` runnable) without
    FreeCAD; outside FreeCAD this is a no-op.
    """
    try:
        import FreeCAD
    except ImportError:
        yield
        return

    FreeCAD.setActiveTransaction(name)
    try:
        yield
    except Exception:
        # A half-applied feature is worse than none: the next tool call then
        # builds on debris and the failure surfaces somewhere else entirely.
        FreeCAD.closeActiveTransaction(True)
        raise
    else:
        FreeCAD.closeActiveTransaction()


def call_tool(name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Validate ``arguments`` against the tool's required params and invoke it.

    Runs the handler on the current thread (see the module threading contract).
    Raises ``KeyError`` for an unknown tool, ``ValueError`` for missing required
    arguments, and propagates whatever the handler raises.
    """
    load_handlers()
    tool = _REGISTRY.get(name)
    if tool is None:
        raise KeyError(f"unknown tool: {name!r}")

    args = dict(arguments or {})
    accepted = tool.parameters.get("properties", {})
    required = tool.parameters.get("required", [])

    # Pure synonyms for the SAME value, so they may satisfy a required argument.
    for synonym in ("doc", "document"):
        if synonym in args and "doc_name" in accepted and "doc_name" not in args:
            args["doc_name"] = args.pop(synonym)

    missing = [k for k in required if k not in args]
    if missing:
        raise ValueError(
            f"tool '{name}' missing required argument(s): {', '.join(missing)}; "
            f"accepts: {', '.join(accepted) or 'no arguments'}"
        )

    # These cross-fill between DIFFERENT things — a sketch's own name and the
    # name of the body it sits on — so they run only after the required check.
    # Before it they silently satisfied it: create_sketch(body_name='Ring') with
    # no `name` produced a sketch called RingSketch instead of saying what was
    # missing, and a `pad` missing both names filled them from one stray `name=`.
    for a, b in (("name", "body_name"), ("name", "sketch_name"),
                 ("name", "varset_name"), ("name", "obj_name")):
        if a in args and b not in args and b in accepted:
            args[b] = args[a]
        elif b in args and a not in args and a in accepted:
            args[a] = args[b]

    # Unknown keys used to reach the handler and surface as a bare
    # "got an unexpected keyword argument 'x'" TypeError, which names the
    # Python function rather than the tool and never says what IS accepted.
    unknown = [k for k in args if k not in accepted]
    if unknown:
        raise ValueError(
            f"tool '{name}' got unknown argument(s): {', '.join(unknown)}; "
            f"accepts: {', '.join(accepted) or 'no arguments'}"
        )

    with transaction(name):
        return tool.handler(**args)


def demo() -> None:
    """Offline self-check of the registry plumbing (no FreeCAD needed).

    Run: ``python3 rpc_server/tools.py``
    """
    saved = dict(_REGISTRY)
    _REGISTRY.clear()
    global load_handlers
    real_load = load_handlers
    load_handlers = lambda: None   # the real one needs FreeCAD; this demo does not
    try:
        @register(
            "echo",
            "Echo back the given text.",
            {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "text to echo"}},
                "required": ["text"],
            },
        )
        def _echo(text: str) -> dict:
            return {"echoed": text}

        # describe_tools shape
        desc = describe_tools()
        assert desc == [
            {
                "name": "echo",
                "description": "Echo back the given text.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string", "description": "text to echo"}},
                    "required": ["text"],
                },
            }
        ], desc

        # happy path
        assert call_tool("echo", {"text": "hi"}) == {"echoed": "hi"}

        # missing required arg
        try:
            call_tool("echo", {})
            assert False, "expected ValueError for missing 'text'"
        except ValueError as e:
            assert "text" in str(e)

        # unknown argument: named as a tool argument, listing what is accepted,
        # rather than a bare TypeError naming the Python function.
        try:
            call_tool("echo", {"text": "hi", "cleanup": True})
            assert False, "expected ValueError for unknown argument"
        except ValueError as e:
            assert "cleanup" in str(e) and "accepts: text" in str(e), e

        # the missing-argument message says what is accepted too
        try:
            call_tool("echo", {})
            assert False, "expected ValueError"
        except ValueError as e:
            assert "accepts: text" in str(e), e

        # unknown tool
        try:
            call_tool("nope", {})
            assert False, "expected KeyError for unknown tool"
        except KeyError:
            pass

        # duplicate registration rejected
        try:
            register("echo", "dup", {"type": "object", "properties": {}})(lambda: None)
            assert False, "expected ValueError for duplicate registration"
        except ValueError:
            pass

        # malformed schema rejected
        try:
            register("bad", "d", {"type": "string"})(lambda: None)
            assert False, "expected ValueError for non-object schema"
        except ValueError:
            pass

        # transaction() is a no-op without FreeCAD, and re-raises either way.
        with transaction("noop"):
            pass
        try:
            with transaction("boom"):
                raise RuntimeError("x")
            assert False, "expected the error to propagate"
        except RuntimeError:
            pass

        # Profiles filter what is advertised, never what can be run.
        @register("only_padded", "Category comes from the defining module.",
                  {"type": "object", "properties": {}})
        def _padded() -> dict:
            return {}

        _REGISTRY["only_padded"] = Tool(
            "only_padded", "d", {"type": "object", "properties": {}},
            _REGISTRY["only_padded"].handler, "partdesign",
        )
        assert {t["name"] for t in describe_tools()} >= {"echo", "only_padded"}
        assert "only_padded" not in {t["name"] for t in describe_tools("Core only")}
        assert "only_padded" in {t["name"] for t in describe_tools("PartDesign")}
        # ...and an unadvertised tool still runs, so narrowing a profile can never
        # break a caller that asks for one by name.
        assert call_tool("only_padded", {}) == {}
        # An unknown profile means everything, not nothing.
        assert describe_tools("no such profile") == describe_tools()

        print("tools.py demo: all checks passed")
    finally:
        load_handlers = real_load
        _REGISTRY.clear()
        _REGISTRY.update(saved)


if __name__ == "__main__":
    demo()
