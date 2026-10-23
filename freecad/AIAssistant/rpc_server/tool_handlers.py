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

import ast
import base64
import contextlib
import difflib
import io
import json
import os
import re
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET

import FreeCAD

from .exec_env import exec_namespace
from .fem_executor import run_fem_analysis as _fem_run
from .object_factory import create_object_gui
from .parts_library import get_parts_list as _lib_parts_list
from .parts_library import insert_part_from_library as _lib_insert_part
from .partdesign_handlers import _doc
from .property_mapper import Object, set_object_property
from .serialize import _safe, serialize_object
from .tools import register
from .view_manager import save_active_screenshot

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


def _document_solids(doc) -> int:
    """Solids across a document — the one claim a transcript cannot fake."""
    # A PartDesign::Body's Shape mirrors its Tip feature's Shape, so counting
    # both double-counts every body. Skip the Tip; the Body already covers it.
    tips = {body.Tip.Name for body in doc.Objects
            if body.TypeId == "PartDesign::Body" and body.Tip}
    total = 0
    for obj in doc.Objects:
        if obj.Name in tips:
            continue
        try:
            total += len(obj.Shape.Solids)
        except Exception:
            pass
    return total


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
    before_objects, before_solids = len(doc.Objects), _document_solids(doc)
    for _ in range(max(1, int(steps))):
        doc.undo()
    doc.recompute()
    after_solids = _document_solids(doc)
    result = {
        "steps_undone": steps,
        "objects": len(doc.Objects), "objects_before": before_objects,
        "solids": after_solids, "solids_before": before_solids,
    }
    # Watched live: a run padded a bracket successfully, then asked to undo four
    # steps to redo one sketch, and threw the finished solid away. The call
    # answered {"steps_undone": 4} — true, and no help at all. Undo is a blunt
    # instrument and the model has no other way to learn what it just cost.
    if after_solids < before_solids:
        result["warning"] = (
            f"This undo destroyed {before_solids - after_solids} solid(s): the "
            f"document had {before_solids} and now has {after_solids}. Undo reverts "
            "whole tool calls, including features you meant to keep. To change one "
            "thing, edit or delete that object instead — get_objects shows what is "
            "there."
        )
    return result


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
    # Type before position. A PartDesign::Body brings an Origin and its six
    # origin features along with it, so "added" is eight objects and added[-1]
    # is an origin point. An observed run asked for a Body called InnerRace,
    # got back {"object_name": "Origin013", "label": "Origin-Point005"} because
    # the name was taken and the fallback took the last thing to appear, and
    # every later call then addressed a point instead of the body.
    made = (
        next((o for o in added if o.Name == obj_name and o.TypeId == obj_type), None)
        or next((o for o in added if o.TypeId == obj_type), None)
        or next((o for o in added if o.Name == obj_name), None)
        or (added[-1] if added else doc.getObject(obj_name))
    )
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
    available = sorted(exec_namespace())
    # Only the MISSING name is worth talking about, and it is the last one quoted:
    # "module 'FreeCAD' has no attribute 'addObject'" is about addObject. Looking
    # at any other quoted name produced the actively misleading "'FreeCAD' is
    # already in scope" — true, unhelpful, and not what failed.
    quoted = re.findall(r"'([A-Za-z_][A-Za-z0-9_]*)'", message)
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


def _bind_doc(ns: dict, doc_name: str, activate: bool = False) -> None:
    """Point ``doc`` at ``doc_name`` for this call, or remove it.

    ``exec_namespace()`` is one dict shared by every call and by the async
    worker thread, so a ``doc`` left behind outlives the call that set it: the
    next execute_code without a doc_name would find a ``doc`` naming whatever
    ran last, possibly a document since closed. Clearing it when no doc_name is
    given keeps the tool's own "there is no 'doc' variable" contract true.
    # ponytail: still one shared namespace, so an async call racing a sync one
    # can still swap doc under it. Per-call namespaces if that ever bites.
    """
    if not doc_name:
        ns.pop("doc", None)
        return
    try:
        ns["doc"] = FreeCAD.getDocument(doc_name)
        if activate:
            FreeCAD.setActiveDocument(doc_name)
    except Exception:
        ns.pop("doc", None)
        raise ValueError(
            f"Document '{doc_name}' not found. Open it, or call create_document."
        ) from None


# No regex pre-pass over the source. One was tried, to glue back identifiers a
# tokenizer had split ("PitchDi a" -> "PitchDia"), and a regex cannot tell code
# from a string or an attribute from a keyword: the rule that closed the gap
# after a dotted name also rewrote `sum(f.Length.Value for f in feats)` to
# `sum(f.Length.Valuef or f in feats)`, which still parses and returns the wrong
# number. A comprehension over the document is the commonest thing anyone
# writes here. Let the syntax error reach the caller; the handler below reports
# it against the source they actually sent.


@register(
    "execute_code",
    "Run Python in FreeCAD on the GUI thread; returns stdout. Prefer the typed "
    "tools when they fit; use execute_code_async for long CPU-bound geometry.\n"
    "Already global, do NOT import: App/FreeCAD, Gui/FreeCADGui, Part, Sketcher, "
    "Units, Quantity, post_to_gui, and these shortcuts:\n"
    "  * Parametric variables (VarSet):\n"
    "      vs = readyVars(doc)           # creates or reuses App::VarSet\n"
    "      addVar(vs, 'InnerDia', 8.0)             # creates PropertyLength & sets 8.0 mm\n"
    "      addVar(vs, 'BallCount', 8)              # creates PropertyInteger & sets 8\n"
    "      bindExpression(sk, 'bore', 'Vars.InnerDia')  # drives constraint/pad from var\n"
    "  * PartDesign geometry:\n"
    "      b = readyPart(doc, 'Body')              # creates/reuses body (recreate=False)\n"
    "      sk = createSketch(b, 'Base')            # attaches to body's XY plane\n"
    "      addCircle(sk, 20)                       # fully-constrained circle\n"
    "      createPad(b, sk, 6)                     # pad without deprecation warnings\n"
    "      createPocket(b, sk, 6)                  # pocket cut\n"
    "      createPolar(b, pad_or_pocket, 6)        # polar pattern\n"
    "      originPlane(body), addPolygon(sketch, points), constrainPoint, fcVec\n"
    "Do NOT prefix these with module names (use readyPart, NOT PartDesign.readyPart).\n"
    "Sketches should end up fully constrained. Use addCircle and addPolygon — "
    "they add the geometry AND its constraints for you. Do not hand-write "
    "Sketcher.Constraint calls.\n"
    "Never assign Pad/Pocket .Midplane — deprecated; use .SideType ('One side', 'Two sides', 'Symmetric').\n"
    "There is no 'doc' variable — get one first. Example:\n"
    "  doc = App.getDocument('Flange')\n"
    "  vs = readyVars(doc); addVar(vs, 'Dia', 40); addVar(vs, 'Thick', 6)\n"
    "  b = readyPart(doc, 'Body'); sk = createSketch(b, 'Base')\n"
    "  addCircle(sk, 20); createPad(b, sk, 6); doc.recompute()",
    {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source to execute."},
            "doc_name": {"type": "string", "description": "Optional target document name."},
        },
        "required": ["code"],
    },
)
def execute_code(code: str, doc_name: str = "") -> dict:
    buf = io.StringIO()
    ns = exec_namespace()
    _bind_doc(ns, doc_name, activate=True)
    try:
        ast.parse(code)
    except SyntaxError as exc:
        raise SyntaxError(
            f"Python syntax error at line {exc.lineno}: {exc.msg}. "
            f"Check code syntax: {exc.text}"
        ) from None
    try:
        with contextlib.redirect_stdout(buf):
            exec(code, ns)  # raises on error -> propagated
    except SyntaxError as exc:
        raise SyntaxError(
            f"Python syntax error at line {exc.lineno}: {exc.msg}. "
            f"Check code syntax: {exc.text}"
        ) from None
    except TypeError as exc:
        msg = str(exc)
        if "not a document object type" in msg:
            raise TypeError(
                f"{exc}. addObject takes an OBJECT type — 'App::VarSet', "
                "'PartDesign::Body', 'Spreadsheet::Sheet'. 'App::Property*' are "
                "PROPERTY types and belong in obj.addProperty(type, name, group, "
                "doc). Better: use readyVars(doc) and addVar(vs, name, value)."
            ) from None
        if "must be str" in msg and ("argument 2" in msg or "not float" in msg or "not int" in msg):
            raise TypeError(
                f"{exc}. obj.addProperty takes (type_str, name_str), e.g. "
                "obj.addProperty('App::PropertyLength', 'InnerDia'). "
                "Or use the built-in helper: addVar(varset, 'InnerDia', 8.0)."
            ) from None
        raise
    except (ImportError, NameError) as exc:
        text = str(exc)
        if "OCP" in text or "OpenCASCADE" in text:
            raise ImportError(
                f"{exc}. OCP / OpenCASCADE is not used in FreeCAD Python. "
                "Use PartDesign helpers: b = readyPart(doc, 'Body'); sk = createSketch(b, 'Base'); "
                "addCircle(sk, radius); createPad(b, sk, 6); createPocket(b, sk, 6)."
            ) from None
        if "cannot import name" in text and "from 'Part'" in text:
            raise ImportError(
                f"{exc}. Part boolean functions are not direct imports. "
                "Use PartDesign features: readyPart(doc, 'Name'), createSketch, addCircle, createPad, createPocket."
            ) from None
        hint = _namespace_hint(text)
        if hint:
            raise type(exc)(f"{exc}.{hint}") from None
        raise
    except ValueError as exc:
        msg = str(exc)
        if "FileName" in msg and "is not set" in msg:
            raise ValueError(
                f"{exc}. App.ActiveDocument.save() requires a filename for new documents. "
                "Use doc.saveAs('/path/to/file.FCStd') or the save_document tool."
            ) from None
        raise
    except AttributeError as exc:
        text = str(exc)
        hint = _namespace_hint(text)
        if hint and "has no attribute" in text:
            raise AttributeError(f"{exc}.{hint}") from None
        if "has no attribute 'value'" in text and "Quantity" in text:
            raise AttributeError(
                f"{exc}. FreeCAD Quantity uses uppercase .Value (e.g. q.Value) or float(q)."
            ) from None
        if "module 'FreeCAD' has no attribute 'VarSet'" in text or "module 'App' has no attribute 'VarSet'" in text:
            raise AttributeError(
                f"{exc}. FreeCAD has no App.VarSet Python class. Check obj.TypeId == 'App::VarSet' "
                "or use readyVars(doc)."
            ) from None
        if "has no attribute 'Properties'" in text:
            raise AttributeError(
                f"{exc}. FreeCAD objects have .PropertiesList, not .Properties."
            ) from None
        if "has no attribute 'setVariable'" in text:
            raise AttributeError(
                f"{exc}. FreeCAD documents have no setVariable method. "
                "Use vs = readyVars(doc); addVar(vs, 'Name', value)."
            ) from None
        # Reaching for an OBJECT through the document — doc.VarSet, doc.Body,
        # doc.Object — is the single most common thing a model gets wrong in
        # here (9 of 21 errors across a four-task suite run), and the generic
        # "add the property first" answer below is advice for a different
        # problem entirely: it wants to FIND something, not create a property.
        if "'App.Document' object has no attribute" in text:
            raise AttributeError(
                f"{exc}. A document does not expose its objects as attributes. "
                "Look one up by name — doc.getObject('Vars') — or by type: "
                "next(o for o in doc.Objects if o.TypeId == 'App::VarSet'). "
                "For the VarSet specifically, readyVars(doc) returns it and "
                "creates it if absent."
            ) from None
        if "object has no attribute" in text and "App." in text:
            raise AttributeError(
                f"{exc}. A FreeCAD object only has the properties it was given. "
                "Create it first: obj.addProperty('App::PropertyLength', 'bore', "
                "'Group', 'description'), then assign obj.bore. An App::VarSet "
                "starts empty. Better: use addVar(varset, name, value)."
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
        "properties": {
            "code": {"type": "string", "description": "Background-safe Python source."},
            "doc_name": {"type": "string", "description": "Optional target document name."},
        },
        "required": ["code"],
    },
)
def execute_code_async(code: str, doc_name: str = "") -> dict:
    def worker() -> None:
        try:
            ns = exec_namespace()
            _bind_doc(ns, doc_name)
            exec(code, ns)
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


_PACKAGE_XML_NS = {"pm": "https://wiki.freecad.org/Package_Metadata"}
_SCAN_CACHE_FILENAME = "freecad_ai_workbench_scan.json"


def _scan_cache_path() -> str:
    return os.path.join(FreeCAD.getUserAppDataDir(), _SCAN_CACHE_FILENAME)


def _addon_metadata() -> list[dict]:
    """name/description/path for every installed addon that ships a package.xml."""
    addons = []
    mod_dirs = set()
    for getter in (FreeCAD.getUserAppDataDir, FreeCAD.getHomePath):
        try:
            mod_dirs.add(os.path.join(getter(), "Mod"))
        except Exception:
            pass
    for mod_dir in mod_dirs:
        if not os.path.isdir(mod_dir):
            continue
        for entry in sorted(os.listdir(mod_dir)):
            addon_dir = os.path.join(mod_dir, entry)
            pkg_path = os.path.join(addon_dir, "package.xml")
            if not os.path.isfile(pkg_path):
                continue  # no metadata: a built-in Mod, covered by the workbench sweep instead
            try:
                root = ET.parse(pkg_path).getroot()
                name = (root.findtext("pm:name", namespaces=_PACKAGE_XML_NS) or entry).strip()
                desc = (root.findtext("pm:description", namespaces=_PACKAGE_XML_NS) or "").strip()
            except Exception:
                name, desc = entry, ""
            addons.append({"name": name, "description": desc, "path": addon_dir})
    return addons


_CRASHY_WORKBENCHES = {"FemWorkbench"}


def _scan_workbenches_live() -> list[dict]:
    """Walk every installed workbench and every command it contributes.

    Most third-party addons (freecad.gears, Fasteners, ...) register no
    commands until FreeCADGui.activateWorkbench runs their Initialize() —
    without actually cycling through each one, a lazily-loaded addon is
    invisible here even though it is installed. This is the only generic way
    to force that registration without hand-parsing each addon's own layout.

    A command is attributed to a workbench two ways, unioned: (1) it sits on
    a toolbar that is not shared by every workbench (the File/Edit/View/...
    chrome every workbench carries), found via getToolbarItems() right after
    activating; (2) it is new in FreeCADGui.listCommands() since the previous
    workbench was activated — catches a command that registers lazily but was
    never put on a toolbar. Neither alone is enough: GearWorkBench's commands
    are already registered before this scan starts (so (2) sees nothing new
    for it), while a toolbar-less lazy command needs (2) to be seen at all.
    """
    addons = _addon_metadata()
    try:
        import FreeCADGui as Gui

        original = Gui.activeWorkbench()
    except Exception:
        # Headless (freecadcmd) imports FreeCADGui fine and has no
        # activeWorkbench/listWorkbenches at all, so importing was never the
        # thing that could fail. Nothing to activate: report the addons on disk.
        return [dict(a, internal="", kind="addon", commands=[]) for a in addons]

    original_name = original.name() if original else None

    toolbars_by_wb, module_path_by_wb, lazy_new_by_wb = {}, {}, {}
    seen = set(Gui.listCommands())
    for internal in sorted(Gui.listWorkbenches()):
        if internal in _CRASHY_WORKBENCHES:
            # ponytail: FemWorkbench's VTK post-processing pipeline segfaults
            # on systems with a mismatched system vtk package - that's a
            # native crash, unrecoverable with try/except. Skip outright
            # instead of taking the whole scan down with it.
            continue
        try:
            Gui.activateWorkbench(internal)
            wb = Gui.activeWorkbench()
            module = sys.modules.get(type(wb).__module__)
            module_path_by_wb[internal] = os.path.dirname(getattr(module, "__file__", "") or "")
            try:
                toolbars_by_wb[internal] = wb.getToolbarItems()
            except Exception:
                toolbars_by_wb[internal] = {}
            now = set(Gui.listCommands())
            lazy_new_by_wb[internal] = now - seen
            seen = now
        except Exception:
            # A workbench can fail anywhere in its Initialize() (e.g. a
            # mismatched native dependency like VTK) - skip it like an
            # outright activation failure, not abort the whole sweep.
            continue
    if original_name:
        try:
            Gui.activateWorkbench(original_name)
        except Exception:
            pass

    # Skip workbenches with zero toolbars (NoneWorkbench, or one whose
    # getToolbarItems() failed) - an empty set in this intersection wipes out
    # every "shared" toolbar for everyone else, misattributing Std_*/PieMenu_*
    # chrome to every workbench as if it were one of its own commands.
    nonempty = [set(t) for t in toolbars_by_wb.values() if t]
    shared_toolbars = set.intersection(*nonempty) if nonempty else set()

    result = []
    for internal, wb in sorted(Gui.listWorkbenches().items()):
        own_toolbar_cmds = {
            cmd for name, cmds in toolbars_by_wb.get(internal, {}).items()
            if name not in shared_toolbars for cmd in cmds if cmd != "Separator"
        }
        commands = []
        for cmd_name in sorted(own_toolbar_cmds | lazy_new_by_wb.get(internal, set())):
            try:
                info = Gui.Command.get(cmd_name).getInfo()
                commands.append({
                    "name": cmd_name,
                    "menu": (info.get("menuText") or "").replace("&", ""),
                    "tooltip": info.get("toolTip") or "",
                })
            except Exception:
                commands.append({"name": cmd_name, "menu": "", "tooltip": ""})
        module_path = module_path_by_wb.get(internal, "")
        owner = next((a for a in addons if module_path.startswith(a["path"])), None)
        result.append({
            "name": getattr(wb, "MenuText", "") or internal,
            "internal": internal,
            "kind": "addon" if owner else "built-in",
            "description": owner["description"] if owner else "",
            "path": owner["path"] if owner else "",
            "commands": commands,
        })

    covered = {w["path"] for w in result if w["path"]}
    for addon in addons:
        if addon["path"] not in covered:
            # Installed, but registers no workbench of its own (a pie-menu or
            # dialog-only addon) - still worth listing so it isn't invisible.
            result.append(dict(addon, internal="", kind="addon", commands=[]))
    return result


@register(
    "list_workbenches",
    "List installed FreeCAD workbenches, built-in and third-party addons, each "
    "with the commands it registers (name, menu text, tooltip) - e.g. which "
    "library has a Bevel Gear command, without reading source. Result is "
    "cached to disk after the first call; pass force=true to re-scan after "
    "installing or updating an addon. Call this before hand-building "
    "something in execute_code that a workbench like Gears, SheetMetal or "
    "Fasteners may already do — reuse beats reimplementing.",
    {"type": "object", "properties": {
        "force": {"type": "boolean",
                  "description": "Re-scan every workbench instead of using the cached result."},
    }},
)
def list_workbenches(force: bool = False) -> list[dict]:
    cache_path = _scan_cache_path()
    if not force and os.path.isfile(cache_path):
        try:
            with open(cache_path) as f:
                return json.load(f)
        except Exception:
            pass  # corrupt cache: fall through and rebuild it

    result = _scan_workbenches_live()
    try:
        with open(cache_path, "w") as f:
            json.dump(result, f, indent=2)
    except Exception as e:
        FreeCAD.Console.PrintWarning(f"Failed to cache workbench scan: {e}\n")
    return result


def format_addon_summary(workbenches: list[dict]) -> str:
    """Condense a ``list_workbenches`` result into addons worth mentioning upfront.

    Built-ins are skipped - a model already knows Part/PartDesign/Sketcher from
    training, so the only new information here is what third-party addons this
    particular install has and what they can do. Skips addons with no commands
    too (not yet activated, or dialog-only) - nothing useful to say about them.
    """
    addons = [w for w in workbenches if w.get("kind") == "addon" and w.get("commands")]
    if not addons:
        return ""
    lines = [
        "Installed addons (from the last workbench scan - rerun "
        '"Rescan local workbenches" after installing or updating one):',
    ]
    for wb in sorted(addons, key=lambda w: w["name"]):
        cmds = ", ".join(c["name"] for c in wb["commands"])
        # The path, so driving it via execute_code/read_file never needs a
        # filesystem search - a CLI without this addon's own list_workbenches
        # tool (e.g. a different pre-registered "freecad" MCP server) has no
        # other way to find where an addon lives.
        where = f" ({wb['path']})" if wb.get("path") else ""
        lines.append(f"- {wb['name']}{where}: {cmds}")
    return "\n".join(lines)


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

    Run: ``freecadcmd -c 'from freecad.AIAssistant.rpc_server.tool_handlers import demo; demo()'``
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

    # A forced scan rewrites the on-disk cache, and under freecadcmd it can
    # only see addons on disk - no Gui means no commands. Leaving that behind
    # would strip the GUI's own cached scan down to a command-less list and
    # silently empty the addon summary the panel sends the model.
    saved_cache = None
    if os.path.isfile(_scan_cache_path()):
        with open(_scan_cache_path()) as f:
            saved_cache = f.read()
    try:
        wbs = list_workbenches(force=True)
        assert isinstance(wbs, list) and wbs, "expected at least one workbench entry"
        assert all({"name", "internal", "kind", "description", "path", "commands"} == set(w) for w in wbs)
        addons = [w for w in wbs if w["kind"] == "addon"]
        assert addons, "expected at least one addon with package.xml metadata (this addon has one)"
        assert all(w["description"] for w in addons), "addon package.xml should yield a description"
        assert all(os.path.isdir(w["path"]) for w in addons)
        this_addon = next(w for w in wbs if w["path"] and os.path.basename(w["path"]) == "AI")
        if any(w["commands"] for w in wbs):   # GUI only: headless registers no commands
            assert any(c["name"] == "AILaunchAgentCommand" for c in this_addon["commands"]), this_addon
        cached = list_workbenches()
        assert cached == wbs, "an uncached call right after a forced scan should read back the cache"
    finally:
        if saved_cache is not None:
            with open(_scan_cache_path(), "w") as f:
                f.write(saved_cache)

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

        # Reaching for an object through the document is the commonest mistake
        # in here; the answer is how to look one up, not how to add a property.
        msg = fails("doc = App.newDocument('HintProbe'); doc.VarSet")
        assert "does not expose its objects as attributes" in msg, msg
        assert "doc.getObject(" in msg and "readyVars(doc)" in msg, msg
        FreeCAD.closeDocument("HintProbe")
        msg = fails("Sketcher.addCircle(None, 5)")
        # The name worth naming is the missing attribute, not the module it was
        # looked for on.
        assert "'addCircle' is already in scope" in msg, msg
        msg = fails("createskatch(None, 'x')")
        # The hint may offer several near names ("create_sketch, createSketch");
        # what matters is that the helper actually in scope is among them.
        assert "createSketch" in msg, msg
        # A genuine mistake with nothing close by is left alone.
        assert "already in scope" not in fails("zzz_no_such_name()")

        print("tool_handlers demo: all checks passed")
    finally:
        FreeCAD.closeDocument(doc.Name)
