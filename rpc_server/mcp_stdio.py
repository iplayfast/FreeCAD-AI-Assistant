"""Standalone MCP server: bridges MCP-over-stdio to this addon's XML-RPC port.

A CLI's mcp_config points its ``command``/``args`` at this file, spawns it as a
subprocess, and speaks MCP's newline-delimited JSON-RPC 2.0 to it on stdin/
stdout. This process then forwards ``tools/list``/``tools/call`` to the
``FreeCADRPC`` XML-RPC server (rpc_server.py) at ``RPC_URL``, which is the same
registry the panel and the AI() console use — so a tool added anywhere shows up
here too, with no separate wiring.

Runs OUTSIDE FreeCAD as a plain subprocess (typically launched with the
system's own python3, not FreeCAD's bundled one), so it is deliberately
FreeCAD-free and stdlib-only, exactly like ``tools.py``. It is a bridge, not a
replacement: the addon's RPC server must already be running (Start RPC Server
in FreeCAD, or auto_start_rpc) or every call reports that it isn't.

Register it with an MCP-speaking CLI by pointing its config at this file:

    {"mcpServers": {"freecad": {"command": "python3", "args": ["<this file>"]}}}

See ``README.md`` for the exact config location per CLI.
"""

import json
import os
import sys
import xmlrpc.client

# Must match rpc_server.settings.RPC_HOST/RPC_PORT. Duplicated rather than
# imported: that module pulls in FreeCAD, which this process never has — so
# nothing here notices when the port is changed there, and the bridge just
# talks to a closed socket. $FREECAD_RPC_URL is the escape hatch: whoever
# changes the port sets it, and no CLI config has to be edited.
RPC_URL = os.environ.get("FREECAD_RPC_URL") or "http://127.0.0.1:9875"

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "freecad", "version": "1.0.0"}


def _rpc() -> xmlrpc.client.ServerProxy:
    return xmlrpc.client.ServerProxy(RPC_URL, allow_none=True)


def _unreachable(e: Exception) -> str:
    return (
        f"cannot reach the FreeCAD RPC server at {RPC_URL}: {e}. Start it from "
        "the AI Assistant toolbar in FreeCAD ('Start RPC Server'), or enable "
        "auto-start."
    )


def _text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _tool_content(result) -> list:
    """Shape a handler's return value into MCP content blocks.

    An image comes back from the registry as {"mime_type", "data"} with the PNG
    base64-encoded. JSON-dumped into a text block — which is what everything used
    to get — that is a few hundred KB of base64 the model cannot decode: the one
    tool that lets an agent SEE what it built delivered nothing it could look at.
    MCP has an image block; use it.
    """
    if (isinstance(result, dict) and result.get("data")
            and str(result.get("mime_type", "")).startswith("image/")):
        return [{"type": "image", "data": result["data"],
                 "mimeType": result["mime_type"]}]
    return [{"type": "text",
             "text": result if isinstance(result, str) else json.dumps(result)}]


def _handle(req: dict) -> dict | None:
    """One JSON-RPC request/notification in, one response dict or None out.

    ``None`` means "no reply" — correct for notifications (no ``id``), and for
    a request whose id is also missing (nothing to address a reply to).
    """
    method = req.get("method")
    req_id = req.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }}

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if req_id is None:
        return None  # an unrecognized notification: nothing to reply to

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        try:
            # The whole schema is ~27k characters, re-sent on every step of every
            # turn — more than the entire window of a local model started with
            # -c 2048. $FREECAD_TOOL_PROFILE advertises one of tools.PROFILES
            # instead ("Core only", "PartDesign", ...); call_tool still runs
            # anything. Sent only when set, so an older FreeCAD whose
            # describe_tools takes no argument still answers.
            profile = os.environ.get("FREECAD_TOOL_PROFILE")
            resp = _rpc().describe_tools(profile) if profile else _rpc().describe_tools()
        except Exception as e:
            return {"jsonrpc": "2.0", "id": req_id,
                     "error": {"code": -32000, "message": _unreachable(e)}}
        if not resp.get("success"):
            return {"jsonrpc": "2.0", "id": req_id, "error": {
                "code": -32000, "message": resp.get("error", "describe_tools failed")}}
        tools = [
            {"name": t["name"], "description": t["description"], "inputSchema": t["parameters"]}
            for t in resp["tools"]
        ]
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            resp = _rpc().call_tool(name, arguments)
        except Exception as e:
            return {"jsonrpc": "2.0", "id": req_id, "result": _text_result(_unreachable(e), True)}
        if not resp.get("success"):
            return {"jsonrpc": "2.0", "id": req_id,
                     "result": _text_result(str(resp.get("error")), True)}
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"content": _tool_content(resp.get("result")),
                           "isError": False}}

    return {"jsonrpc": "2.0", "id": req_id,
             "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> None:
    # MCP's stdio transport: one JSON-RPC message per line, no other framing.
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue  # not our concern to report; nothing has an id to reply to
        reply = _handle(req)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


def demo() -> None:
    """Offline check of the JSON-RPC dispatch (no FreeCAD, no real RPC server).

    Run: ``python3 rpc_server/mcp_stdio.py --demo``
    """
    import types

    global _rpc
    real_rpc = _rpc

    fake_tools = [{"name": "ping_tool", "description": "d", "parameters": {"type": "object", "properties": {}}}]
    fake = types.SimpleNamespace(
        describe_tools=lambda: {"success": True, "tools": fake_tools},
        call_tool=lambda name, args: {"success": True, "result": {"ok": True, "name": name}},
    )
    _rpc = lambda: fake  # noqa: E731  (swap the transport, not the dispatch under test)
    try:
        init = _handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert init["result"]["serverInfo"]["name"] == "freecad"

        assert _handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

        listed = _handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert listed["result"]["tools"][0]["inputSchema"] == fake_tools[0]["parameters"]

        # $FREECAD_TOOL_PROFILE reaches the server; unset, the call takes no
        # argument at all, so an older FreeCAD still answers it.
        asked = []
        fake.describe_tools = lambda *a: (asked.append(a),
                                          {"success": True, "tools": fake_tools})[1]
        _handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        os.environ["FREECAD_TOOL_PROFILE"] = "Core only"
        try:
            _handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        finally:
            del os.environ["FREECAD_TOOL_PROFILE"]
        assert asked == [(), ("Core only",)], asked

        called = _handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "ping_tool", "arguments": {}}})
        assert called["result"]["isError"] is False
        assert '"ok": true' in called["result"]["content"][0]["text"]

        # A screenshot must arrive as an image block, not as base64 in text —
        # otherwise the agent cannot see what it built.
        fake.call_tool = lambda name, args: {
            "success": True, "result": {"mime_type": "image/png", "data": "iVBOR"}}
        shot = _handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                         "params": {"name": "get_active_screenshot", "arguments": {}}})
        assert shot["result"]["content"] == [
            {"type": "image", "data": "iVBOR", "mimeType": "image/png"}], shot
        # ...but a dict that merely has a "data" key is still text.
        fake.call_tool = lambda name, args: {"success": True, "result": {"data": [1, 2]}}
        plain = _handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                          "params": {"name": "whatever", "arguments": {}}})
        assert plain["result"]["content"][0]["type"] == "text", plain

        unknown = _handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})
        assert unknown["error"]["code"] == -32601
    finally:
        _rpc = real_rpc

    def _refuse():
        raise ConnectionRefusedError("refused")
    _rpc = lambda: types.SimpleNamespace(describe_tools=_refuse)
    try:
        down = _handle({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
        assert "cannot reach the FreeCAD RPC server" in down["error"]["message"]
    finally:
        _rpc = real_rpc

    print("mcp_stdio.py demo: all checks passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--demo":
        demo()
    else:
        main()
