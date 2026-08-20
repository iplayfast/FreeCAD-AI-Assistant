"""Shortcuts for building PartDesign geometry from ``execute_code``.

Adapted from the generic (non-gear) half of GearWorkBench's ``util.py``:

    Copyright 2019-2025, Chris Bruner — License LGPL V2.1
    https://github.com/iplayfast/GearWorkBench

Copied rather than imported, because GearWorkBench may not be installed and a
soft dependency that silently changes what the assistant can do is worse than
either having these or not.

These are injected into the ``execute_code`` namespace rather than registered as
tools. Every registered tool costs schema tokens on every request and competes
for the model's attention; these are composable building blocks used in
sequence, which is exactly the job ``execute_code`` already does:

    body = readyPart(doc, "Bracket")
    sk = createSketch(body, "Base")
    sk.addGeometry(Part.Circle(App.Vector(0, 0, 0), App.Vector(0, 0, 1), 12))
    pad = createPad(body, sk, 10)

What they mostly buy is FreeCAD lore that is easy to get wrong: switching off
``Refine`` so the topological naming engine stops spamming "Cannot find RFI
modified Face", tolerating ``body.addObject`` returning a tuple on some
versions, and attaching a sketch to its own body's XY plane.

Bores are deliberately absent — a bore is just a sketch plus a pocket, which
composes from the primitives here in a few lines. GearWorkBench's square, hex
and keyway bores stay available via ``freecad.GearWorkBench.util`` when that
workbench is installed.
"""

import math
from typing import Any, List, Tuple

import FreeCAD as App
import Part
import Sketcher
from Part import BSplineCurve, Wire

# ---------------------------------------------------------------------------
# Vector and math helpers
# ---------------------------------------------------------------------------


def fcVec(x: List[float]) -> App.Vector:
    """Build an App.Vector from a 2- or 3-element sequence."""
    if len(x) == 2:
        return App.Vector(x[0], x[1], 0)
    return App.Vector(x[0], x[1], x[2])


def toPolar(x: float, y: float) -> Tuple[float, float]:
    """Cartesian -> (radius, angle_in_radians)."""
    return (x ** 2.0 + y ** 2.0) ** 0.5, math.atan2(y, x)


def toCart(r: float, a: float) -> Tuple[float, float]:
    """Polar (radius, angle_in_radians) -> (x, y)."""
    return r * math.cos(a), r * math.sin(a)


def mirrorPointsX(points: List[App.Vector]) -> List[App.Vector]:
    """Mirror points across the Y axis (x -> -x), reversing order."""
    return [App.Vector(-p.x, p.y, p.z) for p in reversed(points)]


def clamp(value: float, min_val: float, max_val: float) -> float:
    """Clamp ``value`` into [min_val, max_val]."""
    return min(max_val, max(value, min_val))


def clamp1(a: float) -> float:
    """Clamp into [-1, 1] — for values about to be passed to asin/acos."""
    return min(1, max(a, -1))


def vectorDistance(v1: App.Vector, v2: App.Vector) -> float:
    """Straight-line distance between two vectors."""
    return math.sqrt((v2.x - v1.x) ** 2 + (v2.y - v1.y) ** 2 + (v2.z - v1.z) ** 2)


# ---------------------------------------------------------------------------
# Curves
# ---------------------------------------------------------------------------


def makeBspline(pts, periodic: bool = False) -> List[Any]:
    """Interpolate each point list in ``pts`` into a BSplineCurve."""
    curves = []
    for group in pts:
        out = BSplineCurve()
        points = list(map(fcVec, group))
        if periodic:
            if len(points) < 3:
                raise ValueError("a periodic BSpline needs at least 3 points")
            out.interpolate(points, PeriodicFlag=True)
        else:
            out.interpolate(points)
        curves.append(out)
    return curves


def makeBsplineWire(pts) -> Wire:
    """Interpolate point lists into BSplines and join them into one Wire."""
    return Wire([curve.toShape() for curve in makeBspline(pts)])


# ---------------------------------------------------------------------------
# PartDesign
# ---------------------------------------------------------------------------


def readyPart(doc, name: str, recreate: bool = False):
    """Return the PartDesign Body called ``name``, creating it if absent.

    GearWorkBench's original always deleted an existing body and rebuilt it,
    which suits a generator regenerating its own output but silently destroys
    a user's work when an assistant reuses a name. Reuse is the default here;
    pass ``recreate=True`` for the original behaviour.
    """
    part = doc.getObject(name)
    if part is not None:
        if part.TypeId != "PartDesign::Body":
            raise ValueError(
                f"'{name}' already exists and is a {part.TypeId}, not a PartDesign::Body"
            )
        if not recreate:
            return part
        if hasattr(part, "removeObjectsFromDocument"):
            part.removeObjectsFromDocument()
        doc.removeObject(name)

    part = doc.addObject("PartDesign::Body", name)
    doc.recompute()          # materialises the Origin and its planes
    return part


def originPlane(body, role: str = "XY_Plane"):
    """The body's own origin plane, or None.

    Looked up through ``body.Origin`` rather than ``doc.getObject("XY_Plane")``:
    in a document with several bodies the planes are named XY_Plane001,
    XY_Plane002, ... and a document-wide lookup silently returns the *first*
    body's plane, attaching new sketches to the wrong body.
    """
    origin = getattr(body, "Origin", None)
    if origin is None:
        return None
    for feature in origin.OriginFeatures:
        if getattr(feature, "Role", None) == role:
            return feature
    return None


def _suffixed(name: str, suffix: str) -> str:
    """``name`` plus ``suffix``, unless it already ends that way.

    The suffix convention is deliberate, but a caller that passes "Sketch"
    because the tool asked for a sketch name got "SketchSketch" — a real object
    with a name nobody meant, which then has to be guessed at for every later
    call. Doubling helps no one.
    """
    return name if name.lower().endswith(suffix.lower()) else name + suffix


def createSketch(body, name: str = ""):
    """Add a sketch to ``body``, attached to that body's XY plane.

    ``name`` gets a "Sketch" suffix, matching the GearWorkBench convention.
    """
    sketch = body.Document.addObject("Sketcher::SketchObject", _suffixed(name, "Sketch"))
    body.addObject(sketch)

    plane = originPlane(body)
    if plane is not None:
        # Renamed in FreeCAD 1.0; try the modern spelling first.
        for attr in ("AttachmentSupport", "Support"):
            if hasattr(sketch, attr):
                try:
                    setattr(sketch, attr, [(plane, "")])
                    sketch.MapMode = "FlatFace"
                    break
                except (AttributeError, TypeError):
                    continue

    sketch.Visibility = False
    return sketch


def constrainPoint(sketch, geo: int, point: int, x: float, y: float) -> None:
    """Pin one point of a sketch geometry to (x, y).

    Uses the cheapest constraint that expresses the position: coincident with
    the origin, on an axis, or a distance from it. Adapted from GearWorkBench's
    _constrainSketchPoint.
    """
    if x == 0 and y == 0:
        sketch.addConstraint(Sketcher.Constraint("Coincident", geo, point, -1, 1))
        return
    # Argument order matters and is easy to invert: DistanceX(a, b, value) is
    # measured FROM a TO b, so the origin must come first. With the point first
    # the constraint reads "origin is x to the right of the point", which pins
    # it at -x — a mirror image that only shows up off-axis.
    if x == 0:
        sketch.addConstraint(Sketcher.Constraint("PointOnObject", geo, point, -2))
    else:
        sketch.addConstraint(Sketcher.Constraint("DistanceX", -1, 1, geo, point, x))
    if y == 0:
        sketch.addConstraint(Sketcher.Constraint("PointOnObject", geo, point, -1))
    else:
        sketch.addConstraint(Sketcher.Constraint("DistanceY", -1, 1, geo, point, y))


def addCircle(sketch, radius: float, x: float = 0.0, y: float = 0.0,
              construction: bool = False) -> int:
    """Add a fully constrained circle; returns its geometry index.

    Position and diameter are constrained, so the circle does not drift when
    anything else in the sketch changes. Writing this by hand means knowing
    that a circle's centre is point index 3 and that Diameter takes the
    diameter rather than the radius — both easy to get wrong, and wrong
    constraints are worse than none.
    """
    geo = sketch.addGeometry(
        Part.Circle(App.Vector(x, y, 0), App.Vector(0, 0, 1), radius), construction
    )
    constrainPoint(sketch, geo, 3, x, y)
    sketch.addConstraint(Sketcher.Constraint("Diameter", geo, radius * 2))
    return geo


def addPolygon(sketch, points, closed: bool = True, constrain: bool = True) -> list:
    """Add a chain of line segments; returns their geometry indices.

    Segments are joined coincident. With ``constrain`` (the default) each
    vertex is also pinned to its coordinates, which fully constrains the chain
    — joining alone leaves the whole outline free to slide.
    """
    pts = [p if hasattr(p, "x") else App.Vector(p[0], p[1], 0) for p in points]
    idx = [
        sketch.addGeometry(Part.LineSegment(pts[i], pts[i + 1]), False)
        for i in range(len(pts) - 1)
    ]
    if closed and len(pts) > 2:
        idx.append(sketch.addGeometry(Part.LineSegment(pts[-1], pts[0]), False))

    for i in range(len(idx) - 1):
        sketch.addConstraint(Sketcher.Constraint("Coincident", idx[i], 2, idx[i + 1], 1))
    if closed and len(idx) > 1:
        sketch.addConstraint(Sketcher.Constraint("Coincident", idx[-1], 2, idx[0], 1))

    if constrain:
        for i, p in enumerate(pts):
            constrainPoint(sketch, idx[i], 1, p.x, p.y)
    return idx


def requireSolvable(sketch) -> None:
    """Refuse to build on a sketch the solver cannot resolve.

    Padding a conflicting sketch produces a feature whose Shape is null, and OCC
    only says so later, from whatever next touches it — by which point the broken
    feature is in the document and has to be cleaned up. Lives here rather than in
    the tool wrapper because execute_code calling createPad directly is the path
    that gets used, and it deserves the same guard.
    """
    sketch.solve()
    conflicting = list(sketch.ConflictingConstraints)
    if conflicting:
        raise ValueError(
            f"Sketch '{sketch.Name}' is over-constrained (DoF {int(sketch.DoF)}); "
            f"constraints {conflicting} conflict, so no solid can be built from it. "
            "Call sketch_info with auto_fix=true to drop the redundant ones, or "
            "remove the constraints added by hand. addCircle/addPolygon already "
            "constrain what they add."
        )


def createPad(body, sketch, height: float, name: str = "", symmetric: bool = False):
    """Pad ``sketch`` by ``height``.

    ``symmetric=True`` grows ``height/2`` each way from the sketch plane. Never
    set ``Midplane`` for this: it is deprecated and assigning it at all — even
    False — logs a deprecation warning. FreeCAD 1.1+ has ``SideType``; older
    builds get the sketch-offset trick GearWorkBench used instead.
    """
    requireSolvable(sketch)
    pad = body.Document.addObject("PartDesign::Pad", _suffixed(name, "Pad"))
    body.addObject(pad)
    pad.Profile = sketch
    pad.Length = height
    if symmetric:
        if hasattr(pad, "SideType"):
            pad.SideType = "Symmetric"
        else:
            # ponytail: mutates the sketch placement; drop when 1.0 is history.
            sketch.Placement.Base.z -= height / 2
    # Refine merges coplanar faces and spams "Cannot find RFI modified Face"
    # from the topological naming engine; the geometry is the same without it.
    pad.Refine = False
    sketch.Visibility = False
    return pad


def createPocket(
    body,
    sketch,
    height: float,
    name: str = "",
    suppressed: bool = False,
    reversed_: bool = True,
    through_all: bool = False,
):
    """Cut a pocket ``height`` deep using ``sketch``.

    ``reversed_`` defaults to True, which cuts *along* the sketch normal — into
    material padded above the sketch plane. FreeCAD's own default is the
    opposite and, for the very common "sketch on XY, pad upward, now bore a
    hole from the same plane" layout, silently removes nothing at all: the
    recompute succeeds and the volume is unchanged. Pass ``reversed_=False``
    for FreeCAD's native direction.

    ``through_all=True`` ignores ``height`` and cuts the full depth.
    """
    requireSolvable(sketch)
    pocket = body.Document.addObject("PartDesign::Pocket", _suffixed(name, "Pocket"))
    body.addObject(pocket)
    pocket.Profile = sketch
    pocket.Length = height
    pocket.Reversed = reversed_
    if through_all:
        pocket.Type = "ThroughAll"
    pocket.Suppressed = suppressed
    pocket.Refine = False
    sketch.Visibility = False
    return pocket


def createPolar(body, feature, count: int, angle: float = 360, sketch=None, name: str = ""):
    """Repeat ``feature`` ``count`` times around a circle. Returns the pattern.

    ``feature`` is a Pad or Pocket — not a sketch. ``count`` includes the
    original, and ``angle`` spans the pattern: 360 for a ring of bolt holes,
    less for an arc.

    The rotation axis comes from the feature's own profile sketch, so there is
    normally nothing else to pass; give ``sketch`` only to rotate about a
    different sketch's axis. Argument order is load-bearing here: ``sketch``
    was once positional between ``feature`` and ``count``, and a model called
    ``createPolar(body, sketch, None, 6)`` — axis and feature swapped — then
    gave up on patterning entirely. Moved after ``angle`` because the obvious
    fourth argument is the angle, and that is what the tool description
    advertises; a signature that disagrees with its own documentation just
    moves the error one argument along.

    The pattern is made the body's tip: unlike the additive/subtractive
    features, ``body.newObject`` does not advance the tip by itself, and a
    pattern that is not the tip computes cleanly, reports "Up-to-date", and has
    no effect whatsoever on the body's shape.
    """
    if sketch is None:
        profile = getattr(feature, "Profile", None)
        if isinstance(profile, (tuple, list)):
            profile = profile[0] if profile else None
        sketch = profile
    if sketch is None:
        raise ValueError(
            "createPolar needs a sketch to rotate about: pass a Pad/Pocket as "
            "`feature` (its profile supplies the axis), or pass sketch=... "
            f"explicitly. Got feature={getattr(feature, 'TypeId', type(feature).__name__)}"
        )
    if not hasattr(sketch, "TypeId"):
        raise ValueError(
            f"createPolar sketch must be a document object, got {sketch!r}. "
            "The signature is createPolar(body, feature, count, angle=360)."
        )

    polar = body.newObject("PartDesign::PolarPattern", _suffixed(name, "Polar"))
    polar.Axis = (sketch, ["N_Axis"])
    polar.Angle = angle
    polar.Occurrences = count
    polar.Originals = [feature]
    polar.Refine = False
    body.Tip = polar
    feature.Visibility = False
    polar.Visibility = True
    return polar


# Injected into the execute_code namespace by exec_env.
HELPERS = {
    "fcVec": fcVec,
    "toPolar": toPolar,
    "toCart": toCart,
    "mirrorPointsX": mirrorPointsX,
    "clamp": clamp,
    "clamp1": clamp1,
    "vectorDistance": vectorDistance,
    "makeBspline": makeBspline,
    "makeBsplineWire": makeBsplineWire,
    "readyPart": readyPart,
    "originPlane": originPlane,
    "createSketch": createSketch,
    "constrainPoint": constrainPoint,
    "addCircle": addCircle,
    "addPolygon": addPolygon,
    "createPad": createPad,
    "createPocket": createPocket,
    "createPolar": createPolar,
}


def demo() -> None:
    """Self-check: build real geometry in a scratch document, then discard it.

    Needs FreeCAD. From the Python console:
      from rpc_server.build_helpers import demo; demo()
    """
    # Pure helpers first — no document needed.
    assert fcVec([1, 2]) == App.Vector(1, 2, 0)
    assert fcVec([1, 2, 3]) == App.Vector(1, 2, 3)
    r, a = toPolar(0, 5)
    assert round(r, 9) == 5 and round(a, 9) == round(math.pi / 2, 9)
    x, y = toCart(*toPolar(3, 4))
    assert round(x, 9) == 3 and round(y, 9) == 4
    assert clamp(11, 0, 10) == 10 and clamp(-1, 0, 10) == 0 and clamp(5, 0, 10) == 5
    assert clamp1(2) == 1 and clamp1(-2) == -1
    assert vectorDistance(App.Vector(0, 0, 0), App.Vector(3, 4, 0)) == 5.0
    mirrored = mirrorPointsX([App.Vector(1, 5, 0), App.Vector(2, 6, 0)])
    assert [(p.x, p.y) for p in mirrored] == [(-2, 6), (-1, 5)], mirrored

    doc = App.newDocument("BuildHelpersDemo")
    try:
        body = readyPart(doc, "Block")
        assert body.TypeId == "PartDesign::Body"
        # Reuse by default: the destructive original was the footgun.
        assert readyPart(doc, "Block") is body, "readyPart must not rebuild by default"

        # Each body attaches to its *own* origin plane, not the first body's.
        second = readyPart(doc, "Other")
        assert originPlane(body) is not originPlane(second), "planes must differ per body"

        sketch = createSketch(body, "Base")
        assert sketch.Name.endswith("Sketch"), sketch.Name
        sketch.addGeometry(Part.Circle(App.Vector(0, 0, 0), App.Vector(0, 0, 1), 10), False)
        pad = createPad(body, sketch, 5)
        doc.recompute()
        assert pad.Refine is False
        expected = math.pi * 10 ** 2 * 5
        assert abs(body.Shape.Volume - expected) < 1.0, (body.Shape.Volume, expected)

        # symmetric straddles the sketch plane; same volume, centred on z=0.
        sym = readyPart(doc, "Sym")
        symSk = createSketch(sym, "Base")
        symSk.addGeometry(Part.Circle(App.Vector(0, 0, 0), App.Vector(0, 0, 1), 10), False)
        createPad(sym, symSk, 5, symmetric=True)
        doc.recompute()
        bb = sym.Shape.BoundBox
        assert abs(sym.Shape.Volume - expected) < 1.0, sym.Shape.Volume
        assert abs(bb.ZMin + 2.5) < 1e-6 and abs(bb.ZMax - 2.5) < 1e-6, (bb.ZMin, bb.ZMax)

        # Pocket a smaller bore straight through. This is the case FreeCAD's
        # native pocket direction gets wrong, so assert material really went.
        bore = createSketch(body, "Bore")
        bore.addGeometry(Part.Circle(App.Vector(0, 0, 0), App.Vector(0, 0, 1), 3), False)
        pocket = createPocket(body, bore, 5)
        doc.recompute()
        drilled = expected - math.pi * 3 ** 2 * 5
        assert abs(body.Shape.Volume - drilled) < 1.0, (body.Shape.Volume, drilled)
        assert len(body.Shape.Solids) == 1, body.Shape.Solids

        # ...and the native direction is the silent no-op we default away from.
        pocket.Reversed = False
        doc.recompute()
        assert abs(body.Shape.Volume - expected) < 1.0, "expected the no-op direction"
        pocket.Reversed = True
        doc.recompute()

        # through_all ignores the given length.
        deep = createSketch(body, "Deep")
        deep.addGeometry(Part.Circle(App.Vector(6, 0, 0), App.Vector(0, 0, 1), 1), False)
        createPocket(body, deep, 0.1, through_all=True)
        doc.recompute()
        assert abs(body.Shape.Volume - (drilled - math.pi * 1 ** 2 * 5)) < 1.0, body.Shape.Volume

        # A polar pattern of a pocket, on its own body.
        stud = readyPart(doc, "Studs")
        base = createSketch(stud, "Disc")
        base.addGeometry(Part.Circle(App.Vector(0, 0, 0), App.Vector(0, 0, 1), 20), False)
        disc = createPad(stud, base, 4)
        doc.recompute()
        holeSk = createSketch(stud, "Hole")
        holeSk.addGeometry(Part.Circle(App.Vector(12, 0, 0), App.Vector(0, 0, 1), 2), False)
        hole = createPocket(stud, holeSk, 4)
        doc.recompute()
        # The axis is derived from the pocket's own profile — no sketch passed.
        polar = createPolar(stud, hole, 6)
        doc.recompute()
        assert polar.Occurrences == 6 and polar.Angle == 360
        assert polar.Axis[0] is holeSk, polar.Axis

        # Passing a sketch where a feature belongs is the mistake that made a
        # model give up on patterning; it must say so, not fail obscurely.
        try:
            createPolar(stud, holeSk, 6)
            raise AssertionError("expected a sketch-as-feature to be rejected")
        except ValueError as e:
            assert "Pad/Pocket" in str(e), e

        # The 4th positional is the angle, matching the documented signature.
        arc = createPolar(stud, hole, 3, 180, name="Arc")
        doc.recompute()
        assert arc.Angle == 180 and arc.Occurrences == 3, (arc.Angle, arc.Occurrences)
        stud.Tip = polar
        doc.recompute()

        # A number where the axis belongs must name the signature, not surface
        # as an OCCT "first element in tuple must be DocumentObject" error.
        try:
            createPolar(stud, hole, 6, 360, sketch=360)
            raise AssertionError("expected a non-object sketch to be rejected")
        except ValueError as e:
            assert "document object" in str(e), e
        # The pattern must be the tip, or it silently does nothing.
        assert stud.Tip is polar, stud.Tip.Name
        polar.Angle = 180   # partial arcs are why `angle` is a parameter
        doc.recompute()
        assert stud.Shape.Volume > 0
        polar.Angle = 360
        doc.recompute()
        one_hole = math.pi * 2 ** 2 * 4
        assert abs(stud.Shape.Volume - (math.pi * 20 ** 2 * 4 - 6 * one_hole)) < 1.0, \
            stud.Shape.Volume

        # recreate=True still wipes, for callers that want the old behaviour.
        rebuilt = readyPart(doc, "Block", recreate=True)
        assert rebuilt is not body and len(rebuilt.Group) == 0, rebuilt.Group

        # Constrained sketch primitives: the point of PartDesign is that the
        # sketch holds its shape, so assert zero degrees of freedom rather than
        # just "it padded".
        cs = readyPart(doc, "Constrained")
        sk2 = createSketch(cs, "Circles")
        addCircle(sk2, 20)                 # centred
        addCircle(sk2, 3, x=12, y=0)       # on the X axis
        addCircle(sk2, 3, x=0, y=12)       # on the Y axis
        addCircle(sk2, 2, x=7, y=7)        # off-axis
        doc.recompute()
        assert sk2.FullyConstrained, f"DoF remaining: {sk2.solve()}"

        # Where the geometry LANDS, not just that it is pinned: inverting the
        # DistanceX/Y argument order still fully constrains the sketch, at the
        # mirror image of the requested point. Counting constraints misses that.
        off_axis = sk2.Geometry[3].Center
        assert (round(off_axis.x, 6), round(off_axis.y, 6)) == (7, 7), off_axis

        sk3 = createSketch(cs, "Poly")
        addPolygon(sk3, [(0, 0), (10, 0), (10, 5), (0, 5)])
        doc.recompute()
        assert sk3.FullyConstrained, "polygon should be fully constrained"
        corners = {(round(v.Point.x, 6), round(v.Point.y, 6)) for v in sk3.Shape.Vertexes}
        assert corners == {(0, 0), (10, 0), (10, 5), (0, 5)}, corners

        # ...and joining alone is not enough, which is why constrain defaults on.
        sk4 = createSketch(cs, "Loose")
        addPolygon(sk4, [(0, 0), (10, 0), (10, 5), (0, 5)], constrain=False)
        doc.recompute()
        assert not sk4.FullyConstrained, "expected a coincident-only chain to be free"

        # Curves.
        wire = makeBsplineWire([[[0, 0], [5, 5], [10, 0]]])
        assert wire.Edges, "expected a wire with edges"
        assert len(makeBspline([[[0, 0], [1, 1], [2, 0]]])) == 1
    finally:
        App.closeDocument(doc.Name)

    print("build_helpers demo: all checks passed")
