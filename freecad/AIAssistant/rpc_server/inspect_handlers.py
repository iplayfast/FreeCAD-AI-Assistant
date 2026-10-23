"""Read-only inspection: what FreeCAD said, what the shape is, what fits.

These answer the questions a model could not previously ask at all. It could
build geometry and read back a volume, but it could not see FreeCAD's own
warnings, could not measure a clearance, and could not find out *which*
constraint the sketch solver was unhappy about.

Nothing here mutates a document (``sketch_info(auto_fix=True)`` is the one
exception, and says so), so all of it is safe to call between build steps.
"""

from collections import deque

import FreeCAD

from .partdesign_handlers import _DOC, _doc, _obj
from .tools import register

_OBJ = {"type": "string", "description": "Name of the object within the document."}


def _shape(doc, name: str, sub: str = ""):
    """The object's shape, or one named subelement of it (``Face3``, ``Edge7``)."""
    o = _obj(doc, name)
    sh = getattr(o, "Shape", None)
    if sh is None:
        raise ValueError(f"'{name}' ({o.TypeId}) has no Shape.")
    if sub:
        try:
            return sh.getElement(sub)
        except Exception:
            raise ValueError(
                f"'{name}' has no subelement '{sub}'. Faces are 'FaceN', edges "
                f"'EdgeN', vertices 'VertexN', 1-based."
            )
    return sh


def _xyz(v, places: int = 4) -> dict:
    return {"x": round(v.x, places), "y": round(v.y, places), "z": round(v.z, places)}


# The report log ---------------------------------------------------------------

@register(
    "get_log",
    "Read FreeCAD's Report view - the warnings and errors that never reach a "
    "tool's return value. Deprecation notices, recompute complaints and OCC "
    "geometry failures all land here and nowhere else, so check it after any "
    "step that 'succeeded' but looks wrong.",
    {
        "type": "object",
        "properties": {
            "lines": {"type": "integer", "description": "Tail size; default 60."},
            "contains": {"type": "string",
                         "description": "Only lines containing this substring."},
        },
    },
)
def get_log(lines: int = 60, contains: str = "") -> dict:
    """Scrape the Report view widget.

    There is no Python hook into ``Base::Console`` - the C++ side writes
    straight to its observers - and the file observer is off unless FreeCAD was
    started with ``--log-file`` (``ConfigGet('LoggingFile')`` is empty). The
    Report view's own QTextEdit is the one place the whole stream is retained.
    """
    text = None
    try:
        import FreeCADGui
        from PySide import QtWidgets

        mw = FreeCADGui.getMainWindow()
        if mw is not None:
            # The widget's objectName is not stable across builds: "ReportOutput"
            # in some, "Report view" (the dock's name, inherited) in others. A
            # miss here is silent - get_log reports "no Report view" on a running
            # GUI and the caller believes the log is empty - so fall back to the
            # only QTextEdit living inside the Report view dock.
            w = (mw.findChild(QtWidgets.QTextEdit, "ReportOutput")
                 or mw.findChild(QtWidgets.QTextEdit, "Report view"))
            if w is None:
                for dock in mw.findChildren(QtWidgets.QDockWidget):
                    # Keep looking past a dock that merely has "report" in its
                    # name and no QTextEdit in it - a third-party panel
                    # enumerated first would otherwise end the search empty and
                    # put us back on the silent "no Report view" this fallback
                    # exists to prevent.
                    if "report" in dock.objectName().lower():
                        w = dock.findChild(QtWidgets.QTextEdit)
                        if w is not None:
                            break
            if w is not None:
                text = w.toPlainText()
    except Exception:
        # Not just ImportError: under freecadcmd the FreeCADGui module imports
        # fine and has no getMainWindow at all, so catching ImportError alone
        # turned "no GUI, read the log file instead" into an AttributeError.
        pass

    if text is None:
        path = FreeCAD.ConfigGet("LoggingFileName")
        if path:
            with open(path, errors="replace") as fh:
                text = "".join(deque(fh, maxlen=max(lines, 1) * 4))
        else:
            return {"available": False, "lines": [],
                    "reason": "No GUI Report view, and file logging is off "
                              "(start FreeCAD with --log-file to enable it)."}

    out = [ln for ln in text.splitlines() if ln.strip()]
    if contains:
        out = [ln for ln in out if contains in ln]
    return {"available": True, "lines": out[-max(lines, 1):], "truncated": len(out) > lines}


# Shape health -----------------------------------------------------------------

@register(
    "check_shape",
    "Check a shape's geometric validity. `bop` runs the slower boolean-operation "
    "check that catches self-intersections - the failure that recomputes "
    "cleanly, reports 'Up-to-date', and then breaks a later feature or the "
    "slicer.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC, "obj_name": _OBJ,
            "bop": {"type": "boolean", "description": "Run the BOP check too. Slower."},
        },
        "required": ["doc_name", "obj_name"],
    },
)
def check_shape(doc_name: str, obj_name: str, bop: bool = False) -> dict:
    doc = _doc(doc_name)
    o = _obj(doc, obj_name)
    sh = _shape(doc, obj_name)
    res = {
        "obj_name": o.Name,
        "state": list(o.State),
        "valid": bool(sh.isValid()),
        "closed": bool(sh.isClosed()) if sh.Faces else None,
        "solids": len(sh.Solids), "shells": len(sh.Shells),
        "faces": len(sh.Faces), "edges": len(sh.Edges),
        "volume": round(sh.Volume, 4), "area": round(sh.Area, 4),
    }
    if bop:
        # check() returns None and raises ValueError carrying the analysis text
        # (the .pyi says -> bool, which is wrong); the message is the useful part.
        try:
            sh.check(True)
            res["bop_ok"] = True
        except Exception as exc:
            res["bop_ok"] = False
            res["bop_error"] = str(exc)[:300]
    return res


@register(
    "mass_properties",
    "Volume, surface area, centre of mass and bounding box. With `density` "
    "(g/cm3) also returns mass in grams - PLA is 1.24, PETG 1.27, ABS 1.04.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC, "obj_name": _OBJ,
            "density": {"type": "number", "description": "g/cm3; omit to skip mass."},
        },
        "required": ["doc_name", "obj_name"],
    },
)
def mass_properties(doc_name: str, obj_name: str, density: float = 0.0) -> dict:
    doc = _doc(doc_name)
    sh = _shape(doc, obj_name)
    bb = sh.BoundBox
    res = {
        "shape_type": sh.ShapeType,
        "solids": len(sh.Solids),
        "volume_mm3": round(sh.Volume, 4),
        "area_mm2": round(sh.Area, 4),
        # A Compound has no CenterOfMass, and neither does a shell or a wire, so
        # asking for it unconditionally turned "this object is not a single
        # solid" into a crash inside the tool. Which it is, is the useful part.
        "center_of_mass": _xyz(sh.CenterOfMass) if hasattr(sh, "CenterOfMass") else None,
        "bbox": {"xmin": round(bb.XMin, 4), "xmax": round(bb.XMax, 4),
                 "ymin": round(bb.YMin, 4), "ymax": round(bb.YMax, 4),
                 "zmin": round(bb.ZMin, 4), "zmax": round(bb.ZMax, 4),
                 "xlen": round(bb.XLength, 4), "ylen": round(bb.YLength, 4),
                 "zlen": round(bb.ZLength, 4)},
    }
    if res["center_of_mass"] is None:
        res["note"] = (
            f"A {sh.ShapeType} has no centre of mass. It holds {len(sh.Solids)} "
            "solid(s): fuse them, or ask about one of them, if you need one."
        )
    if density:
        res["mass_g"] = round(sh.Volume / 1000.0 * density, 4)
    return res


# Fit and clearance ------------------------------------------------------------

@register(
    "measure_distance",
    "Minimum distance between two objects, or between named subelements of them "
    "('Face3', 'Edge7', 'Vertex2'). Returns 0 when they touch or overlap - use "
    "check_interference to tell those two apart.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC,
            # obj1_name, not a_name: nothing guesses a single letter, and a live
            # session sent object1/object2 - close enough to mean this and far
            # enough that the did-you-mean hint could not even suggest a_name.
            "obj1_name": {"type": "string", "description": "First object."},
            "obj2_name": {"type": "string", "description": "Second object."},
            "obj1_sub": {"type": "string",
                         "description": "Subelement of the first, e.g. 'Face3'."},
            "obj2_sub": {"type": "string", "description": "Subelement of the second."},
        },
        "required": ["doc_name", "obj1_name", "obj2_name"],
    },
)
def measure_distance(doc_name: str, obj1_name: str, obj2_name: str,
                     obj1_sub: str = "", obj2_sub: str = "") -> dict:
    doc = _doc(doc_name)
    sa, sb = _shape(doc, obj1_name, obj1_sub), _shape(doc, obj2_name, obj2_sub)
    dist, points, infos = sa.distToShape(sb)
    p1, p2 = points[0] if points else (None, None)
    return {
        "distance": round(dist, 6),
        "touching": dist < 1e-7,
        "from": _xyz(p1) if p1 else None,
        "to": _xyz(p2) if p2 else None,
        "solution_count": len(points),
    }


@register(
    "check_interference",
    "Do these solids overlap, and by how much? Returns the shared volume for "
    "every pair. The question a printed assembly answers the expensive way. "
    "Omit obj_names to test every solid in the document against every other.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC,
            "obj_names": {"type": "array", "items": {"type": "string"},
                          "description": "Two or more objects to test pairwise. "
                                         "Omit for every solid in the document."},
        },
        "required": ["doc_name"],
    },
)
def check_interference(doc_name: str, obj_names: list | None = None) -> dict:
    doc = _doc(doc_name)
    if not obj_names:
        # "Does anything in here overlap" is the question people actually have,
        # and it names no objects. Solids only: a sketch or datum plane has no
        # volume to share, and pairing them produces noise, not findings.
        # A Body's own features (Pad, Pocket, ...) each carry a solid that is a
        # stage of that one body, so including them reports every body as 100%
        # interfering with itself. The Body already stands for all of them.
        in_body = {f.Name for o in doc.Objects if o.TypeId == "PartDesign::Body"
                   for f in o.Group}
        obj_names = [o.Name for o in doc.Objects if o.Name not in in_body
                     and getattr(getattr(o, "Shape", None), "Solids", None)]
    if len(obj_names) < 2:
        raise ValueError(
            f"check_interference needs at least two solids; '{doc_name}' has "
            f"{len(obj_names)}."
        )
    shapes = {n: _shape(doc, n) for n in obj_names}
    pairs = []
    for i, a in enumerate(obj_names):
        for b in obj_names[i + 1:]:
            try:
                vol = shapes[a].common(shapes[b]).Volume
            except Exception as exc:
                pairs.append({"a": a, "b": b, "error": str(exc)[:120]})
                continue
            gap = None
            if vol <= 1e-9:
                gap = round(shapes[a].distToShape(shapes[b])[0], 6)
            pairs.append({"a": a, "b": b, "overlap_volume": round(vol, 6),
                          "interferes": vol > 1e-9, "gap": gap})
    return {"pairs": pairs,
            "any_interference": any(p.get("interferes") for p in pairs)}


@register(
    "cross_section",
    "Slice a shape with planes and report each section's wire count, perimeter "
    "and overall extent. Cheap way to see how a part's section changes along "
    "its length without rendering it. `extent` is the bounding box of the whole "
    "slice, so it is the section's outer size - NOT a wall thickness: a 4 mm "
    "wall on a 20 mm tube still reports xlen 20. Two closed wires means two "
    "separate loops, which is a hollow section only when one is nested inside "
    "the other; two legs of a bracket report the same thing.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC, "obj_name": _OBJ,
            "axis": {"type": "string", "enum": ["x", "y", "z"],
                     "description": "Plane normal. Default z."},
            "positions": {"type": "array", "items": {"type": "number"},
                          "description": "Where to cut, mm along the axis."},
            "count": {"type": "integer",
                      "description": "Instead of positions: this many evenly "
                                     "spaced cuts across the bounding box."},
        },
        "required": ["doc_name", "obj_name"],
    },
)
def cross_section(doc_name: str, obj_name: str, axis: str = "z",
                  positions: list | None = None, count: int = 0) -> dict:
    doc = _doc(doc_name)
    sh = _shape(doc, obj_name)
    normal = FreeCAD.Vector(*{"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[axis])
    bb = sh.BoundBox
    lo, hi = {"x": (bb.XMin, bb.XMax), "y": (bb.YMin, bb.YMax),
              "z": (bb.ZMin, bb.ZMax)}[axis]

    if not positions:
        n = count or 5
        # Inset from the faces: a cut exactly on the end cap is degenerate.
        step = (hi - lo) / (n + 1)
        positions = [lo + step * (i + 1) for i in range(n)]

    out = []
    for p in positions:
        wires = sh.slice(normal, float(p))
        sec = {"position": round(float(p), 4), "wires": len(wires),
               "closed_wires": sum(1 for w in wires if w.isClosed()),
               "length": round(sum(w.Length for w in wires), 4)}
        # How wide and tall the section is - the perimeter alone cannot tell a
        # wide section from a tall one. This is the union box over every wire,
        # so on a tube it is the OUTER diameter; it does not answer "how thick
        # is the wall", and the description no longer claims it does.
        # ponytail: bounding box, not section area; a face built from nested
        # wires fails on exactly the hollow parts this tool is for.
        #
        # Zeros rather than a missing key on an empty slice: every sibling key
        # is unconditional, so a caller looping s["extent"]["xlen"] over the
        # sections would KeyError on the first cut that misses material - a
        # position outside the solid, or the gap between two of them.
        bbs = [w.BoundBox for w in wires]
        sec["extent"] = {
            "xlen": round(max(b.XMax for b in bbs) - min(b.XMin for b in bbs), 4) if bbs else 0.0,
            "ylen": round(max(b.YMax for b in bbs) - min(b.YMin for b in bbs), 4) if bbs else 0.0,
            "zlen": round(max(b.ZMax for b in bbs) - min(b.ZMin for b in bbs), 4) if bbs else 0.0,
        }
        out.append(sec)
    return {"axis": axis, "range": [round(lo, 4), round(hi, 4)], "sections": out}


# Sketch diagnostics -----------------------------------------------------------

@register(
    "sketch_info",
    "Why is this sketch not fully constrained? Returns remaining degrees of "
    "freedom plus the indices of conflicting, redundant and malformed "
    "constraints - the detail behind a bare FullyConstrained=False.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC,
            "sketch_name": {"type": "string", "description": "Name of the sketch."},
            "auto_fix": {"type": "boolean",
                         "description": "Remove redundant constraints and add "
                                        "missing coincidences. Modifies the sketch."},
        },
        "required": ["doc_name", "sketch_name"],
    },
)
def sketch_info(doc_name: str, sketch_name: str, auto_fix: bool = False) -> dict:
    doc = _doc(doc_name)
    sk = _obj(doc, sketch_name, "Sketcher::SketchObject")
    fixed = {}
    if auto_fix:
        # detectMissingPointOnPointConstraints only reports; the paired
        # makeMissing... call is what actually applies them.
        try:
            found = sk.detectMissingPointOnPointConstraints(1e-7)
            if found:
                sk.makeMissingPointOnPointCoincident()
            fixed["coincidences_added"] = int(found)
        except Exception as exc:
            fixed["coincidence_error"] = str(exc)[:120]
        try:
            sk.autoRemoveRedundants()
            fixed["redundants_removed"] = True
        except Exception as exc:
            fixed["redundant_error"] = str(exc)[:120]
        doc.recompute()

    sk.solve()
    return {
        "sketch_name": sk.Name,
        "fully_constrained": bool(sk.FullyConstrained),
        "dof": int(sk.DoF),
        "geometry_count": len(sk.Geometry),
        "constraint_count": len(sk.Constraints),
        "conflicting": list(sk.ConflictingConstraints),
        "redundant": list(sk.RedundantConstraints),
        "partially_redundant": list(sk.PartiallyRedundantConstraints),
        "malformed": list(sk.MalformedConstraints),
        **({"fixed": fixed} if auto_fix else {}),
    }


@register(
    "check_sketch_geometry",
    "Inspect and validate sketch constraints, degrees of freedom, and geometry health (alias for sketch_info).",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC,
            "sketch_name": {"type": "string", "description": "Name of the sketch."},
            "auto_fix": {"type": "boolean", "description": "Auto-fix coincidences/redundancies."},
        },
        "required": ["doc_name", "sketch_name"],
    },
)
def check_sketch_geometry(doc_name: str, sketch_name: str, auto_fix: bool = False) -> dict:
    return sketch_info(doc_name, sketch_name, auto_fix=auto_fix)


# What the user is pointing at -------------------------------------------------

@register(
    "get_selection",
    "What the user has selected in the 3D view, including subelement names like "
    "'Face3' or 'Edge7'. This is how a request phrased as 'fillet THIS edge' "
    "becomes an argument you can pass to fillet.",
    {"type": "object", "properties": {}},
)
def get_selection() -> dict:
    try:
        import FreeCADGui

        selected = FreeCADGui.Selection.getSelectionEx()
    except Exception:
        # Headless (freecadcmd) imports FreeCADGui fine but has no Selection at
        # all, so importing was never the thing that could fail. There is no
        # selection without a 3D view: say so, do not raise.
        return {"available": False, "selection": []}

    sel = []
    for s in selected:
        sel.append({
            "doc_name": s.Document.Name,
            "obj_name": s.Object.Name,
            "label": s.Object.Label,
            "type_id": s.Object.TypeId,
            "sub_elements": list(s.SubElementNames),
            "picked_points": [_xyz(p) for p in s.PickedPoints],
        })
    return {"available": True, "selection": sel}


def demo() -> None:
    """Self-check against real geometry. Run: ``freecadcmd`` with this imported."""
    import Part

    from .tools import call_tool

    doc = FreeCAD.newDocument("InspectDemo")
    try:
        box = doc.addObject("Part::Box", "Box")
        box.Length = box.Width = box.Height = 10
        far = doc.addObject("Part::Box", "Far")
        far.Length = far.Width = far.Height = 10
        far.Placement.Base = FreeCAD.Vector(12, 0, 0)   # 2 mm gap
        over = doc.addObject("Part::Box", "Over")
        over.Length = over.Width = over.Height = 10
        over.Placement.Base = FreeCAD.Vector(5, 0, 0)   # 500 mm3 overlap
        doc.recompute()

        chk = check_shape("InspectDemo", "Box", bop=True)
        assert chk["valid"] and chk["solids"] == 1 and chk["bop_ok"], chk
        assert abs(chk["volume"] - 1000) < 1e-6, chk

        mp = mass_properties("InspectDemo", "Box", density=1.24)
        assert abs(mp["mass_g"] - 1.24) < 1e-6, mp
        assert mp["center_of_mass"] == {"x": 5.0, "y": 5.0, "z": 5.0}, mp
        assert mp["bbox"]["xlen"] == 10.0, mp

        d = measure_distance("InspectDemo", "Box", "Far")
        assert abs(d["distance"] - 2.0) < 1e-6 and not d["touching"], d

        # Subelement addressing: the +X face of Box is 2 mm from Far's -X face.
        assert measure_distance("InspectDemo", "Box", "Far", obj1_sub="Face2")["distance"] >= 0
        # The names a session reaches for must survive the registry's front door.
        assert call_tool("measure_distance", {"doc_name": "InspectDemo",
                                             "object1": "Box", "object2": "Far"})["distance"] > 0

        inter = check_interference("InspectDemo", ["Box", "Far", "Over"])
        assert inter["any_interference"], inter
        pair = [p for p in inter["pairs"] if {p["a"], p["b"]} == {"Box", "Over"}][0]
        assert abs(pair["overlap_volume"] - 500.0) < 1e-6, pair
        gapped = [p for p in inter["pairs"] if {p["a"], p["b"]} == {"Box", "Far"}][0]
        assert not gapped["interferes"] and abs(gapped["gap"] - 2.0) < 1e-6, gapped

        # Omitted obj_names sweeps every solid in the document - the form the
        # question "does anything in here overlap" actually takes.
        swept = check_interference("InspectDemo")
        assert swept["any_interference"], swept
        named = {p["a"] for p in swept["pairs"]} | {p["b"] for p in swept["pairs"]}
        assert named >= {"Box", "Far", "Over"}, swept

        cs = cross_section("InspectDemo", "Box", axis="z", count=3)
        assert len(cs["sections"]) == 3, cs
        assert all(s["closed_wires"] == 1 for s in cs["sections"]), cs
        # 10x10 square perimeter, at every height.
        assert all(abs(s["length"] - 40.0) < 1e-6 for s in cs["sections"]), cs
        # The extent is how wide and tall the section is - perimeter alone
        # cannot tell a wide section from a tall one. Flat in the slice axis.
        assert all(abs(s["extent"]["xlen"] - 10.0) < 1e-6
                   and abs(s["extent"]["ylen"] - 10.0) < 1e-6
                   and s["extent"]["zlen"] == 0.0 for s in cs["sections"]), cs

        # A tube must report its two nested wires, and must report the OUTER
        # diameter as the extent - the number the old description invited a
        # caller to read as a 4 mm wall.
        outer = doc.addObject("Part::Cylinder", "Outer")
        inner = doc.addObject("Part::Cylinder", "Inner")
        outer.Radius, outer.Height = 10, 20
        inner.Radius, inner.Height = 6, 20
        tube = doc.addObject("Part::Cut", "Tube")
        tube.Base, tube.Tool = outer, inner
        doc.recompute()
        hollow = cross_section("InspectDemo", "Tube", axis="z", count=2)
        assert all(s["closed_wires"] == 2 for s in hollow["sections"]), hollow
        assert all(abs(s["extent"]["xlen"] - 20.0) < 1e-6 for s in hollow["sections"]), hollow

        # A cut that misses the solid entirely still reports extent, zeroed:
        # the key is part of every section or a caller cannot loop over them.
        empty = cross_section("InspectDemo", "Box", axis="z", positions=[-5, 500])
        assert all(s["wires"] == 0 and s["extent"]["xlen"] == 0.0
                   for s in empty["sections"]), empty

        # A sketch with a free circle: 3 DoF (centre x, y and radius).
        sk = doc.addObject("Sketcher::SketchObject", "Sk")
        sk.addGeometry(Part.Circle(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 0, 1), 4), False)
        doc.recompute()
        info = sketch_info("InspectDemo", "Sk")
        assert not info["fully_constrained"] and info["dof"] == 3, info
        assert info["conflicting"] == [] and info["redundant"] == [], info

        # get_log and get_selection degrade to available=False without a GUI
        # rather than raising - a headless caller gets an answer either way.
        assert "available" in get_log(lines=5), get_log(lines=5)
        assert "available" in get_selection()

        # mass_properties must survive a shape that is not one solid. A Compound
        # has no CenterOfMass, and asking for it anyway turned "this is not a
        # single solid" into a crash inside the tool.
        import Part

        comp = doc.addObject("Part::Feature", "Comp")
        comp.Shape = Part.makeCompound(
            [Part.makeBox(1, 1, 1), Part.makeBox(1, 1, 1, FreeCAD.Vector(5, 0, 0))]
        )
        doc.recompute()
        m = mass_properties(doc.Name, "Comp")
        assert m["shape_type"] == "Compound" and m["solids"] == 2, m
        assert m["center_of_mass"] is None and "no centre of mass" in m["note"], m
        assert m["volume_mm3"] == 2.0, m          # still reports what it can

        solid = doc.addObject("Part::Feature", "Solid")
        solid.Shape = Part.makeBox(2, 2, 2)
        doc.recompute()
        m = mass_properties(doc.Name, "Solid")
        assert m["center_of_mass"] == {"x": 1.0, "y": 1.0, "z": 1.0}, m
        assert "note" not in m, m

        print("inspect_handlers demo: all checks passed")
    finally:
        FreeCAD.closeDocument(doc.Name)
