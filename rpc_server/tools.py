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


_REGISTRY: dict[str, Tool] = {}


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
        _REGISTRY[name] = Tool(name, description, parameters, fn)
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


def describe_tools() -> list[dict[str, Any]]:
    """The wire-facing description consumed by AI providers and the MCP server.

    Shape (``name``/``description``/``parameters``) is intentionally the common
    denominator across the Anthropic tool spec, the OpenAI function/tool spec,
    and MCP's ``inputSchema`` — each adapter maps this into its own envelope.
    """
    load_handlers()
    return [
        {"name": t.name, "description": t.description, "parameters": t.parameters}
        for t in _REGISTRY.values()
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

    missing = [k for k in required if k not in args]
    if missing:
        raise ValueError(
            f"tool '{name}' missing required argument(s): {', '.join(missing)}; "
            f"accepts: {', '.join(accepted) or 'no arguments'}"
        )

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

        print("tools.py demo: all checks passed")
    finally:
        load_handlers = real_load
        _REGISTRY.clear()
        _REGISTRY.update(saved)


if __name__ == "__main__":
    demo()
