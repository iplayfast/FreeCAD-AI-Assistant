"""Architectural (Arch/BIM) tools, plus the local FreeCAD documentation lookup.

Ported from the AI workbench's own tool layer, which these were the only truly
distinct part of — everything else it defined already existed in the registry.

The BIM workflow here is deliberately sketch-first: walls are built from a
single fully-constrained Sketcher sketch rather than from loose Draft lines, so
the floorplan stays parametric and the user can drag a dimension afterwards and
have the walls follow. ``sketch_line`` names the constraints it adds after the
wall, which is what lets ``get_sketch_geometry`` report the plan back with wall
labels attached.
"""

import json
import os
import re

import FreeCAD
import Part

from .partdesign_handlers import _DOC, _doc, _obj
from .tools import register

# This file is at <addon-root>/freecad/AIAssistant/rpc_server/bim_handlers.py,
# so the addon root is four directories up.
_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

_SKETCH = {"type": "string", "description": "Name of the sketch."}


def _sketch(doc, name: str):
    """The sketch, found by internal Name or by user-facing Label."""
    obj = doc.getObject(name) or next((o for o in doc.Objects if o.Label == name), None)
    if obj is None:
        raise ValueError(f"Sketch '{name}' not found in '{doc.Name}' (tried Name and Label).")
    if obj.TypeId != "Sketcher::SketchObject":
        raise ValueError(f"'{name}' is a {obj.TypeId}, expected Sketcher::SketchObject.")
    return obj


def _by_label(doc, label: str, what: str):
    obj = next((o for o in doc.Objects if o.Label == label), None) or doc.getObject(label)
    if obj is None:
        raise ValueError(f"{what} '{label}' not found in '{doc.Name}'.")
    return obj


# Sketch geometry --------------------------------------------------------------

def _pin(sketch, geo: int, point: int, x: float, y: float, prefix: str = "") -> None:
    """Pin one sketch point to absolute (x, y), naming the constraints.

    Same constraint choice as ``build_helpers.constrainPoint``, but the names
    matter here: they carry the wall label, which is the only way to get from a
    geometry index back to "this is the south wall" when reading a plan back.
    """
    import Sketcher

    def _add(constraint, suffix):
        n = sketch.ConstraintCount
        sketch.addConstraint(constraint)
        if prefix:
            try:
                sketch.renameConstraint(n, f"{prefix}_{suffix}")
            except Exception:
                pass  # a duplicate name is cosmetic; never fail the geometry for it

    if x == 0 and y == 0:
        _add(Sketcher.Constraint("Coincident", geo, point, -1, 1), "origin")
        return
    if x == 0:
        _add(Sketcher.Constraint("PointOnObject", geo, point, -2), "on_yaxis")
    else:
        _add(Sketcher.Constraint("DistanceX", -1, 1, geo, point, x), "x")
    if y == 0:
        _add(Sketcher.Constraint("PointOnObject", geo, point, -1), "on_xaxis")
    else:
        _add(Sketcher.Constraint("DistanceY", -1, 1, geo, point, y), "y")


@register(
    "sketch_line",
    "Add one fully-constrained line to a sketch, given both endpoints as "
    "absolute coordinates in mm. Both ends are pinned relative to the sketch "
    "origin, so no direction or sign arithmetic is needed: a wall running south "
    "from the origin is just (0,0) to (0,-3000). Pass the wall's name as label "
    "so the plan can be read back with get_sketch_geometry. Lines that share an "
    "endpoint produce redundant (not conflicting) constraints, which the solver "
    "handles.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC,
            "sketch_name": _SKETCH,
            "x1": {"type": "number", "description": "Start X in mm."},
            "y1": {"type": "number", "description": "Start Y in mm."},
            "x2": {"type": "number", "description": "End X in mm."},
            "y2": {"type": "number", "description": "End Y in mm."},
            "label": {"type": "string", "description": "Wall name, e.g. 'SouthWall'."},
            "construction": {"type": "boolean", "description": "Construction geometry (default false)."},
        },
        "required": ["doc_name", "sketch_name", "x1", "y1", "x2", "y2"],
    },
)
def sketch_line(doc_name: str, sketch_name: str, x1: float, y1: float, x2: float,
                y2: float, label: str = "", construction: bool = False) -> dict:
    import Part

    doc = _doc(doc_name)
    sk = _sketch(doc, sketch_name)
    idx = sk.addGeometry(
        Part.LineSegment(FreeCAD.Vector(x1, y1, 0), FreeCAD.Vector(x2, y2, 0)), construction
    )
    _pin(sk, idx, 1, x1, y1, f"{label}_start" if label else "")
    _pin(sk, idx, 2, x2, y2, f"{label}_end" if label else "")
    doc.recompute()
    return {"index": idx, "label": label, "start": [x1, y1], "end": [x2, y2]}


@register(
    "get_sketch_geometry",
    "Read a sketch back: every line and arc with its coordinates and, where "
    "sketch_line set one, its wall label. Use this to see what a floorplan "
    "already contains before adding to it, or to resume an interrupted session, "
    "rather than deleting and starting over.",
    {
        "type": "object",
        "properties": {"doc_name": _DOC, "sketch_name": _SKETCH},
        "required": ["doc_name", "sketch_name"],
    },
)
def get_sketch_geometry(doc_name: str, sketch_name: str) -> dict:
    import Part

    doc = _doc(doc_name)
    sk = _sketch(doc, sketch_name)

    # Recover geometry index -> wall label from the constraint names _pin wrote.
    labels: dict[int, str] = {}
    for c in sk.Constraints:
        name, idx = c.Name or "", c.First
        if idx < 0 or not name:
            continue
        for marker in ("_start_", "_end_"):
            if marker in name:
                labels.setdefault(idx, name[: name.index(marker)])
                break

    segments = []
    for i, geo in enumerate(sk.Geometry):
        entry = {"index": i, "label": labels.get(i, ""),
                 "construction": bool(getattr(geo, "Construction", False))}
        if isinstance(geo, Part.LineSegment):
            entry.update(type="line",
                         start=[round(geo.StartPoint.x, 3), round(geo.StartPoint.y, 3)],
                         end=[round(geo.EndPoint.x, 3), round(geo.EndPoint.y, 3)])
        elif isinstance(geo, Part.ArcOfCircle):
            entry.update(type="arc",
                         center=[round(geo.Center.x, 3), round(geo.Center.y, 3)],
                         radius=round(geo.Radius, 3))
        else:
            entry["type"] = type(geo).__name__
        segments.append(entry)

    return {"sketch_name": sk.Name, "segment_count": len(segments), "segments": segments}


# BIM ---------------------------------------------------------------------------

@register(
    "create_bim_document",
    "Create a document with the standard BIM hierarchy: Site -> Building -> "
    "Floor (a BuildingPart). Walls added to the floor inherit its placement, "
    "which is what makes storeys work.",
    {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Document name."}},
        "required": ["name"],
    },
)
def create_bim_document(name: str) -> dict:
    import Arch

    doc = FreeCAD.newDocument(name)
    site = Arch.makeSite(name="ProjectSite")
    site.Label = "Project Site"
    building = Arch.makeBuilding(name="MainBuilding")
    building.Label = "Building"
    site.addObject(building)
    floor = Arch.makeBuildingPart(name="Floor_1")
    floor.Label = "Floor 1"
    floor.Placement = FreeCAD.Placement(FreeCAD.Vector(0, 0, 0), FreeCAD.Rotation(0, 0, 0))
    building.addObject(floor)
    doc.recompute()
    return {"document_name": doc.Name, "site": site.Label,
            "building": building.Label, "floor": floor.Label}


@register(
    "add_wall_from_sketch",
    "Extrude an existing sketch into an Arch Wall. Check the sketch with "
    "sketch_info first — building on an under-constrained baseline gives walls "
    "that move when anything else changes.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC,
            "sketch_name": _SKETCH,
            "height_mm": {"type": "number", "description": "Wall height in mm."},
            "width_mm": {"type": "number", "description": "Wall thickness in mm."},
            "floor_label": {"type": "string", "description": "Label of the BuildingPart to add it to."},
            "align": {"type": "string", "enum": ["Center", "Left", "Right"],
                      "description": "Which side of the baseline the wall sits on (default Center)."},
        },
        "required": ["doc_name", "sketch_name", "height_mm", "width_mm"],
    },
)
def add_wall_from_sketch(doc_name: str, sketch_name: str, height_mm: float, width_mm: float,
                         floor_label: str = "", align: str = "Center") -> dict:
    import Arch

    doc = _doc(doc_name)
    sk = _sketch(doc, sketch_name)
    wall = Arch.makeWall(sk, height=height_mm, width=width_mm, align=align)
    wall.Label = f"Wall_{sk.Label}"
    if floor_label:
        _by_label(doc, floor_label, "Floor").addObject(wall)
    doc.recompute()
    return {"wall_name": wall.Name, "wall_label": wall.Label,
            "invalid": _invalid(doc)}


@register(
    "add_opening",
    "Cut a door or window into an existing wall. pos_x/pos_y place it in the "
    "plan, sill_mm is its height above the floor, and rotation_deg should be 90 "
    "for a wall running along Y — an opening left unrotated on a north-south "
    "wall cuts nothing.",
    {
        "type": "object",
        "properties": {
            "doc_name": _DOC,
            "wall_label": {"type": "string", "description": "Label of the host Arch Wall."},
            "width_mm": {"type": "number", "description": "Opening width in mm."},
            "height_mm": {"type": "number", "description": "Opening height in mm."},
            "pos_x": {"type": "number", "description": "Absolute X position in mm."},
            "pos_y": {"type": "number", "description": "Absolute Y position in mm."},
            "label": {"type": "string", "description": "Name for the opening, e.g. 'FrontDoor'."},
            "sill_mm": {"type": "number", "description": "Height above the floor in mm (default 0)."},
            "rotation_deg": {"type": "number", "description": "Rotation about Z in degrees (default 0)."},
            "is_door": {"type": "boolean", "description": "True for a door, false for a window."},
        },
        "required": ["doc_name", "wall_label", "width_mm", "height_mm", "pos_x", "pos_y", "label"],
    },
)
def add_opening(doc_name: str, wall_label: str, width_mm: float, height_mm: float,
                pos_x: float, pos_y: float, label: str, sill_mm: float = 0.0,
                rotation_deg: float = 0.0, is_door: bool = True) -> dict:
    import Arch
    import Draft

    import Sketcher

    doc = _doc(doc_name)
    wall = _by_label(doc, wall_label, "Wall")

    # The profile is a Sketch standing upright in the wall's plane, not a flat
    # wire: Arch derives the window's cutting direction from the base plane, so
    # a profile lying in XY extrudes along the wall instead of through it and
    # removes nothing. A placed Draft wire fails differently but just as
    # silently — the window's own Shape comes out null once rotated.
    profile = doc.addObject("Sketcher::SketchObject", f"{label}Profile")
    profile.Placement = FreeCAD.Placement(
        FreeCAD.Vector(pos_x, pos_y, sill_mm),
        FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), rotation_deg).multiply(
            FreeCAD.Rotation(FreeCAD.Vector(1, 0, 0), 90)  # tip XY upright onto XZ
        ),
    )
    corners = [(0, 0), (width_mm, 0), (width_mm, height_mm), (0, height_mm)]
    for i, (x, y) in enumerate(corners):
        nx, ny = corners[(i + 1) % 4]
        profile.addGeometry(
            Part.LineSegment(FreeCAD.Vector(x, y, 0), FreeCAD.Vector(nx, ny, 0)), False
        )
    for i in range(4):
        profile.addConstraint(Sketcher.Constraint("Coincident", i, 2, (i + 1) % 4, 1))
    doc.recompute()

    opening = Arch.makeWindow(profile, name=label)
    opening.Label = label
    opening.Hosts = [wall]
    if hasattr(opening, "IfcType"):
        opening.IfcType = "Door" if is_door else "Window"

    # Follow the wall into its storey, so the opening moves with it.
    for obj in doc.Objects:
        if hasattr(obj, "Group") and wall in obj.Group:
            obj.addObject(opening)
            break

    doc.recompute()
    return {"opening_name": opening.Name, "opening_label": opening.Label,
            "hosted_in": wall_label, "invalid": _invalid(doc)}


def _invalid(doc) -> list[str]:
    """Labels of objects left in an error state — an empty recompute is a lie
    if the wall it produced is marked Invalid."""
    return [o.Label for o in doc.Objects
            if getattr(o, "State", None) and "Invalid" in o.State]


# Documentation lookup ----------------------------------------------------------

def _clean_wikitext(text: str) -> str:
    text = re.sub(r"<translate>|</translate>|<languages/>", "", text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\{\{Code\|code=(.*?)\}\}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"'{2,}", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@register(
    "search_wiki",
    "Look up a FreeCAD command in the bundled wiki mirror and return its "
    "Description and Scripting sections, Python examples included. Worth a call "
    "before writing code against any API you are not certain of — Sketcher "
    "constraint argument orders and Arch signatures especially.",
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Wiki page name ('Sketcher_ConstrainDistanceX', "
                               "'Arch_Wall') or a plain term ('wall opening').",
            }
        },
        "required": ["query"],
    },
)
def search_wiki(query: str) -> dict:
    wiki_dir = os.path.join(_ADDON_DIR, "FreeCAD-Documentation-Project", "wiki")
    if not os.path.isdir(wiki_dir):
        raise ValueError(f"Wiki mirror not found at {wiki_dir!r}.")

    pages = [f for f in os.listdir(wiki_dir) if f.endswith(".wikitext")]
    q = query.strip().replace(" ", "_").lower()
    terms = [p for p in q.replace("_", " ").split() if len(p) > 2]

    def score(page: str) -> int:
        name = page[:-9].lower()
        if q == name:
            return 100
        if name.startswith(q):
            return 80
        if q in name:
            return 60
        return 30 if terms and all(t in name for t in terms) else 0

    matches = sorted(((score(p), p) for p in pages if score(p)), reverse=True)
    if not matches:
        near = sorted({p[:-9] for p in pages if any(t in p.lower() for t in terms)})[:10]
        # The wiki is organised by GUI command name, so a scripted API question
        # — "App::VarSet", "ExpressionEngine" — matches nothing and, when the
        # title shares no words with any page, matches nothing to suggest either.
        # A dead end with an empty list is where the model starts guessing, and
        # the other reference tool is the one that answers this shape of question.
        return {
            "error": f"No wiki page matches '{query}'.",
            "suggestions": near,
            "try_instead": (
                "The wiki is indexed by GUI command name. For a scripted API "
                f"question, call lookup_freecad_op({query!r}) — it maps operations "
                "to their Python, including types the wiki does not cover."
            ),
        }

    path = os.path.join(wiki_dir, matches[0][1])
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read(16 * 1024)

    redirect = re.match(r"#REDIRECT\s*\[\[([^\]]+)\]\]", raw.strip())
    if redirect:
        target = os.path.join(wiki_dir, redirect.group(1).replace(" ", "_") + ".wikitext")
        if os.path.exists(target):
            path = target
            with open(path, encoding="utf-8", errors="replace") as f:
                raw = f.read(16 * 1024)

    def section(heading: str, limit: int) -> str:
        m = re.search(rf"==\s*{heading}\s*==(.+?)(?===|\Z)", raw, re.DOTALL | re.IGNORECASE)
        return _clean_wikitext(m.group(1))[:limit] if m else ""

    return {
        "page": os.path.basename(path)[:-9],
        "description": section("Description", 600),
        "scripting": section("Scripting", 3000) or "(no Scripting section on this page)",
        "related_pages": [m[1][:-9] for m in matches[1:6]],
    }


def _skill_names() -> list[str]:
    """Names of the bundled skills, straight off disk."""
    skills_dir = os.path.join(_ADDON_DIR, "skills")
    return sorted(f[:-3] for f in os.listdir(skills_dir) if f.endswith(".md"))


# The names go in the schema, not just the prose: without them models guess
# ("list", "help") or omit `name` entirely and burn a round trip on the error.
_SKILLS = _skill_names()


@register(
    "get_skill",
    "Read a bundled workflow guide — step-by-step recipes and worked code for "
    "one area of FreeCAD. Call this before starting that kind of job, not after "
    "getting stuck. The only guides that exist are: " + ", ".join(_SKILLS) + ".",
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "enum": _SKILLS,
                "description": "Skill name. One of: " + ", ".join(_SKILLS),
            }
        },
        "required": ["name"],
    },
)
def get_skill(name: str) -> dict:
    skills_dir = os.path.join(_ADDON_DIR, "skills")
    safe = os.path.basename(name).lower()
    path = os.path.join(skills_dir, f"{safe}.md")
    if not os.path.exists(path):
        raise ValueError(
            f"No skill named '{safe}'. Available: {', '.join(_skill_names())}")
    with open(path, encoding="utf-8") as f:
        return {"skill": safe, "content": f.read()}


_OPS_CACHE: dict = {}


def _ops() -> dict:
    """The bundled FreeCAD operation map, loaded once."""
    if not _OPS_CACHE:
        path = os.path.join(_ADDON_DIR, "skills", "freecad_ops.json")
        with open(path, encoding="utf-8") as f:
            _OPS_CACHE.update(json.load(f))
    return _OPS_CACHE


@register(
    "lookup_freecad_op",
    "Look up how to do a FreeCAD operation this addon has no dedicated tool for "
    "— fillet, chamfer, revolution, loft, sweep, boolean, Draft, Assembly, "
    "TechDraw, FEM, Spreadsheet, mesh import/export. Returns the scripted Python "
    "for each match, what must exist first, what usually follows next, and the "
    "non-obvious part. Call this BEFORE writing execute_code against an API you "
    "are not certain of: the scripted FreeCAD API changed at 1.0 and guessing "
    "produces code that fails on an attribute that no longer exists.",
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you want to do: 'fillet', 'revolve a sketch', "
                               "'export STEP', 'linear pattern'.",
            },
            "limit": {"type": "integer", "description": "Max matches, default 8."},
        },
        "required": ["query"],
    },
)
def lookup_freecad_op(query: str, limit: int = 8) -> dict:
    data = _ops()
    words = [w for w in re.split(r"\W+", query.lower()) if len(w) > 2]
    # "revolve" has to find "Revolution", "extrude" has to find "Extrusion". A
    # 5-character prefix covers the endings FreeCAD's command names actually use
    # without pulling in everything that merely starts the same way.
    terms = set(words) | {w[:5] for w in words if len(w) > 5}

    haystacks = []
    for category, entries in data["tools"].items():
        for entry in entries:
            hay = " ".join(str(entry.get(k, "")) for k in ("id", "py", "note")).lower()
            haystacks.append((category, entry, entry.get("id", "").lower(),
                              hay + " " + category.lower()))

    # Rare terms decide the answer. Without this, "revolve a sketch" returns
    # Sketcher commands: "sketch" appears in dozens of entries and outweighs the
    # one word that actually names what was asked for.
    weight = {}
    for term in terms:
        df = sum(1 for _, _, _, hay in haystacks if term in hay)
        weight[term] = 1.0 / (1 + df)

    scored = []
    for category, entry, name, hay in haystacks:
        score = sum(hay.count(t) * weight[t] for t in terms)
        # A hit in the command's own name outranks a passing mention in someone
        # else's note.
        score += 5 * sum(weight[t] for t in terms if t in name)
        if score:
            scored.append((score, category, entry))
    scored.sort(key=lambda s: -s[0])
    matches = [
        {"category": c, **{k: v for k, v in e.items() if v}}
        for _, c, e in scored[: max(1, min(limit, 20))]
    ]
    if not matches:
        raise ValueError(
            f"Nothing matches '{query}'. Categories: {', '.join(data['tools'])}."
        )
    # The playbook entries are short and are exactly the things that get this
    # wrong, so they ride along rather than needing a second call.
    hits = {k: v for k, v in data["failure_playbook"].items()
            if any(w in k.lower() for w in words)}
    return {
        "matches": matches,
        **({"failure_playbook": hits} if hits else {}),
        "conventions": data["conventions"],
    }


def demo() -> None:
    """End-to-end check: sketch a room, wall it, cut a door.

    Run: ``freecadcmd -c "import sys; sys.path.insert(0, '<addon>'); \
from .bim_handlers import demo; demo()"``
    """
    doc = create_bim_document("BimHandlersDemo")["document_name"]
    try:
        d = FreeCAD.getDocument(doc)
        sk = d.addObject("Sketcher::SketchObject", "Plan")

        # A 4 x 3 m room, walls named as they are drawn.
        corners = [(0, 0), (4000, 0), (4000, 3000), (0, 3000)]
        for i, (x1, y1) in enumerate(corners):
            x2, y2 = corners[(i + 1) % 4]
            sketch_line(doc, "Plan", x1, y1, x2, y2, label=f"Wall{i}")

        d.recompute()
        sk.solve()
        assert sk.FullyConstrained, f"plan should be fully constrained, DoF={sk.DoF}"

        # Absolute pinning must put the geometry where it was asked for, not at
        # its mirror image — the failure mode that hid in constrainPoint.
        plan = get_sketch_geometry(doc, "Plan")
        assert plan["segment_count"] == 4, plan
        first = next(s for s in plan["segments"] if s["label"] == "Wall0")
        assert first["start"] == [0, 0] and first["end"] == [4000, 0], first
        assert {s["label"] for s in plan["segments"]} == {f"Wall{i}" for i in range(4)}, plan

        wall = add_wall_from_sketch(doc, "Plan", height_mm=2400, width_mm=200,
                                    floor_label="Floor 1")
        assert not wall["invalid"], wall
        solid = d.getObject(wall["wall_name"])
        assert solid.Shape.Volume > 0, "wall has no volume"
        before = solid.Shape.Volume

        # A door on the south wall, which runs along X: no rotation needed.
        opening = add_opening(doc, wall["wall_label"], width_mm=900, height_mm=2100,
                              pos_x=1000, pos_y=0, label="FrontDoor")
        assert not opening["invalid"], opening
        d.recompute()
        cut = before - solid.Shape.Volume
        assert abs(cut - 900 * 2100 * 200) < 1, (
            f"door should remove width*height*thickness; removed {cut}"
        )

        # ...and a window on the east wall, which runs along Y. This is the case
        # rotation_deg exists for: unrotated, the opening lies along the wall
        # instead of across it and removes nothing.
        before2 = solid.Shape.Volume
        add_opening(doc, wall["wall_label"], width_mm=1200, height_mm=1000,
                    pos_x=4000, pos_y=1000, label="SideWindow",
                    sill_mm=900, rotation_deg=90, is_door=False)
        d.recompute()
        cut2 = before2 - solid.Shape.Volume
        assert abs(cut2 - 1200 * 1000 * 200) < 1, (
            f"rotated window should cut across the wall; removed {cut2}"
        )

        # Lookups: a real page, and a miss that suggests rather than raises.
        assert "Arch" in search_wiki("Arch_Wall")["page"]
        assert "suggestions" in search_wiki("zzzz_no_such_page_zzzz")
        assert "bim" == get_skill("bim")["skill"]
        try:
            get_skill("nope")
            raise AssertionError("expected ValueError for an unknown skill")
        except ValueError:
            pass

        # Every snake_case call a skill shows must be a real tool or a real
        # execute_code helper. Skills are prose: renaming a tool breaks nothing
        # and nobody notices. bim.md told models to call execute_python,
        # check_sketch, sketch_connect, sketch_arc and sketch_constrain_point —
        # none of which has ever existed anywhere in this addon — and the only
        # symptom was an agent that looked incompetent at BIM.
        import builtins

        from . import tools as _tools
        from .build_helpers import HELPERS

        known = ({t["name"] for t in _tools.describe_tools()}
                 | set(HELPERS) | set(dir(builtins)))
        for skill in _skill_names():
            text = get_skill(skill)["content"]
            # Code spans only, and only names with an underscore: prose is full
            # of "(all values in mm)" and would drown the signal otherwise.
            code = "\n".join(re.findall(r"```.*?```", text, re.S)
                             + re.findall(r"`([^`\n]+)`", text))
            called = {c for c in re.findall(r"(?<![\w.])([a-z][a-z_0-9]*)\s*\(", code)
                      if "_" in c}
            assert not called - known, \
                f"{skill}.md calls what does not exist: {sorted(called - known)}"

        # lookup_freecad_op: both relevance rules that were wrong at first.
        ids = lambda q: [m["id"] for m in lookup_freecad_op(q, limit=4)["matches"]]
        # Stemming: "revolve" has to reach "Revolution".
        assert "PartDesign_Revolution" in ids("revolve a sketch"), ids("revolve a sketch")
        # Rare terms decide: "sketch" appears in dozens of entries and must not
        # drown out the one word naming what was asked for.
        assert "Part_Revolve" in ids("revolve a sketch")
        assert "PartDesign_Chamfer" in ids("chamfer an edge")
        assert "PartDesign_Mirrored" in ids("mirror a feature")
        got = lookup_freecad_op("fillet")
        assert got["matches"] and "conventions" in got, list(got)
        try:
            lookup_freecad_op("zzzznotathing")
            raise AssertionError("expected ValueError for no match")
        except ValueError as exc:
            assert "Categories" in str(exc), exc

        print("bim_handlers demo: all checks passed")
    finally:
        FreeCAD.closeDocument(doc)
