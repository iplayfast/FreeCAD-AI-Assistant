import FreeCAD

import threading
from typing import Any
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

from .commands import register_commands, schedule_toggle_sync
from .gui_dispatch import (
    cleanup_waker,
    dispatch_to_gui,
    ensure_gui_pump,
    request_shutdown,
)
from .settings import RPC_HOST, RPC_PORT

rpc_server_thread = None
rpc_server_instance = None


class FreeCADRPC:
    """RPC server for FreeCAD.

    Every method here is a thin shim over the tool registry: it translates the
    XML-RPC argument shape in and the ``{"success": ...}`` envelope out, and
    ``rpc_server.tools`` does the work. They used to be second implementations —
    ``_edit_object_gui`` was ``tool_handlers.edit_object`` with a different log
    string — which meant the panel and the wire could disagree about what one
    operation does, and every registry fix (uniquified object names, syntax
    errors pointing at a line, "document not found" naming the document) stopped
    at the registry. The registry is the one that gets maintained, so the wire
    now calls it.

    The methods are kept, rather than leaving only ``call_tool``, because they
    are a published interface: scripts and older MCP packages call them by name.
    """

    TIMEOUT = 60               # generous wait for GUI thread to become free
    EXECUTE_CODE_TIMEOUT = 90  # GUI-thread execution; use execute_code_async for heavy OCCT ops

    def _tool(self, name: str, arguments: dict = None, timeout: float = None):
        """Run a registry tool on the GUI thread. Returns its value, or raises."""
        from . import tools

        return dispatch_to_gui(
            lambda: tools.call_tool(name, arguments or {}),
            timeout=self.EXECUTE_CODE_TIMEOUT if timeout is None else timeout,
        )

    def _wrapped(self, name: str, arguments: dict = None, timeout: float = None,
                 shape=None) -> dict:
        """``_tool`` in the legacy envelope: {"success": True, ...} or an error.

        ``shape`` turns the tool's return value into the extra fields the old
        method promised — the callers below are the only place those legacy
        field names still exist.
        """
        try:
            result = self._tool(name, arguments, timeout)
        except Exception as e:
            return {"success": False, "error": str(e)}
        return {"success": True, **(shape(result) if shape else {})}

    def _quiet(self, name: str, arguments: dict = None, default=None):
        """``_tool`` for the read methods that answer with a bare value.

        They return ``[]``/``None`` on failure instead of raising, because that
        is what they have always done and callers branch on it.
        """
        try:
            return self._tool(name, arguments)
        except Exception as e:
            # Swallowed for the caller, but not silent: a screenshot that comes
            # back None because the active view is a spreadsheet is otherwise
            # indistinguishable from one that came back None because the tool
            # raised, and neither says so anywhere.
            FreeCAD.Console.PrintWarning(f"MCP RPC: {name} failed: {e}\n")
            return default

    def ping(self):
        return True

    def describe_tools(self, profile: str = None) -> dict:
        """List handlers from the tool registry (name/description/schema).

        ``profile`` names a subset from ``tools.PROFILES``; omitted or unknown
        means everything. Advertising less never restricts what ``call_tool``
        will run — it only shortens the schema, which is what a model with a
        small context window needs.
        """
        try:
            from . import tools
            return {"success": True, "tools": tools.describe_tools(profile)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        """Invoke a registered handler by name on the GUI thread.

        This is the generic bridge, and the one to prefer: any handler added to
        the registry is callable over RPC without a new RPC method.
        """
        try:
            return {"success": True, "result": self._tool(name, arguments)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def create_document(self, name="New_Document"):
        # document_name is what FreeCAD actually called it: it sanitises and
        # uniquifies ("Bearing 608" -> "Bearing_608"), and the old method echoed
        # back the requested string, so every later call named a document that
        # did not exist.
        return self._wrapped("create_document", {"name": name},
                             shape=lambda r: {"document_name": r["document_name"]})

    def create_object(self, doc_name, obj_data: dict[str, Any]):
        return self._wrapped(
            "create_object",
            {"doc_name": doc_name,
             "obj_type": obj_data["Type"],
             "obj_name": obj_data.get("Name", "New_Object"),
             "properties": obj_data.get("Properties", {}),
             "analysis_name": obj_data.get("Analysis")},
            # Likewise uniquified: two "Race"s give Race and Race001.
            shape=lambda r: {"object_name": r["object_name"]},
        )

    def edit_object(self, doc_name: str, obj_name: str, properties: dict[str, Any]) -> dict[str, Any]:
        # Callers pass either {"Properties": {...}} or the properties directly.
        props = properties.get("Properties", properties)
        return self._wrapped(
            "edit_object",
            {"doc_name": doc_name, "obj_name": obj_name, "properties": props},
            shape=lambda r: {"object_name": r["object_name"]},
        )

    def delete_object(self, doc_name: str, obj_name: str):
        return self._wrapped("delete_object", {"doc_name": doc_name, "obj_name": obj_name},
                             shape=lambda r: {"object_name": obj_name})

    def reload_document(self, doc_name: str) -> dict[str, Any]:
        """Close and re-open a document by name to pick up external file
        changes (e.g. edits made by another process such as `freecadcmd`
        running headlessly). Returns success once the new document is
        loaded from disk.
        """
        return self._wrapped("reload_document", {"doc_name": doc_name},
                             shape=lambda r: {"document_name": r["document_name"]})

    def run_fem_analysis(self, doc_name: str, analysis_name: str, timeout: int = 600) -> dict[str, Any]:
        """Run the CalculiX solver on an existing Fem::FemAnalysis and return summary results."""
        try:
            timeout_s = int(timeout)
        except (TypeError, ValueError):
            return {"success": False, "error": f"invalid timeout: {timeout!r}"}
        # The handler returns a rich result dict of its own, passed straight
        # through. Solving outlasts the default GUI-dispatch wait, which is what
        # the timeout argument is for.
        try:
            return self._tool(
                "run_fem_analysis",
                {"doc_name": doc_name, "analysis_name": analysis_name},
                timeout=timeout_s,
            )
        except Exception as e:
            return {"success": False, "error": str(e)}

    def execute_code_async(self, code: str) -> dict[str, Any]:
        """Run code on a background thread and return immediately.

        For long OCCT computations (fuse/cut/loft) that would freeze the GUI or
        exceed the MCP timeout. The heavy geometry math runs off the GUI thread,
        so FreeCAD stays responsive while it computes.

        IMPORTANT: document mutation (addObject, recompute, ViewObject, save) is
        NOT safe on a background thread. The executed code is given a
        ``post_to_gui`` callable in its namespace — hand it a zero-arg function to
        run the document-writing part on the GUI thread::

            result = shapeA.fuse(shapeB)          # heavy, background-safe
            post_to_gui(lambda: _apply(result))   # touches the document

        The caller should poll a document object for completion status.
        """
        return self._wrapped("execute_code_async", {"code": code},
                             shape=lambda r: {"message": r["message"]})

    def execute_code(self, code: str) -> dict[str, Any]:
        """Execute Python code on the GUI thread and wait for the result.

        Runs on the GUI thread so that FreeCAD document operations
        (addObject, recompute, save) are safe and correctly ordered.
        Use execute_code_async for heavy OCCT boolean ops (fuse/cut)
        that would block the GUI thread too long.
        """
        try:
            result = self._tool("execute_code", {"code": code})
        except Exception as e:
            # Log the offending code (truncated) to make errors traceable
            code_preview = code if len(code) <= 800 else code[:800] + "\n...(truncated)"
            FreeCAD.Console.PrintError(
                f"Error executing Python code: {e}\n"
                f"--- code ---\n{code_preview}\n--- end ---\n"
            )
            return {"success": False, "error": str(e)}
        FreeCAD.Console.PrintMessage("Python code executed successfully.\n")
        return {
            "success": True,
            "message": "Python code executed successfully.\nOutput: " + result.get("output", ""),
        }

    def get_objects(self, doc_name):
        return self._quiet("get_objects", {"doc_name": doc_name}, default=[])

    def get_object(self, doc_name, obj_name):
        return self._quiet("get_object", {"doc_name": doc_name, "obj_name": obj_name})

    def insert_part_from_library(self, relative_path):
        return self._wrapped("insert_part_from_library", {"relative_path": relative_path},
                             shape=lambda r: {"message": r["message"]})

    def list_documents(self):
        return self._quiet("list_documents", default=[])

    def get_parts_list(self):
        return self._quiet("get_parts_list", default=[])

    def get_active_screenshot(
        self,
        view_name: str = "Isometric",
        width: int | None = None,
        height: int | None = None,
        focus_object: str | None = None,
    ) -> str:
        """Get a screenshot of the active view as a base64-encoded PNG string.

        Returns None if the active view does not support screenshots
        (e.g., TechDraw or Spreadsheet workbench).
        """
        shot = self._quiet("get_active_screenshot", {
            "view_name": view_name, "width": width, "height": height,
            "focus_object": focus_object,
        })
        return shot["data"] if shot else None


class _LocalOnlyHandler(SimpleXMLRPCRequestHandler):
    """Refuse anything a web page could have sent.

    Loopback is not the boundary it looks like. A page you visit in any browser
    can POST cross-origin to 127.0.0.1 without a CORS preflight, as long as it
    uses a "simple" content type — it cannot read the reply, but by then
    execute_code has already run arbitrary Python inside FreeCAD. Verified: a
    text/plain POST carrying an XML-RPC body reaches the handler untouched.

    Two headers close it, and no credential has to be invented:

      * a cross-origin fetch always carries Origin, and a real XML-RPC client
        never does;
      * text/xml is not a simple content type, so requiring it forces a
        preflight, which this server does not answer.
    """

    def do_POST(self):
        origin = self.headers.get("Origin")
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if origin or content_type not in ("text/xml", "application/xml"):
            FreeCAD.Console.PrintWarning(
                f"MCP RPC: refused a request that looks browser-driven "
                f"(Origin={origin!r}, Content-Type={content_type!r})\n"
            )
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return super().do_POST()


def start_rpc_server(port=RPC_PORT):
    # Bound to loopback only: this server runs exec() of arbitrary Python, so it
    # must never be reachable off the local machine.
    global rpc_server_thread, rpc_server_instance

    if rpc_server_instance:
        return "RPC Server already running."

    host = RPC_HOST
    rpc_server_instance = SimpleXMLRPCServer(
        (host, port), requestHandler=_LocalOnlyHandler, allow_none=True, logRequests=False
    )
    rpc_server_instance.register_instance(FreeCADRPC())

    def server_loop():
        # No announcement here. Every caller already prints what this function
        # returns, so saying it again logged one event as two lines - and said it
        # from a worker thread, which is not where FreeCAD.Console wants to be
        # called from.
        rpc_server_instance.serve_forever()

    rpc_server_thread = threading.Thread(target=server_loop, daemon=True)
    rpc_server_thread.start()

    ensure_gui_pump()

    return f"RPC Server started at {host}:{port}."


def stop_rpc_server():
    global rpc_server_instance, rpc_server_thread

    if rpc_server_instance:
        request_shutdown()
        cleanup_waker()
        rpc_server_instance.shutdown()
        rpc_server_thread.join()
        rpc_server_instance = None
        rpc_server_thread = None
        FreeCAD.Console.PrintMessage("RPC Server stopped.\n")
        return "RPC Server stopped."

    return "RPC Server was not running."


def demo() -> None:
    """Self-check of the wire guard: browser-shaped requests must not get in.

    Runs a throwaway server on an ephemeral port, so it never disturbs a real
    one. Run: ``freecadcmd run_checks.py``.
    """
    import urllib.error
    import urllib.request
    import xmlrpc.client

    server = SimpleXMLRPCServer(("127.0.0.1", 0), requestHandler=_LocalOnlyHandler,
                                allow_none=True, logRequests=False)
    server.register_function(lambda: "RAN", "ping")
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        call = (b"<?xml version='1.0'?><methodCall><methodName>ping</methodName>"
                b"<params></params></methodCall>")

        def post(headers) -> int:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/", data=call, method="POST", headers=headers)
            try:
                return urllib.request.urlopen(request, timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code

        # The attack: a "simple" content type needs no CORS preflight, so any
        # page in any browser can send this to a loopback port. It must not run.
        assert post({"Content-Type": "text/plain;charset=UTF-8"}) == 403
        # Origin is present on every cross-origin fetch and on no real client.
        assert post({"Content-Type": "text/xml", "Origin": "https://evil.example"}) == 403
        # ...and a genuine client still works, which is the whole constraint.
        assert post({"Content-Type": "text/xml"}) == 200
        proxy = xmlrpc.client.ServerProxy(f"http://127.0.0.1:{port}", allow_none=True)
        assert proxy.ping() == "RAN"
    finally:
        server.shutdown()
        server.server_close()

    print("rpc_server demo: all checks passed", flush=True)


register_commands()
schedule_toggle_sync()
