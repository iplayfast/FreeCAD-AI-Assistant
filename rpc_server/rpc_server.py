import FreeCAD
import FreeCADGui

import contextlib
import base64
import io
import os
import tempfile
import threading
from typing import Any
from xmlrpc.server import SimpleXMLRPCServer

from PySide import QtCore

from rpc_server.commands import register_commands, schedule_toggle_sync
from rpc_server.fem_executor import run_fem_analysis as _run_fem_analysis
from rpc_server.gui_dispatch import (
    cleanup_waker,
    dispatch_to_gui,
    ensure_gui_pump,
    post_to_gui,
    request_shutdown,
)
from rpc_server.exec_env import exec_namespace
from rpc_server.object_factory import create_object_gui
from rpc_server.parts_library import get_parts_list, insert_part_from_library
from rpc_server.property_mapper import Object, set_object_property
from rpc_server.serialize import serialize_object
from rpc_server.settings import RPC_HOST, RPC_PORT, load_settings, save_settings
from rpc_server.view_manager import save_active_screenshot

rpc_server_thread = None
rpc_server_instance = None


def _run(task, **ok_fields) -> dict:
    """Dispatch a mutation ``task`` to the GUI thread and shape the wire result.

    ``task`` returns ``True`` on success or raises on failure (the dispatcher
    re-raises the task's exception, or ``TimeoutError`` on timeout). Success
    becomes ``{"success": True, **ok_fields}``; any exception becomes
    ``{"success": False, "error": ...}``.
    """
    try:
        dispatch_to_gui(task)
    except Exception as e:
        return {"success": False, "error": str(e)}
    return {"success": True, **ok_fields}


class FreeCADRPC:
    """RPC server for FreeCAD"""
    TIMEOUT = 60               # generous wait for GUI thread to become free
    EXECUTE_CODE_TIMEOUT = 90  # GUI-thread execution; use execute_code_async for heavy OCCT ops

    def ping(self):
        return True

    def describe_tools(self) -> dict:
        """List every handler registered in the tool registry (name/description/schema)."""
        try:
            from rpc_server import tools
            return {"success": True, "tools": tools.describe_tools()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        """Invoke a registered handler by name on the GUI thread.

        This is the generic bridge: any handler added to ``tool_handlers`` becomes
        callable over RPC without a new RPC method.
        """
        try:
            from rpc_server import tools
            result = dispatch_to_gui(
                lambda: tools.call_tool(name, arguments or {}),
                timeout=self.EXECUTE_CODE_TIMEOUT,
            )
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def create_document(self, name="New_Document"):
        return _run(lambda: self._create_document_gui(name), document_name=name)

    def create_object(self, doc_name, obj_data: dict[str, Any]):
        obj = Object(
            name=obj_data.get("Name", "New_Object"),
            type=obj_data["Type"],
            analysis=obj_data.get("Analysis", None),
            properties=obj_data.get("Properties", {}),
        )
        return _run(lambda: self._create_object_gui(doc_name, obj), object_name=obj.name)

    def edit_object(self, doc_name: str, obj_name: str, properties: dict[str, Any]) -> dict[str, Any]:
        obj = Object(
            name=obj_name,
            properties=properties.get("Properties", {}),
        )
        return _run(lambda: self._edit_object_gui(doc_name, obj), object_name=obj.name)

    def delete_object(self, doc_name: str, obj_name: str):
        return _run(lambda: self._delete_object_gui(doc_name, obj_name), object_name=obj_name)

    def reload_document(self, doc_name: str) -> dict[str, Any]:
        """Close and re-open a document by name to pick up external file
        changes (e.g. edits made by another process such as `freecadcmd`
        running headlessly). Returns success once the new document is
        loaded from disk.
        """
        return _run(lambda: self._reload_document_gui(doc_name), document_name=doc_name)

    def run_fem_analysis(self, doc_name: str, analysis_name: str, timeout: int = 600) -> dict[str, Any]:
        """Run the CalculiX solver on an existing Fem::FemAnalysis and return summary results."""
        try:
            timeout_s = int(timeout)
        except (TypeError, ValueError):
            return {"success": False, "error": f"invalid timeout: {timeout!r}"}
        # The handler returns a rich result dict of its own; on dispatch
        # timeout/failure, shape the raised exception into the same envelope.
        try:
            return dispatch_to_gui(
                lambda: self._run_fem_analysis_gui(doc_name, analysis_name),
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
        def _show(msg):
            post_to_gui(lambda: FreeCADGui.getMainWindow().statusBar().showMessage(msg))

        def worker() -> None:
            # No sys.stdout redirect here: contextlib.redirect_stdout swaps stdout
            # process-wide, not per-thread, so it would race with the GUI thread.
            # Background code should report via FreeCAD.Console (thread-safe).
            _show("MCP: running background task…")
            try:
                exec(code, exec_namespace())
                FreeCAD.Console.PrintMessage("Async code execution completed.\n")
            except Exception as e:
                import traceback as _tb
                FreeCAD.Console.PrintError(f"Async code error: {e}\n{_tb.format_exc()}")
            finally:
                post_to_gui(lambda: FreeCADGui.getMainWindow().statusBar().clearMessage())

        threading.Thread(target=worker, daemon=True).start()
        return {"success": True, "message": "Background execution started."}

    def execute_code(self, code: str) -> dict[str, Any]:
        """Execute Python code on the GUI thread and wait for the result.

        Runs on the GUI thread so that FreeCAD document operations
        (addObject, recompute, save) are safe and correctly ordered.
        Use execute_code_async for heavy OCCT boolean ops (fuse/cut)
        that would block the GUI thread too long.
        """
        output_buffer = io.StringIO()

        def task():
            with contextlib.redirect_stdout(output_buffer):
                exec(code, exec_namespace())
            return True

        try:
            dispatch_to_gui(task, timeout=self.EXECUTE_CODE_TIMEOUT)
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
            "message": "Python code executed successfully.\nOutput: " + output_buffer.getvalue(),
        }

    def get_objects(self, doc_name):
        # Serialization touches obj.ViewObject, a GUI API that FreeCAD 1.x only
        # permits on the main thread, so run it there via dispatch_to_gui.
        def task():
            # FreeCAD.getDocument raises (not returns None) for an unknown name.
            try:
                doc = FreeCAD.getDocument(doc_name)
            except Exception:
                return []
            return [serialize_object(obj) for obj in doc.Objects]

        try:
            return dispatch_to_gui(task)
        except Exception:
            return []

    def get_object(self, doc_name, obj_name):
        # See get_objects: serialization must run on the GUI thread.
        def task():
            try:
                doc = FreeCAD.getDocument(doc_name)
            except Exception:
                return None
            obj = doc.getObject(obj_name)
            return serialize_object(obj) if obj else None

        try:
            return dispatch_to_gui(task)
        except Exception:
            return None

    def insert_part_from_library(self, relative_path):
        return _run(
            lambda: self._insert_part_from_library(relative_path),
            message="Part inserted from library.",
        )

    def list_documents(self):
        return list(FreeCAD.listDocuments().keys())

    def get_parts_list(self):
        return get_parts_list()

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
        fd, tmp_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)

        def task():
            try:
                active_view = FreeCADGui.ActiveDocument.ActiveView
            except Exception:
                return False
            if active_view is None or not hasattr(active_view, "saveImage"):
                view_type = type(active_view).__name__ if active_view is not None else "None"
                FreeCAD.Console.PrintWarning(
                    f"MCP RPC: view type '{view_type}' does not support screenshots\n"
                )
                return False
            return save_active_screenshot(tmp_path, view_name, width, height, focus_object)

        try:
            try:
                ok = dispatch_to_gui(task)
            except Exception as e:
                FreeCAD.Console.PrintWarning(f"MCP RPC: screenshot failed: {e}\n")
                return None
            if ok is True:
                with open(tmp_path, "rb") as f:
                    return base64.b64encode(f.read()).decode("utf-8")
            return None  # task returned False: view does not support screenshots
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def _create_document_gui(self, name):
        doc = FreeCAD.newDocument(name)
        doc.recompute()
        FreeCAD.Console.PrintMessage(f"Document '{name}' created via RPC.\n")
        return True

    def _create_object_gui(self, doc_name, obj: Object):
        return create_object_gui(doc_name, obj)

    def _edit_object_gui(self, doc_name: str, obj: Object):
        try:
            doc = FreeCAD.getDocument(doc_name)
        except Exception:
            raise ValueError(f"Document '{doc_name}' not found.")

        obj_ins = doc.getObject(obj.name)
        if not obj_ins:
            raise ValueError(f"Object '{obj.name}' not found in document '{doc_name}'.")

        set_object_property(doc, obj_ins, obj.properties)  # raises on failure
        doc.recompute()
        FreeCAD.Console.PrintMessage(f"Object '{obj.name}' updated via RPC.\n")
        return True

    def _run_fem_analysis_gui(self, doc_name: str, analysis_name: str):
        return _run_fem_analysis(doc_name, analysis_name)

    def _delete_object_gui(self, doc_name: str, obj_name: str):
        try:
            doc = FreeCAD.getDocument(doc_name)
        except Exception:
            raise ValueError(f"Document '{doc_name}' not found.")

        doc.removeObject(obj_name)  # raises if the object does not exist
        doc.recompute()
        FreeCAD.Console.PrintMessage(f"Object '{obj_name}' deleted via RPC.\n")
        return True

    def _reload_document_gui(self, doc_name: str):
        if doc_name not in FreeCAD.listDocuments():
            raise ValueError(f"Document '{doc_name}' is not loaded.")
        doc = FreeCAD.getDocument(doc_name)
        file_path = doc.FileName
        if not file_path:
            raise ValueError(
                f"Document '{doc_name}' has no file on disk "
                "(unsaved scratch document); nothing to reload from."
            )
        if not os.path.exists(file_path):
            raise ValueError(f"File for '{doc_name}' not found at {file_path!r}.")
        # Close, then reopen from the same file. Reopen preserves the
        # original document name when the file was previously saved
        # under that name.
        FreeCAD.closeDocument(doc_name)
        FreeCAD.openDocument(file_path)
        FreeCAD.Console.PrintMessage(
            f"Document '{doc_name}' reloaded from '{file_path}' via RPC.\n"
        )
        return True

    def _insert_part_from_library(self, relative_path):
        insert_part_from_library(relative_path)  # raises on failure
        return True


def start_rpc_server(port=RPC_PORT):
    # Bound to loopback only: this server runs exec() of arbitrary Python, so it
    # must never be reachable off the local machine.
    global rpc_server_thread, rpc_server_instance

    if rpc_server_instance:
        return "RPC Server already running."

    host = RPC_HOST
    rpc_server_instance = SimpleXMLRPCServer(
        (host, port), allow_none=True, logRequests=False
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


register_commands()
schedule_toggle_sync()
