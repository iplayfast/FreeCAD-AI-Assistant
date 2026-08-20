"""FreeCAD operation handlers, registered into the tool registry.

Importing this module (at addon startup, inside FreeCAD) populates
``tools._REGISTRY`` with the canonical document/object operations. Each handler
runs on the GUI thread (the caller marshals via ``dispatch_to_gui``), returns a
JSON-serialisable value, and raises on failure — the same "return-or-raise"
contract the RPC layer already uses.

This is the single source of truth for these operations. The RPC methods in
``rpc_server`` will be reduced to thin delegators over ``tools.call_tool`` in a
follow-up; until then their private ``_*_gui`` copies are the transitional
duplicate.  # ponytail: transitional dup, remove when RPC delegates to registry
"""

import base64
import contextlib
import io
import os
import tempfile
import threading

import FreeCAD

from rpc_server.exec_env import exec_namespace
from rpc_server.fem_executor import run_fem_analysis as _fem_run
from rpc_server.object_factory import create_object_gui
from rpc_server.parts_library import get_parts_list as _lib_parts_list
from rpc_server.parts_library import insert_part_from_library as _lib_insert_part
from rpc_server.partdesign_handlers import _doc
from rpc_server.property_mapper import Object, set_object_property
from rpc_server.serialize import _safe, serialize_object
from rpc_server.tools import register
from rpc_server.view_manager import save_active_screenshot

# Reused JSON-Schema fragments -------------------------------------------------

_DOC_NAME = {"type": "string", "description": "Name of the target document."}
_OBJ_NAME = {"type": "string", "description": "Name of the object within the document."}
_PROPERTIES = {
    "type": "object",
    "additionalProperties": True,
    "description": (
        "FreeCAD property name -> value. Vectors accept {x,y,z}; Placement "
        "accepts {Base:{x,y,z}, Rotation:{Axis:{x,y,z}, Angle:deg}}; single "
        "link properties (Base/Tool/Source/etc.) accept the target object's "
        "name as a string; ShapeColor accepts [r,g,b] or [r,g,b,a]."
    ),
}


# Document operations ----------------------------------------------------------

@register(
    "list_documents",
    "List the names of all open FreeCAD documents.",
    {"type": "object", "properties": {}},
)
def list_documents() -> list[str]:
    return list(FreeCAD.listDocuments().keys())


@register(
    "create_document",
    "Create a new, empty FreeCAD document and make it active.",
    {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Name for the new document."}},
        "required": ["name"],
    },
)
def create_document(name: str) -> dict:
    doc = FreeCAD.newDocument(name)
    doc.recompute()
    FreeCAD.Console.PrintMessage(f"Document '{doc.Name}' created via tool registry.\n")
    # doc.Name, never the requested string. FreeCAD sanitises it — "Bearing 608"
    # becomes "Bearing_608", "608 Bearing" becomes "_608_Bearing" — and uniquifies
    # collisions. Handing back what was asked for means every later call names a
    # document that does not exist. Label is what the user sees and keeps spaces.
    return {"document_name": doc.Name, "label": doc.Label}


@register(
    "reload_document",
    "Close and re-open a saved document from disk to pick up external changes "
    "(e.g. edits made by a headless freecadcmd process).",
    {"type": "object", "properties": {"doc_name": _DOC_NAME}, "required": ["doc_name"]},
)
def reload_document(doc_name: str) -> dict:
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
    FreeCAD.closeDocument(doc_name)
    FreeCAD.openDocument(file_path)
    FreeCAD.Console.PrintMessage(f"Document '{doc_name}' reloaded from '{file_path}'.\n")
    return {"document_name": doc_name}


@register(
    "open_document",
    "Open a .FCStd file from disk and make it the active document.",
    {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Absolute path to the .FCStd file."}},
        "required": ["path"],
    },
)
def open_document(path: str) -> dict:
    if not os.path.exists(path):
        raise ValueError(f"File not found: {path!r}")
    doc = FreeCAD.openDocument(path)
    return {
        "document_name": doc.Name,
        "objects": [{"name": o.Name, "label": o.Label, "type": o.TypeId} for o in doc.Objects],
    }


@register(
    "save_document",
    "Save a document. With a path it saves there (save-as); without one it "
    "saves in place, which requires the document to have been saved before.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC_NAME,
            "path": {"type": "string", "description": "Absolute path to save to. Omit to save in place."},
        },
        "required": ["doc_name"],
    },
)
def save_document(doc_name: str, path: str = "") -> dict:
    doc = _doc(doc_name)
    if path:
        doc.saveAs(path)
    elif doc.FileName:
        doc.save()
    else:
        raise ValueError(
            f"Document '{doc_name}' has never been saved — pass a path to save it somewhere."
        )
    return {"saved_to": doc.FileName}


@register(
    "export_file",
    "Export objects to a file. The format follows the extension: .step/.stp, "
    ".iges, .stl, .ifc, .dxf, .svg, .obj. Omit object_names to export the "
    "whole document.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC_NAME,
            "path": {"type": "string", "description": "Absolute export path, including the extension."},
            "object_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Names of the objects to export. Omit for all of them.",
            },
        },
        "required": ["doc_name", "path"],
    },
)
def export_file(doc_name: str, path: str, object_names: list | None = None) -> dict:
    doc = _doc(doc_name)
    if object_names:
        objs = []
        for name in object_names:
            obj = doc.getObject(name) or next((o for o in doc.Objects if o.Label == name), None)
            if obj is None:
                raise ValueError(f"Object '{name}' not found in '{doc_name}'.")
            objs.append(obj)
    else:
        objs = list(doc.Objects)
    if not objs:
        raise ValueError(f"Document '{doc_name}' has nothing to export.")
    FreeCAD.export(objs, path)
    return {"exported_to": path, "object_count": len(objs)}


@register(
    "undo",
    "Undo the last operation(s). Each tool call is one undo step, so this "
    "reverts whole tool calls rather than individual API calls.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC_NAME,
            "steps": {"type": "integer", "description": "How many steps to undo (default 1)."},
        },
        "required": ["doc_name"],
    },
)
def undo(doc_name: str, steps: int = 1) -> dict:
    doc = _doc(doc_name)
    for _ in range(max(1, int(steps))):
        doc.undo()
    doc.recompute()
    return {"steps_undone": steps}


@register(
    "read_file",
    "Read a UTF-8 text file (up to 64 KB) from disk — dimension specs, survey "
    "notes, anything the user points at rather than types out.",
    {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Absolute path to the file."}},
        "required": ["path"],
    },
)
def read_file(path: str) -> dict:
    max_bytes = 64 * 1024
    if not os.path.exists(path):
        raise ValueError(f"File not found: {path!r}")
    with open(path, encoding="utf-8", errors="replace") as fh:
        content = fh.read(max_bytes)
    return {"content": content, "truncated": len(content) == max_bytes}


# Read operations --------------------------------------------------------------

@register(
    "get_objects",
    "Get all objects in a document, each fully serialised (properties, shape "
    "summary, placement, view attributes). Returns [] for an unknown document.",
    {"type": "object", "properties": {"doc_name": _DOC_NAME}, "required": ["doc_name"]},
)
def get_objects(doc_name: str) -> list:
    try:
        doc = FreeCAD.getDocument(doc_name)
    except Exception:
        return []
    return [serialize_object(obj) for obj in doc.Objects]


@register(
    "get_object",
    "Get one object, fully serialised. Returns null if the document or object "
    "does not exist.",
    {
        "type": "object",
        "properties": {"doc_name": _DOC_NAME, "obj_name": _OBJ_NAME},
        "required": ["doc_name", "obj_name"],
    },
)
def get_object(doc_name: str, obj_name: str):
    try:
        doc = FreeCAD.getDocument(doc_name)
    except Exception:
        return None
    obj = doc.getObject(obj_name)
    return serialize_object(obj) if obj else None


def _link_targets(value) -> list[tuple]:
    """Normalise any ``App::PropertyLink*`` value into ``[(target_name, [subs])]``.

    The four flavours arrive with different shapes: ``obj`` (Link), ``[obj, ...]``
    (LinkList), ``(obj, [subs])`` (LinkSub) and ``[(obj, (subs,)), ...]``
    (LinkSubList). The only ambiguous case is a 2-sequence — ``(obj, [subs])``
    versus a two-element LinkList — told apart by whether the second element is
    itself an object.
    """
    if value is None:
        return []
    if hasattr(value, "Name"):
        return [(value.Name, [])]
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and hasattr(value[0], "Name") and not hasattr(value[1], "Name"):
            return [(value[0].Name, [s for s in (value[1] or []) if s])]
        targets = []
        for item in value:
            targets.extend(_link_targets(item))
        return targets
    return []


@register(
    "get_dependency_graph",
    "Structural map of a document: what every object links to, via which "
    "property, plus its recompute state. This is FreeCAD's dependency graph as "
    "data — far cheaper than get_objects and the right way to check how a model "
    "is actually wired. A sketch with no AttachmentSupport edge is floating on "
    "its Placement rather than attached to geometry; 'Cyclic' marks objects "
    "caught in a dependency loop (typically a SubShapeBinder pointing back at a "
    "body that consumes it); 'Status' appears only when an object is not valid. "
    "Returns [] for an unknown document.",
    {"type": "object", "properties": {"doc_name": _DOC_NAME}, "required": ["doc_name"]},
)
def get_dependency_graph(doc_name: str) -> list:
    try:
        doc = FreeCAD.getDocument(doc_name)
    except Exception:
        return []

    graph = []
    for obj in doc.Objects:
        depends_on = []
        for prop in obj.PropertiesList:
            try:
                type_id = obj.getTypeIdOfProperty(prop)
                # Hidden link properties are FreeCAD's own back-references
                # (Pad._Body, Sketch.Exports) — plumbing, not design intent.
                if "PropertyLink" not in type_id or "Hidden" in type_id:
                    continue
                targets = _link_targets(getattr(obj, prop))
            except Exception:
                continue
            for name, subs in targets:
                edge = {"Property": prop, "Object": name}
                if subs:
                    edge["Sub"] = subs
                depends_on.append(edge)

        entry = {
            "Name": obj.Name,
            "Label": obj.Label,
            "TypeId": obj.TypeId,
            "State": [str(s) for s in obj.State],
            "DependsOn": depends_on,
        }
        try:
            obj.OutListRecursive
        except Exception:
            # OutListRecursive is the cheapest cycle probe FreeCAD exposes: it
            # raises rather than returning on a loop.
            entry["Cyclic"] = True
        status = _safe(obj.getStatusString)
        if status != "Valid":
            entry["Status"] = status
        graph.append(entry)

    return graph


# Mutation operations ----------------------------------------------------------

@register(
    "create_object",
    "Create an object in a document. obj_type is a FreeCAD type id such as "
    "'Part::Box', 'Part::Cylinder', 'Draft::Circle', or a 'Fem::*' type. For "
    "'Fem::*' objects, pass analysis_name to add it to that analysis container.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC_NAME,
            "obj_type": {"type": "string", "description": "FreeCAD type id, e.g. 'Part::Box'."},
            "obj_name": {"type": "string", "description": "Name for the new object."},
            "properties": _PROPERTIES,
            "analysis_name": {
                "type": "string",
                "description": "For Fem::* objects: the analysis container to add the object to.",
            },
        },
        "required": ["doc_name", "obj_type", "obj_name"],
    },
)
def create_object(
    doc_name: str,
    obj_type: str,
    obj_name: str,
    properties: dict | None = None,
    analysis_name: str | None = None,
) -> dict:
    obj = Object(
        name=obj_name,
        type=obj_type,
        analysis=analysis_name,
        properties=properties or {},
    )
    # What actually appeared, not what was asked for. addObject uniquifies:
    # asking twice for "Race" gives "Race" and then "Race001", and reporting
    # "Race" both times sends every later call to the first object — which does
    # not fail, it silently edits the wrong one. Diffing around the call is the
    # only reliable way, since create_object_gui does not hand the object back.
    doc = _doc(doc_name)
    before = {o.Name for o in doc.Objects}
    create_object_gui(doc_name, obj)  # returns True or raises
    added = [o for o in doc.Objects if o.Name not in before]
    made = next((o for o in added if o.Name == obj_name), None) or (
        added[-1] if added else doc.getObject(obj_name))
    if made is None:
        return {"object_name": obj_name}
    return {"object_name": made.Name, "label": made.Label,
            **({"renamed_from": obj_name} if made.Name != obj_name else {})}


@register(
    "edit_object",
    "Change properties on an existing object, then recompute.",
    {
        "type": "object",
        "properties": {"doc_name": _DOC_NAME, "obj_name": _OBJ_NAME, "properties": _PROPERTIES},
        "required": ["doc_name", "obj_name", "properties"],
    },
)
def edit_object(doc_name: str, obj_name: str, properties: dict) -> dict:
    try:
        doc = FreeCAD.getDocument(doc_name)
    except Exception:
        raise ValueError(f"Document '{doc_name}' not found.")
    obj = doc.getObject(obj_name)
    if not obj:
        raise ValueError(f"Object '{obj_name}' not found in document '{doc_name}'.")
    set_object_property(doc, obj, properties)  # raises on failure
    doc.recompute()
    FreeCAD.Console.PrintMessage(f"Object '{obj.Name}' updated via tool registry.\n")
    return {"object_name": obj.Name, "label": obj.Label}


@register(
    "delete_object",
    "Delete an object from a document, then recompute.",
    {
        "type": "object",
        "properties": {"doc_name": _DOC_NAME, "obj_name": _OBJ_NAME},
        "required": ["doc_name", "obj_name"],
    },
)
def delete_object(doc_name: str, obj_name: str) -> dict:
    try:
        doc = FreeCAD.getDocument(doc_name)
    except Exception:
        raise ValueError(f"Document '{doc_name}' not found.")
    doc.removeObject(obj_name)  # raises if the object does not exist
    doc.recompute()
    FreeCAD.Console.PrintMessage(f"Object '{obj_name}' deleted via tool registry.\n")
    return {"object_name": obj_name}


# Code execution ---------------------------------------------------------------

def _namespace_hint(message: str) -> str:
    """Point a failed name at where it actually lives in the exec namespace.

    execute_code runs with FreeCAD, App, Gui, Part, Sketcher and every
    build_helpers shortcut already bound. Every one of those has been reached
    for the wrong way in practice — imported when it is a global, or looked up
    on a module when it is a plain function — and the interpreter's message says
    only that the name is absent, which is the least useful true thing.
    """
    import difflib
    import re as _re

    available = sorted(exec_namespace())
    # Only the MISSING name is worth talking about, and it is the last one quoted:
    # "module 'FreeCAD' has no attribute 'addObject'" is about addObject. Looking
    # at any other quoted name produced the actively misleading "'FreeCAD' is
    # already in scope" — true, unhelpful, and not what failed.
    quoted = _re.findall(r"'([A-Za-z_][A-Za-z0-9_]*)'", message)
    if not quoted:
        return ""
    missing = quoted[-1]
    if missing in available:
        return (f" '{missing}' is already in scope as a global — use it directly, "
                "do not import it or look it up on a module.")
    near = difflib.get_close_matches(missing, available, n=3, cutoff=0.75)
    if near:
        return f" Did you mean {', '.join(near)}? Those are in scope already."
    return ""


@register(
    "execute_code",
    "Run Python in FreeCAD on the GUI thread; returns stdout. Prefer the typed "
    "tools when they fit; use execute_code_async for long CPU-bound geometry.\n"
    "Already global, do NOT import: App/FreeCAD, Gui/FreeCADGui, Part, Sketcher, "
    "post_to_gui, and these PartDesign shortcuts — readyPart(doc, name) (reuses "
    "an existing body; recreate=True wipes it), createSketch(body, name), "
    "createPad(body, sketch, height), createPocket(body, sketch, depth), "
    "createPolar(body, feature, count, angle=360), originPlane(body), "
    "addCircle(sketch, radius, x=0, y=0), addPolygon(sketch, points), "
    "constrainPoint, fcVec, toPolar, toCart, mirrorPointsX, clamp, "
    "vectorDistance, makeBspline, makeBsplineWire.\n"
    "Sketches should end up fully constrained. Use addCircle(sketch, radius, "
    "x=0, y=0) and addPolygon(sketch, points) — they add the geometry AND its "
    "constraints for you. Do not hand-write Sketcher.Constraint calls: the "
    "point indices and argument forms are easy to get wrong.\n"
    "For repeated features (bolt holes, spokes, teeth) make ONE feature and "
    "pattern it: createPolar(body, pocket, 6). Do not build six sketches and "
    "six pockets — the user can then change the count in one place. `feature` "
    "is the Pad/Pocket, not its sketch.\n"
    "Never assign Pad/Pocket .Midplane — deprecated, and assigning it at all "
    "(even False, even its current value) logs a warning. Use .SideType, one "
    "of 'One side' (the default), 'Two sides', 'Symmetric'.\n"
    "There is no 'doc' variable — get one first. Example:\n"
    "  doc = App.getDocument('Flange')\n"
    "  b = readyPart(doc, 'Body'); sk = createSketch(b, 'Base')\n"
    "  addCircle(sk, 20); createPad(b, sk, 6)          # disc\n"
    "  bore = createSketch(b, 'Bore'); addCircle(bore, 5)\n"
    "  createPocket(b, bore, 6); doc.recompute()       # centre bore",
    {
        "type": "object",
        "properties": {"code": {"type": "string", "description": "Python source to execute."}},
        "required": ["code"],
    },
)
def execute_code(code: str) -> dict:
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            exec(code, exec_namespace())  # raises on error -> propagated
    except TypeError as exc:
        # Confusing a property type for an object type is the single most common
        # way hand-written FreeCAD code fails, and the message says what is wrong
        # without saying what to do. The caller reads this string and nothing else.
        if "not a document object type" in str(exc):
            raise TypeError(
                f"{exc}. addObject takes an OBJECT type — 'App::VarSet', "
                "'PartDesign::Body', 'Spreadsheet::Sheet'. 'App::Property*' are "
                "PROPERTY types and belong in obj.addProperty(type, name, group, "
                "doc). Call lookup_freecad_op for the operation you are attempting."
            ) from None
        raise
    except (ImportError, NameError) as exc:
        # The namespace is pre-populated, and the failure is nearly always the
        # caller reaching for something that is already there under another
        # access path: `import Gui` when Gui is a global, or a bare name that is
        # one letter off a helper. Say where the thing actually is.
        hint = _namespace_hint(str(exc))
        if hint:
            raise type(exc)(f"{exc}.{hint}") from None
        raise
    except AttributeError as exc:
        # A FreeCAD object has only the properties it was given. Assigning to one
        # that was never created is the second-commonest way hand-written code
        # fails here, and it is what a VarSet invites: addObject makes an empty
        # one, so vs.bore = 8 raises until addProperty has made 'bore' exist.
        text = str(exc)
        # "module 'Sketcher' has no attribute 'addCircle'" — addCircle is one of
        # ours, injected as a global, and the caller guessed it hangs off the
        # module it is used with.
        hint = _namespace_hint(text)
        if hint and "has no attribute" in text:
            raise AttributeError(f"{exc}.{hint}") from None
        if "object has no attribute" in text and "App." in text:
            raise AttributeError(
                f"{exc}. A FreeCAD object only has the properties it was given. "
                "Create it first: obj.addProperty('App::PropertyLength', 'bore', "
                "'Group', 'description'), then assign obj.bore. An App::VarSet "
                "starts empty. Call lookup_freecad_op('varset') for the sequence."
            ) from None
        raise
    return {"output": buf.getvalue()}


@register(
    "execute_code_async",
    "Run Python on a background thread and return immediately, so a long "
    "CPU-bound computation (e.g. a heavy OCCT fuse/cut/loft) does not freeze "
    "FreeCAD. Document mutation is NOT safe off the GUI thread: do the heavy "
    "math here, then hand the document-writing part to post_to_gui (in scope). "
    "Report progress via FreeCAD.Console and poll a document object (get_object) "
    "for completion.",
    {
        "type": "object",
        "properties": {"code": {"type": "string", "description": "Background-safe Python source."}},
        "required": ["code"],
    },
)
def execute_code_async(code: str) -> dict:
    def worker() -> None:
        try:
            exec(code, exec_namespace())
            FreeCAD.Console.PrintMessage("Async code execution completed.\n")
        except Exception as e:
            import traceback as _tb

            FreeCAD.Console.PrintError(f"Async code error: {e}\n{_tb.format_exc()}")

    threading.Thread(target=worker, daemon=True).start()
    return {"message": "Background execution started."}


# FEM --------------------------------------------------------------------------

@register(
    "run_fem_analysis",
    "Run the CalculiX solver on an existing Fem::FemAnalysis container and return "
    "a summary (node count, von Mises min/max, max displacement, working dir). "
    "Returns a result dict whose 'success' flag reports solver outcome.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC_NAME,
            "analysis_name": {"type": "string", "description": "Name of the Fem analysis container."},
        },
        "required": ["doc_name", "analysis_name"],
    },
)
def run_fem_analysis(doc_name: str, analysis_name: str) -> dict:
    return _fem_run(doc_name, analysis_name)


# Parts library ----------------------------------------------------------------

@register(
    "get_parts_list",
    "List the relative paths of every .FCStd part available in the installed "
    "parts_library addon. Returns [] if the library is not installed.",
    {"type": "object", "properties": {}},
)
def get_parts_list() -> list:
    return _lib_parts_list()


@register(
    "insert_part_from_library",
    "Insert a part from the parts_library addon into the active document "
    "(creating one if none is open). relative_path comes from get_parts_list.",
    {
        "type": "object",
        "properties": {
            "relative_path": {
                "type": "string",
                "description": "Path of the part relative to the parts library root.",
            }
        },
        "required": ["relative_path"],
    },
)
def insert_part_from_library(relative_path: str) -> dict:
    _lib_insert_part(relative_path)  # raises on failure
    return {"message": f"Inserted '{relative_path}' from the parts library."}


# View -------------------------------------------------------------------------

@register(
    "get_active_screenshot",
    "Capture the active 3D view as a PNG. Returns {mime_type, data} where data is "
    "base64-encoded. Optionally frame a named object. Raises if the active view "
    "cannot be captured (e.g. a TechDraw or Spreadsheet view).",
    {
        "type": "object",
        "properties": {
            "view_name": {
                "type": "string",
                "enum": ["Isometric", "Front", "Top", "Right", "Back", "Left",
                         "Bottom", "Dimetric", "Trimetric"],
                "description": "Standard view orientation to capture from.",
            },
            "width": {"type": "integer", "description": "Image width in px (defaults to view size)."},
            "height": {"type": "integer", "description": "Image height in px (defaults to view size)."},
            "focus_object": {"type": "string", "description": "Name of an object to frame on."},
        },
    },
)
def get_active_screenshot(
    view_name: str = "Isometric",
    width: int | None = None,
    height: int | None = None,
    focus_object: str | None = None,
) -> dict:
    fd, tmp_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        save_active_screenshot(tmp_path, view_name, width, height, focus_object)  # raises on failure
        with open(tmp_path, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return {"mime_type": "image/png", "data": data}


# Sketch text labels -----------------------------------------------------------

_DEFAULT_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


def _pick_font(font: str | None) -> str:
    if font:
        if not os.path.isfile(font):
            raise ValueError(f"font file not found: {font}")
        return font
    for f in _DEFAULT_FONTS:
        if os.path.isfile(f):
            return f
    for root, _dirs, files in os.walk("/usr/share/fonts"):
        for fname in sorted(files):
            if fname.lower().endswith((".ttf", ".otf")):
                return os.path.join(root, fname)
    raise ValueError("no usable font found; pass an explicit font path")


def _v(d, what):
    if isinstance(d, dict):
        return FreeCAD.Vector(float(d.get("x", 0)), float(d.get("y", 0)), float(d.get("z", 0)))
    if isinstance(d, (list, tuple)) and len(d) == 3:
        return FreeCAD.Vector(*[float(c) for c in d])
    raise ValueError(f"{what} must be {{x,y,z}} or [x,y,z]")


def _text_sketch(doc, name, text, font, height):
    """A sketch holding one Sketcher Text constraint (handle line length = cap height)."""
    import Part
    import Sketcher

    sk = doc.addObject("Sketcher::SketchObject", name)
    sk.addGeometry(
        Part.LineSegment(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, height, 0)), True
    )
    sk.addConstraint(
        Sketcher.Constraint("Text", [sk.GeometryCount - 1, 0], text, font, True)
    )
    sk.setTextAndFont(len(sk.Constraints) - 1, text, font, True, False)
    doc.recompute()
    return sk


@register(
    "create_text_label",
    "Engrave or emboss text on a PartDesign Body as a real Sketcher Text feature "
    "plus a Pocket/Pad, so the label stays editable in the tree. The text is "
    "centred on 'position', lying in the plane whose outward normal is 'normal', "
    "with its vertical axis along 'up'. If 'max_width' is given the cap height "
    "shrinks to fit (never below 'min_height'). Engraving only removes material, "
    "so it cannot create interference with neighbouring parts.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC_NAME,
            "body_name": {
                "type": "string",
                "description": "Name of the target PartDesign::Body.",
            },
            "text": {"type": "string", "description": "The text to place."},
            "position": {
                "type": "object",
                "description": "Point on the face where the text is centred, {x,y,z}.",
            },
            "normal": {
                "type": "object",
                "description": "Outward face normal, {x,y,z}. The label is cut along -normal.",
            },
            "up": {
                "type": "object",
                "description": "In-plane direction of the text's vertical axis, {x,y,z}.",
            },
            "font": {
                "type": "string",
                "description": "Absolute path to a .ttf/.otf. Defaults to a system sans-bold.",
            },
            "height": {"type": "number", "description": "Cap height in mm (default 2.5)."},
            "depth": {"type": "number", "description": "Engrave/emboss depth in mm (default 0.5)."},
            "max_width": {
                "type": "number",
                "description": "If set, shrink height until the text is no wider than this.",
            },
            "min_height": {
                "type": "number",
                "description": "Floor for the auto-shrink (default 1.8 mm).",
            },
            "mode": {
                "type": "string",
                "enum": ["engrave", "emboss"],
                "description": "engrave = Pocket (default), emboss = Pad.",
            },
        },
        "required": ["doc_name", "body_name", "text", "position", "normal", "up"],
    },
)
def create_text_label(
    doc_name: str,
    body_name: str,
    text: str,
    position,
    normal,
    up,
    font: str | None = None,
    height: float = 2.5,
    depth: float = 0.5,
    max_width: float | None = None,
    min_height: float = 1.8,
    mode: str = "engrave",
) -> dict:
    try:
        doc = FreeCAD.getDocument(doc_name)
    except Exception:
        raise ValueError(f"Document '{doc_name}' not found.")
    body = doc.getObject(body_name)
    if body is None:
        raise ValueError(f"Object '{body_name}' not found in '{doc_name}'.")
    if body.TypeId != "PartDesign::Body":
        raise ValueError(f"'{body_name}' is a {body.TypeId}, not a PartDesign::Body.")
    if mode not in ("engrave", "emboss"):
        raise ValueError("mode must be 'engrave' or 'emboss'")

    font = _pick_font(font)
    pos, n, up_v = _v(position, "position"), _v(normal, "normal"), _v(up, "up")
    if n.Length < 1e-9 or up_v.Length < 1e-9:
        raise ValueError("normal and up must be non-zero")
    n.normalize()
    up_v = up_v.sub(n * up_v.dot(n))          # Vector.multiply is in place; this is not
    if up_v.Length < 1e-9:
        raise ValueError("up must not be parallel to normal")
    up_v.normalize()
    u = up_v.cross(n)                          # u x up = n, right-handed

    # auto-fit: text width scales linearly with cap height
    if max_width:
        probe = _text_sketch(doc, "_lblfit", text, font, height)
        w = probe.Shape.BoundBox.XLength
        doc.removeObject(probe.Name)
        doc.recompute()
        if w > max_width:
            height = max(float(min_height), height * float(max_width) / w)

    sk = _text_sketch(doc, "lbl_" + body_name, text, font, height)
    bb = sk.Shape.BoundBox
    base = (
        pos
        + u * (-bb.XLength / 2.0 - bb.XMin)
        + up_v * (-bb.YLength / 2.0 - bb.YMin)
    )
    sk.Placement = FreeCAD.Placement(
        FreeCAD.Matrix(
            u.x, up_v.x, n.x, base.x,
            u.y, up_v.y, n.y, base.y,
            u.z, up_v.z, n.z, base.z,
            0, 0, 0, 1,
        )
    )
    body.addObject(sk)

    typ = "PartDesign::Pocket" if mode == "engrave" else "PartDesign::Pad"
    feat = doc.addObject(typ, ("engrave_" if mode == "engrave" else "emboss_") + body_name)
    feat.Profile = sk
    feat.Length = float(depth)
    body.addObject(feat)
    doc.recompute()

    return {
        "body": body_name,
        "text": text,
        "sketch": sk.Name,
        "feature": feat.Name,
        "cap_height_mm": round(float(height), 3),
        "text_width_mm": round(float(bb.XLength), 3),
        "font": font,
        "valid": bool(feat.isValid()),
        "solids": len(body.Shape.Solids),
        "volume_mm3": round(float(body.Shape.Volume), 3),
    }


def demo() -> None:
    """Self-check for the dependency graph. Needs FreeCAD.

    Run: ``freecadcmd -c 'from rpc_server.tool_handlers import demo; demo()'``
    """

    class _Obj:
        def __init__(self, name):
            self.Name = name

    a, b = _Obj("A"), _Obj("B")
    assert _link_targets(None) == []
    assert _link_targets(a) == [("A", [])]                      # Link
    assert _link_targets([a, b]) == [("A", []), ("B", [])]      # LinkList, 2 elements
    assert _link_targets((a, ["Edge1"])) == [("A", ["Edge1"])]  # LinkSub
    assert _link_targets((a, ("",))) == [("A", [])]             # LinkSub, whole object
    assert _link_targets([(a, ("Face3",)), (b, ("",))]) == [("A", ["Face3"]), ("B", [])]

    doc = FreeCAD.newDocument("_depgraph_demo")
    try:
        body = doc.addObject("PartDesign::Body", "Body")
        sketch = doc.addObject("Sketcher::SketchObject", "Sketch")
        body.addObject(sketch)
        sketch.AttachmentSupport = [(doc.getObject("XY_Plane"), "")]
        sketch.MapMode = "FlatFace"
        pad = doc.addObject("PartDesign::Pad", "Pad")
        pad.Profile = sketch
        body.addObject(pad)
        doc.recompute()

        graph = {e["Name"]: e for e in get_dependency_graph("_depgraph_demo")}
        edges = lambda name: {(e["Property"], e["Object"]) for e in graph[name]["DependsOn"]}

        # The attachment is what makes a sketch non-floating — the whole point.
        assert ("AttachmentSupport", "XY_Plane") in edges("Sketch"), edges("Sketch")
        assert ("Profile", "Sketch") in edges("Pad"), edges("Pad")
        # Hidden back-references stay out.
        assert not [e for e in graph["Pad"]["DependsOn"] if e["Property"] == "_Body"]
        assert graph["Pad"]["TypeId"] == "PartDesign::Pad"
        assert "Cyclic" not in graph["Pad"]

        # Detach: the sketch falls back to Placement and the edge disappears.
        sketch.AttachmentSupport = None
        doc.recompute()
        graph = {e["Name"]: e for e in get_dependency_graph("_depgraph_demo")}
        assert not [
            e for e in graph["Sketch"]["DependsOn"] if e["Property"] == "AttachmentSupport"
        ], graph["Sketch"]["DependsOn"]

        assert get_dependency_graph("_no_such_document_") == []
        # Names reported must be the names FreeCAD actually made, or every later
        # call addresses something else — or, worse, addresses the wrong object
        # and succeeds.
        made = create_object(doc.Name, "Part::Box", "Race")
        again = create_object(doc.Name, "Part::Box", "Race")
        assert made["object_name"] == "Race", made
        assert again["object_name"] == "Race001", again
        assert again["renamed_from"] == "Race", again
        assert doc.getObject("Race001") is not None
        assert doc.getObject(made["object_name"]) is not doc.getObject(again["object_name"])
        assert edit_object(doc.Name, "Race001", {"Length": 7})["object_name"] == "Race001"

        spaced = create_document("Demo Doc Name")
        try:
            # A space is not legal in an internal name; the Label keeps it.
            assert spaced["document_name"] == "Demo_Doc_Name", spaced
            assert spaced["label"] == "Demo Doc Name", spaced
            assert spaced["document_name"] in FreeCAD.listDocuments()
        finally:
            FreeCAD.closeDocument(spaced["document_name"])

        # Reaching for something that IS in scope, the wrong way. Every one of
        # these came off a real transcript.
        def fails(code):
            try:
                execute_code(code)
            except Exception as exc:
                return str(exc)
            raise AssertionError(f"expected a failure from {code!r}")

        assert "already in scope" in fails("import Gui"), fails("import Gui")
        msg = fails("Sketcher.addCircle(None, 5)")
        # The name worth naming is the missing attribute, not the module it was
        # looked for on.
        assert "'addCircle' is already in scope" in msg, msg
        msg = fails("createskatch(None, 'x')")
        assert "Did you mean createSketch" in msg, msg
        # A genuine mistake with nothing close by is left alone.
        assert "already in scope" not in fails("zzz_no_such_name()")

        print("tool_handlers demo: all checks passed")
    finally:
        FreeCAD.closeDocument(doc.Name)
