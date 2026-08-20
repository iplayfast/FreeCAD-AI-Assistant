"""PartDesign operations, registered into the tool registry.

``build_helpers`` already holds the implementations, adapted from GearWorkBench.
They were only reachable from inside ``execute_code``, so an assistant had to
read the source to discover them - and, not finding them, would hand-roll worse
versions with raw ``doc.addObject``. These wrappers make them first-class tools.

Deliberate choices, each one a bug paid for:

* Sketches are positioned with ``AttachmentOffset``, never ``Placement``.
  ``createSketch`` attaches with ``MapMode='FlatFace'``, so a Placement written
  by the caller is silently overwritten on the next recompute and the pad then
  extrudes from the wrong origin - which reads as the pad *replacing* the body
  rather than adding to it.
* ``plane`` names one of the BODY's own origin planes. A document-wide lookup of
  "XY_Plane" returns the first body's plane once a second body exists.
* Every builder returns ``valid`` and ``volume``. PartDesign happily stacks a
  feature on a broken base and reports success; the caller needs the state at
  each step, not at the end.
* Nothing here sets ``Midplane`` - deprecated in FreeCAD 1.0+, and setting it
  False forces SideType='One side'. For a symmetric pad, offset the sketch by
  half the length instead.
"""

from typing import Any

import FreeCAD

from rpc_server.tools import register

_DOC = {"type": "string", "description": "Name of the target document."}
_BODY = {"type": "string", "description": "Name of the PartDesign::Body."}
_SKETCH = {"type": "string", "description": "Name of the sketch."}
_PLANE = {
    "type": "string",
    "enum": ["XY_Plane", "XZ_Plane", "YZ_Plane"],
    "description": "Which of the body's own origin planes to attach to.",
}


def _helpers() -> dict[str, Any]:
    from rpc_server.build_helpers import HELPERS
    return HELPERS


def _doc(doc_name: str):
    try:
        return FreeCAD.getDocument(doc_name)
    except Exception:
        raise ValueError(f"Document '{doc_name}' not found.")


def _obj(doc, name: str, kind: str | None = None):
    o = doc.getObject(name)
    if o is None:
        raise ValueError(f"Object '{name}' not found in '{doc.Name}'.")
    if kind and o.TypeId != kind:
        raise ValueError(f"'{name}' is a {o.TypeId}, expected {kind}.")
    return o


def _state(body) -> dict:
    """Validity and volume, so a caller sees a broken feature where it happened.

    An empty body has no shape at all, which is normal rather than a failure -
    reporting it as invalid made every create_body call look like an error.
    """
    if not getattr(body, "Group", None):
        return {"valid": True, "volume": 0.0, "solids": 0, "empty": True}
    try:
        sh = body.Shape
        return {"valid": bool(sh.isValid()), "volume": round(sh.Volume, 3),
                "solids": len(sh.Solids)}
    except Exception as exc:
        return {"valid": False, "volume": None, "solids": 0, "error": str(exc)[:120]}


@register(
    "create_varset",
    "Create the document's App::VarSet (or return the existing one) to store parametric "
    "dimension variables. Pass a dictionary of variables (e.g. {'InnerDia': 8.0, "
    "'OuterDia': 22.0, 'BallCount': 8}) to create and initialize them in one step. "
    "Returns expression_paths — use those verbatim in bind_expression; do not assemble "
    "a path by hand. Omit `name`: one VarSet per document named 'Vars' is the convention, "
    "and a second name means half the model reads variables the other half never sees.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC,
            "name": {"type": "string",
                     "description": "Name for the VarSet. Omit it — the default 'Vars' is "
                                    "what every expression example uses."},
            "variables": {
                "type": "object",
                "description": "Key-value mapping of variable names to numbers/values to create.",
            },
        },
        "required": ["doc_name"],
    },
)
def create_varset(doc_name: str, name: str = "Vars", variables: dict[str, Any] | None = None) -> dict:
    doc = _doc(doc_name)
    h = _helpers()
    vs = h["readyVars"](doc, name)
    paths = {}
    if variables:
        for k, v in variables.items():
            paths[k] = h["addVar"](vs, k, v)
    doc.recompute()
    props = {p: str(getattr(vs, p)) for p in vs.PropertiesList if vs.getGroupOfProperty(p) != "Base"}
    return {"varset_name": vs.Name, "variables": props, "expression_paths": paths}


@register(
    "set_variables",
    "Set or add parametric dimension variables on an existing App::VarSet. Omit "
    "`varset_name` for the document's 'Vars'.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC,
            "varset_name": {"type": "string",
                            "description": "Name of the VarSet object. Defaults to 'Vars'."},
            "variables": {
                "type": "object",
                "description": "Key-value mapping of variable names to numbers/values to set.",
            },
        },
        "required": ["doc_name", "variables"],
    },
)
def set_variables(doc_name: str, varset_name: str = "Vars", variables: dict[str, Any] | None = None) -> dict:
    doc = _doc(doc_name)
    h = _helpers()
    vs = _obj(doc, varset_name, "App::VarSet")
    paths = {}
    for k, v in (variables or {}).items():
        paths[k] = h["addVar"](vs, k, v)
    doc.recompute()
    props = {p: str(getattr(vs, p)) for p in vs.PropertiesList if vs.getGroupOfProperty(p) != "Base"}
    return {"varset_name": vs.Name, "variables": props, "expression_paths": paths}


@register(
    "create_body",
    "Create a PartDesign::Body (or return the existing one of that name).",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC,
            "name": {"type": "string", "description": "Name for the body (e.g. 'InnerRing')."},
            "body_name": {"type": "string", "description": "Alias for name."},
            "recreate": {
                "type": "boolean",
                "description": "Delete and rebuild if it already exists. Default false "
                               "- reuse, so an existing model is not silently destroyed.",
            },
        },
        "required": ["doc_name"],
    },
)
def create_body(doc_name: str, name: str = "", body_name: str = "", recreate: bool = False) -> dict:
    name = name or body_name or "Body"
    doc = _doc(doc_name)
    body = _helpers()["readyPart"](doc, name, recreate=recreate)
    doc.recompute()
    return {"body_name": body.Name, **_state(body)}


@register(
    "create_sketch",
    "Add a sketch to a body, attached to one of that body's own origin planes. "
    "Offset moves it ALONG the plane normal via AttachmentOffset - do not set "
    "Placement on an attached sketch, it is overwritten on recompute.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC,
            "body_name": _BODY,
            "name": {"type": "string", "description": "Base name; 'Sketch' is appended."},
            "plane": _PLANE,
            "offset": {
                "type": "number",
                "description": "Shift along the plane normal, mm. For a symmetric pad "
                               "of length L use -L/2.",
            },
        },
        "required": ["doc_name", "body_name", "name"],
    },
)
def create_sketch(doc_name: str, body_name: str, name: str,
                  plane: str = "XY_Plane", offset: float = 0.0) -> dict:
    doc = _doc(doc_name)
    body = _obj(doc, body_name, "PartDesign::Body")
    h = _helpers()
    sk = h["createSketch"](body, name)
    pl = h["originPlane"](body, plane)
    if pl is None:
        raise ValueError(f"Body '{body_name}' has no origin plane '{plane}'.")
    sk.AttachmentSupport = [(pl, "")]
    sk.MapMode = "FlatFace"
    if offset:
        sk.AttachmentOffset = FreeCAD.Placement(
            FreeCAD.Vector(0, 0, offset), FreeCAD.Rotation())
    doc.recompute()
    g = sk.getGlobalPlacement().Base
    return {"sketch_name": sk.Name, "plane": plane, "offset": offset,
            "global_origin": {"x": round(g.x, 4), "y": round(g.y, 4), "z": round(g.z, 4)}}


@register(
    "sketch_circle",
    "Add a circle to a sketch, centred at (x, y) in sketch coordinates. "
    "Tip: You can add multiple concentric circles (e.g. inner bore + outer perimeter) "
    "to the SAME sketch to pad a hollow ring, tube, bushing, or bearing ring in a single step without needing a pocket.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC, "sketch_name": _SKETCH,
            "radius": {"type": "number"},
            "x": {"type": "number"}, "y": {"type": "number"},
            "construction": {"type": "boolean"},
        },
        "required": ["doc_name", "sketch_name", "radius"],
    },
)
def sketch_circle(doc_name: str, sketch_name: str, radius: float,
                  x: float = 0.0, y: float = 0.0, construction: bool = False) -> dict:
    doc = _doc(doc_name)
    sk = _obj(doc, sketch_name, "Sketcher::SketchObject")
    idx = _helpers()["addCircle"](sk, radius, x, y, construction=construction)
    doc.recompute()
    return {"sketch_name": sk.Name, "geometry_index": idx,
            "geometry_count": len(sk.Geometry)}


@register(
    "sketch_polygon",
    "Add a closed (or open) polygon to a sketch from a list of {x, y} points.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC, "sketch_name": _SKETCH,
            "points": {
                "type": "array",
                "items": {"type": "object",
                          "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                          "required": ["x", "y"]},
                "description": "Vertices in order, sketch coordinates.",
            },
            "closed": {"type": "boolean"},
            "constrain": {"type": "boolean",
                          "description": "Add coincident constraints between segments."},
        },
        "required": ["doc_name", "sketch_name", "points"],
    },
)
def sketch_polygon(doc_name: str, sketch_name: str, points: list,
                   closed: bool = True, constrain: bool = True) -> dict:
    doc = _doc(doc_name)
    sk = _obj(doc, sketch_name, "Sketcher::SketchObject")
    pts = [FreeCAD.Vector(float(p["x"]), float(p["y"]), 0.0) for p in points]
    if len(pts) < 2:
        raise ValueError("sketch_polygon needs at least two points.")
    idx = _helpers()["addPolygon"](sk, pts, closed=closed, constrain=constrain)
    doc.recompute()
    return {"sketch_name": sk.Name, "geometry_indices": list(idx),
            "geometry_count": len(sk.Geometry)}


@register(
    "pad",
    "Pad a sketch into its body. If the sketch contains concentric circles (e.g. inner and outer), "
    "FreeCAD automatically pads the annular ring (hollow cylinder/tube/bearing ring) directly.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC, "body_name": _BODY, "sketch_name": _SKETCH,
            "length": {"type": "number", "description": "Pad length, mm."},
            "name": {"type": "string", "description": "Base name; 'Pad' is appended."},
            "reversed": {"type": "boolean",
                         "description": "Extrude against the sketch normal."},
            "symmetric": {"type": "boolean",
                          "description": "Grow length/2 each way from the sketch plane."},
        },
        "required": ["doc_name", "body_name", "sketch_name", "length"],
    },
)
def pad(doc_name: str, body_name: str, sketch_name: str, length: float,
        name: str = "", reversed: bool = False, symmetric: bool = False) -> dict:
    doc = _doc(doc_name)
    body = _obj(doc, body_name, "PartDesign::Body")
    sk = _obj(doc, sketch_name, "Sketcher::SketchObject")
    f = _helpers()["createPad"](body, sk, length, name, symmetric)
    if reversed:
        f.Reversed = True
    doc.recompute()
    state = _state(body)
    # "Invalid" in State catches a feature FreeCAD marked failed, but a feature
    # can recompute "clean" and still leave the body with no usable shape. A
    # result saying failed=false next to valid=false is worse than either alone.
    failed = "Invalid" in f.State or not state.get("valid", True)
    return {"feature_name": f.Name, "failed": failed, **state}


@register(
    "pocket",
    "Cut a pocket into a body from a sketch. `reversed` defaults to true, which "
    "cuts ALONG the sketch normal into material padded above the sketch plane; "
    "FreeCAD's native direction often removes nothing at all and still succeeds.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC, "body_name": _BODY, "sketch_name": _SKETCH,
            "length": {"type": "number", "description": "Depth, mm. Ignored if through_all."},
            "name": {"type": "string"},
            "reversed": {"type": "boolean"},
            "through_all": {"type": "boolean"},
        },
        "required": ["doc_name", "body_name", "sketch_name", "length"],
    },
)
def pocket(doc_name: str, body_name: str, sketch_name: str, length: float,
           name: str = "", reversed: bool = True, through_all: bool = False) -> dict:
    doc = _doc(doc_name)
    body = _obj(doc, body_name, "PartDesign::Body")
    sk = _obj(doc, sketch_name, "Sketcher::SketchObject")
    before = _state(body).get("volume")
    f = _helpers()["createPocket"](body, sk, length, name,
                                   reversed_=reversed, through_all=through_all)
    doc.recompute()
    st = _state(body)
    removed = None if before is None or st["volume"] is None else round(before - st["volume"], 3)
    failed = "Invalid" in f.State or not st.get("valid", True)
    return {"feature_name": f.Name, "failed": failed, "removed": removed, **st}


@register(
    "polar_pattern",
    "Repeat a pad, pocket, sphere, or cylinder around a circle. `count` includes the original.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC, "body_name": _BODY,
            "feature_name": {"type": "string", "description": "The Pad, Pocket, Sphere or Cylinder to repeat."},
            "count": {"type": "integer"},
            "angle": {"type": "number", "description": "Span in degrees; 360 for a full ring."},
            "name": {"type": "string"},
        },
        "required": ["doc_name", "body_name", "feature_name", "count"],
    },
)
def polar_pattern(doc_name: str, body_name: str, feature_name: str, count: int,
                  angle: float = 360.0, name: str = "") -> dict:
    doc = _doc(doc_name)
    body = _obj(doc, body_name, "PartDesign::Body")
    feat = _obj(doc, feature_name)
    f = _helpers()["createPolar"](body, feat, count, angle, name=name)
    doc.recompute()
    return {"feature_name": f.Name, "failed": "Invalid" in f.State, **_state(body)}


@register(
    "create_sphere",
    "Create a solid sphere (e.g. for bearing balls) in a body or document.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC,
            "body_name": {"type": "string", "description": "PartDesign::Body name."},
            "radius": {"type": "number", "description": "Radius of the sphere, mm."},
            "name": {"type": "string", "description": "Base name (default 'Sphere')."},
            "x": {"type": "number", "description": "X position offset (e.g. PitchDia/2 for bearing ball)."},
            "y": {"type": "number", "description": "Y position offset."},
            "z": {"type": "number", "description": "Z position offset."},
        },
        "required": ["doc_name", "body_name", "radius"],
    },
)
def create_sphere(doc_name: str, body_name: str, radius: float, name: str = "Ball",
                  x: float = 0.0, y: float = 0.0, z: float = 0.0) -> dict:
    doc = _doc(doc_name)
    body = _obj(doc, body_name, "PartDesign::Body")
    f = _helpers()["createSphere"](body, radius, name=name, x=x, y=y, z=z)
    doc.recompute()
    return {"feature_name": f.Name, "failed": "Invalid" in f.State, **_state(body)}


@register(
    "create_cylinder",
    "Create a solid cylinder (e.g. for pins, rollers, shafts) in a body or document.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC,
            "body_name": {"type": "string", "description": "PartDesign::Body name."},
            "radius": {"type": "number", "description": "Radius of the cylinder, mm."},
            "height": {"type": "number", "description": "Height / length of the cylinder, mm."},
            "name": {"type": "string", "description": "Base name (default 'Cylinder')."},
            "x": {"type": "number", "description": "X position offset."},
            "y": {"type": "number", "description": "Y position offset."},
            "z": {"type": "number", "description": "Z position offset."},
        },
        "required": ["doc_name", "body_name", "radius", "height"],
    },
)
def create_cylinder(doc_name: str, body_name: str, radius: float, height: float, name: str = "Cylinder",
                    x: float = 0.0, y: float = 0.0, z: float = 0.0) -> dict:
    doc = _doc(doc_name)
    body = _obj(doc, body_name, "PartDesign::Body")
    f = _helpers()["createCylinder"](body, radius, height, name=name, x=x, y=y, z=z)
    doc.recompute()
    return {"feature_name": f.Name, "failed": "Invalid" in f.State, **_state(body)}
