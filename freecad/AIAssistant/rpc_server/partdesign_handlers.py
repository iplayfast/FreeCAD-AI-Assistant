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

from .tools import register

_DOC = {"type": "string", "description": "Name of the target document."}
_BODY = {"type": "string", "description": "Name of the PartDesign::Body."}
_SKETCH = {"type": "string", "description": "Name of the sketch."}
_PLANE = {
    "type": "string",
    "enum": ["XY_Plane", "XZ_Plane", "YZ_Plane"],
    "description": "Which of the body's own origin planes to attach to.",
}


def _helpers() -> dict[str, Any]:
    from .build_helpers import HELPERS
    return HELPERS


def _doc(doc_name: str):
    try:
        return FreeCAD.getDocument(doc_name)
    except Exception:
        raise ValueError(f"Document '{doc_name}' not found.")


def _inventory(doc, limit: int = 30) -> str:
    """What is actually in ``doc``: Name, plus Label where the two differ."""
    have = [o.Name if o.Label == o.Name else f"{o.Name} (Label '{o.Label}')"
            for o in doc.Objects]
    if len(have) > limit:
        have = have[:limit] + [f"... and {len(have) - limit} more"]
    return ", ".join(have) or "no objects"


def _obj(doc, name: str, kind: str | None = None):
    o = doc.getObject(name)
    if o is None:
        # FreeCAD uniquifies Name on a collision but leaves Label as asked for,
        # so a second pad is Name='Pad001' with Label='Pad' - and a caller that
        # chose that name is looking for its Label. _sketch in bim_handlers has
        # resolved both ways since it was written, for the same reason.
        matches = doc.getObjectsByLabel(name)
        if len(matches) > 1:
            raise ValueError(
                f"'{name}' is the Label of {len(matches)} objects in '{doc.Name}': "
                f"{', '.join(m.Name for m in matches)}. Pass one of those names."
            )
        if not matches:
            # Naming it is what lets the next call be right. Every builder tool
            # returns the name it created; a smoke test asked for 'Pad' after
            # padding, got only "not found", and had nowhere to go from there.
            raise ValueError(
                f"Object '{name}' not found in '{doc.Name}'. It has: {_inventory(doc)}"
            )
        o = matches[0]
    if kind and o.TypeId != kind:
        raise ValueError(f"'{name}' is a {o.TypeId}, expected {kind}.")
    return o


def _body_of(obj):
    """The PartDesign::Body ``obj`` lives in, or None if it is loose."""
    try:
        owner = obj.getParentGeoFeatureGroup()
    except Exception:
        return None
    return owner if getattr(owner, "TypeId", "") == "PartDesign::Body" else None


def _obj_in_body(doc, name: str, kind: str | None, body):
    """``_obj``, but the object must belong to ``body``.

    pad/pocket/polar_pattern looked their sketch or feature up document-wide.
    Sketch names are unique per document but a model names them per body, so an
    observed run called pad(body_name='OuterRing', sketch_name='Sketch') where
    'Sketch' had been created on InnerRing: it extruded one body's profile into
    another, reported volume 351.858 and failed=false, and the rest of the
    bearing was built on top of that. This is the same trap ``originPlane``
    already avoids by going through body.Origin rather than doc.getObject.

    Loose objects (no parent body) are allowed through — the guard exists to
    catch a name resolving to the WRONG body, not to invent a new failure.
    """
    obj = _obj(doc, name, kind)
    owner = _body_of(obj)
    if owner is not None and owner.Name != body.Name:
        raise ValueError(
            f"'{name}' belongs to body '{owner.Name}', not '{body.Name}'. "
            f"Padding one body's sketch into another silently builds the wrong "
            f"solid. Use a sketch from '{body.Name}' (create_sketch makes one), "
            f"or pass body_name='{owner.Name}'."
        )
    return obj


def _segment_key_from(a, b) -> tuple:
    """An order-independent identity for a line between two points.

    Rounded to a micrometre because a model re-deriving the same corner rarely
    reproduces the same float, and undirected because drawing the same wall
    backwards is still the same wall.
    """
    ends = tuple(sorted(((round(a.x, 3), round(a.y, 3)), (round(b.x, 3), round(b.y, 3)))))
    return ends


def _segment_key(geometry) -> tuple:
    """``_segment_key_from`` for an existing sketch element; () if not a line."""
    start, end = getattr(geometry, "StartPoint", None), getattr(geometry, "EndPoint", None)
    return _segment_key_from(start, end) if start and end else ()


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
    # Refuse an outline this sketch already has. Watched live: a model added the
    # same nine-segment profile three times, was told "ok, geometry_count 27"
    # each time, and never learned that the sketch could no longer pad — three
    # outlines lying on each other are self-intersecting, and pad fails much
    # later with an error about the shape rather than about the duplicate.
    existing = {_segment_key(g) for g in sk.Geometry}
    wanted = {_segment_key_from(pts[i], pts[i + 1]) for i in range(len(pts) - 1)}
    if closed and len(pts) > 2:
        wanted.add(_segment_key_from(pts[-1], pts[0]))
    already = wanted & existing
    if already:
        raise ValueError(
            f"Sketch '{sk.Name}' already contains {len(already)} of these "
            f"{len(wanted)} segments — this outline is already there. Adding it "
            "again would stack duplicate geometry, which cannot be padded. Read "
            "it back with get_sketch_geometry, and pad what is there."
        )
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
    sk = _obj_in_body(doc, sketch_name, "Sketcher::SketchObject", body)
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
            "length": {"type": "number", "description": "Depth, mm. Required unless through_all."},
            "name": {"type": "string"},
            "reversed": {"type": "boolean"},
            "through_all": {"type": "boolean"},
        },
        "required": ["doc_name", "body_name", "sketch_name"],
    },
)
def pocket(doc_name: str, body_name: str, sketch_name: str, length: float = 0.0,
           name: str = "", reversed: bool = True, through_all: bool = False) -> dict:
    doc = _doc(doc_name)
    body = _obj(doc, body_name, "PartDesign::Body")
    sk = _obj_in_body(doc, sketch_name, "Sketcher::SketchObject", body)
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
    feat = _obj_in_body(doc, feature_name, None, body)
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


def demo() -> None:
    """Build a small part through the tools, in a scratch document, then discard it.

    Needs FreeCAD: ``freecadcmd run_checks.py``. These are the tools an agent
    actually drives, and until this existed nothing exercised them as tools —
    only the helpers underneath, which is not where the arguments get checked.
    """
    from .tools import call_tool

    doc_name = call_tool("create_document", {"name": "PartDesignDemo"})["document_name"]
    doc_obj = FreeCAD.getDocument(doc_name)
    # The GUI turns this on for every document it opens; under freecadcmd it
    # defaults to 0, which makes setActiveTransaction record nothing and every
    # doc.undo() a silent no-op - so the undo check below would pass vacuously.
    doc_obj.UndoMode = 1
    try:
        call_tool("create_body", {"doc_name": doc_name, "name": "Plate"})
        call_tool("create_sketch", {"doc_name": doc_name, "body_name": "Plate",
                                    "name": "Profile", "plane": "XY_Plane"})
        outline = [{"x": 0, "y": 0}, {"x": 60, "y": 0}, {"x": 60, "y": 40}, {"x": 0, "y": 40}]
        added = call_tool("sketch_polygon", {"doc_name": doc_name,
                                             "sketch_name": "ProfileSketch", "points": outline})
        assert added["geometry_count"] == 4, added

        # The failure this whole check exists for: the same outline added again
        # used to answer "ok, geometry_count 8", leaving a sketch that can never
        # pad and a model with no way to know.
        for repeat in (outline, list(reversed(outline))):   # winding does not matter
            try:
                call_tool("sketch_polygon", {"doc_name": doc_name,
                                             "sketch_name": "ProfileSketch", "points": repeat})
                raise AssertionError("a duplicate outline must be refused")
            except ValueError as exc:
                assert "already there" in str(exc), exc

        # A genuinely different outline is still fine.
        inner = [{"x": 5, "y": 5}, {"x": 20, "y": 5}, {"x": 20, "y": 20}]
        assert call_tool("sketch_polygon", {"doc_name": doc_name,
                                            "sketch_name": "ProfileSketch",
                                            "points": inner})["geometry_count"] == 7

        padded = call_tool("pad", {"doc_name": doc_name, "body_name": "Plate",
                                   "sketch_name": "ProfileSketch", "length": 5})
        assert not padded["failed"] and padded["solids"] >= 1, padded

        # Undo must say what it cost. A watched run padded successfully, undid
        # four steps to redo a sketch, and threw the finished solid away on a
        # result that said only {"steps_undone": 4}.
        undone = call_tool("undo", {"doc_name": doc_name, "steps": 1})
        assert undone["solids_before"] == 1 and undone["solids"] == 0, undone
        assert "destroyed 1 solid" in undone.get("warning", ""), undone
        padded = call_tool("pad", {"doc_name": doc_name, "body_name": "Plate",
                                   "sketch_name": "ProfileSketch", "length": 5})
        assert padded["solids"] >= 1, padded

        # Parametric means the dimension is DRIVEN, not merely correct once.
        call_tool("create_varset", {"doc_name": doc_name, "variables": {"Thickness": 9}})
        bound = call_tool("bind_expression", {"doc_name": doc_name, "obj_name": padded["feature_name"],
                                              "property_name": "Length",
                                              "expression": "Vars.Thickness"})
        assert bound["value"] == 9, bound
        call_tool("set_variables", {"doc_name": doc_name, "variables": {"Thickness": 12}})
        assert abs(doc_obj.getObject(padded["feature_name"]).Length.Value - 12) < 1e-6, \
            "changing the variable must rebuild the feature"

        # Both halves of what a smoke test hit: a name that only exists as a
        # Label, and a name that exists nowhere.
        feature = doc_obj.getObject(padded["feature_name"])
        feature.Label = "TopPad"
        assert _obj(doc_obj, "TopPad").Name == feature.Name, "a Label must resolve too"
        try:
            _obj(doc_obj, "Nonexistent")
            raise AssertionError("a missing object must raise")
        except ValueError as exc:
            assert feature.Name in str(exc), f"the message must name what IS there: {exc}"

    finally:
        FreeCAD.closeDocument(doc_name)

    print("partdesign_handlers demo: all checks passed", flush=True)


@register(
    "bind_expression",
    "Drive an object's dimension from a VarSet expression, so changing the "
    "variable rebuilds the model. This is what makes a document parametric "
    "rather than merely built to the right size: bind_expression(obj_name='Pad', "
    "property_name='Length', expression='Vars.Width'). On a sketch, property_name "
    "may be the NAME of a dimensional constraint. Use the expression_paths "
    "create_varset returned; do not assemble a path by hand.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC,
            "obj_name": {"type": "string",
                         "description": "Object whose property is being driven, e.g. 'Pad'."},
            "property_name": {"type": "string",
                              "description": "Property or named constraint, e.g. 'Length', 'Radius'."},
            "expression": {"type": "string",
                           "description": "FreeCAD expression, e.g. 'Vars.Width' or 'Vars.Bore / 2'."},
        },
        "required": ["doc_name", "obj_name", "property_name", "expression"],
    },
)
def bind_expression(doc_name: str, obj_name: str, property_name: str, expression: str) -> dict:
    # The helper has existed since the beginning, reachable only from inside
    # execute_code — while the system prompt asked for a PARAMETRIC document.
    # So the one operation that makes a document parametric was the one thing a
    # model had to write Python for, and a watched run spent 16 of 30 calls in
    # execute_code doing exactly that, failing 10 of them.
    #
    # The argument is property_name, not property: every other tool here names
    # its string arguments doc_name/obj_name/sketch_name/body_name, so a caller
    # reaching for the odd one out sent property_name and got rejected.
    doc = _doc(doc_name)
    obj = _obj(doc, obj_name)
    value = _helpers()["bindExpression"](obj, property_name, expression)
    doc.recompute()
    # evalExpression can return a Base.Quantity, which xmlrpc can't marshal:
    # its __getattr__ proxy answers the marshaller's __dict__ probe with a
    # bound method instead of raising, and that trips the encoder.
    if hasattr(value, "Value"):
        value = value.Value
    return {"object_name": obj.Name, "property_name": property_name,
            "expression": expression, "value": value}
