import io
import contextlib
import traceback
import platform


def _app():
    import FreeCAD
    return FreeCAD


def _gui():
    import FreeCADGui
    return FreeCADGui


# ---------------------------------------------------------------------------
# Persistent execution context
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Sketch helper functions (injected into exec_ctx)
#
# These use the absolute-positioning strategy from GearWorkBench/util.py:
# each point is pinned via DistanceX/DistanceY measured FROM the sketch
# origin (-1, 1).  A point at (0, -3000) just passes y_val=-3000 — no
# direction or sign calculation needed by the caller.
# ---------------------------------------------------------------------------

def _sketch_constrain_point(sketch, geo_idx, pt_idx, x, y, name_prefix=None):
    """Pin a sketch point to absolute (x, y) coordinates.

    Mirrors util.py:constrainSketchPoint — uses PointOnObject for zero
    axes and signed DistanceX/DistanceY from the origin for everything else.
    If name_prefix is given, constraints are renamed for readability in the UI.
    """
    import Sketcher
    n = sketch.ConstraintCount  # index of next constraint to be added
    if x == 0 and y == 0:
        sketch.addConstraint(Sketcher.Constraint("Coincident", geo_idx, pt_idx, -1, 1))
        if name_prefix:
            try:
                sketch.renameConstraint(n, f"{name_prefix}_origin")
            except Exception:
                pass
        return
    if x == 0:
        sketch.addConstraint(Sketcher.Constraint("PointOnObject", geo_idx, pt_idx, -2))
        if name_prefix:
            try:
                sketch.renameConstraint(n, f"{name_prefix}_on_yaxis")
            except Exception:
                pass
        n += 1
    else:
        sketch.addConstraint(Sketcher.Constraint("DistanceX", -1, 1, geo_idx, pt_idx, x))
        if name_prefix:
            try:
                sketch.renameConstraint(n, f"{name_prefix}_x")
            except Exception:
                pass
        n += 1
    if y == 0:
        sketch.addConstraint(Sketcher.Constraint("PointOnObject", geo_idx, pt_idx, -1))
        if name_prefix:
            try:
                sketch.renameConstraint(n, f"{name_prefix}_on_xaxis")
            except Exception:
                pass
    else:
        sketch.addConstraint(Sketcher.Constraint("DistanceY", -1, 1, geo_idx, pt_idx, y))
        if name_prefix:
            try:
                sketch.renameConstraint(n, f"{name_prefix}_y")
            except Exception:
                pass


def _sketch_line(sketch, x1, y1, x2, y2, construction=False, label=None):
    """Add a fully-constrained line by pinning both endpoints absolutely.

    Both start (x1,y1) and end (x2,y2) are fixed via absolute DistanceX/Y
    constraints measured from the sketch origin.  No sign calculation needed:
    a wall going south from (0,0) to (0,-3000) simply passes y2=-3000.

    Pass label (e.g. 'LoftWall') to name the constraints in the Sketcher UI
    so each wall's dimensions are identifiable.

    When two lines share an endpoint the constraints are redundant (not
    conflicting), which the Sketcher solver handles fine.

    Returns the geometry index.
    """
    import Part, FreeCAD as App
    idx = sketch.addGeometry(
        Part.LineSegment(App.Vector(x1, y1, 0), App.Vector(x2, y2, 0)),
        construction,
    )
    start_prefix = f"{label}_start" if label else None
    end_prefix   = f"{label}_end"   if label else None
    _sketch_constrain_point(sketch, idx, 1, x1, y1, name_prefix=start_prefix)
    _sketch_constrain_point(sketch, idx, 2, x2, y2, name_prefix=end_prefix)
    return idx


def _sketch_connect(sketch, idx1, pt1, idx2, pt2):
    """Add a Coincident constraint between two sketch points.

    pt: 1=start, 2=end.  Use idx=-1, pt=1 for the sketch origin.
    Useful when you want an explicit topological connection without
    re-pinning absolute positions.
    """
    import Sketcher
    sketch.addConstraint(Sketcher.Constraint("Coincident", idx1, pt1, idx2, pt2))


def _sketch_arc(sketch, x, y, diameter, start_angle_rad, end_angle_rad, construction=False):
    """Add an arc with centre pinned to absolute (x, y).

    start_angle_rad / end_angle_rad are in radians.
    Returns {'index': int, 'start_point': Vector, 'end_point': Vector}.
    """
    import Part, Sketcher, FreeCAD as App
    radius = diameter / 2.0
    base = Part.Circle(App.Vector(x, y, 0), App.Vector(0, 0, 1), radius)
    arc = Part.ArcOfCircle(base, start_angle_rad, end_angle_rad)
    start_p = arc.StartPoint
    end_p = arc.EndPoint
    idx = sketch.addGeometry(arc, construction)
    # Pin centre (point index 3)
    _sketch_constrain_point(sketch, idx, 3, x, y)
    sketch.addConstraint(Sketcher.Constraint("Diameter", idx, diameter))
    return {"index": idx, "start_point": start_p, "end_point": end_p}


def _sketch_circle(sketch, x, y, diameter, construction=False):
    """Add a circle with centre pinned to absolute (x, y).

    Returns geometry index.
    """
    import Part, Sketcher, FreeCAD as App
    idx = sketch.addGeometry(
        Part.Circle(App.Vector(x, y, 0), App.Vector(0, 0, 1), diameter / 2.0),
        construction,
    )
    _sketch_constrain_point(sketch, idx, 3, x, y)
    sketch.addConstraint(Sketcher.Constraint("Diameter", idx, diameter))
    return idx


def _sketch_polygon(sketch, points, closed=True, construction=False):
    """Add a polygon (series of connected line segments) to a sketch.

    points: list of (x, y) tuples or App.Vector objects.
    Returns list of geometry indices.
    """
    import Part, Sketcher, FreeCAD as App

    vecs = []
    for p in points:
        if isinstance(p, (tuple, list)):
            vecs.append(App.Vector(p[0], p[1], 0))
        else:
            vecs.append(p)

    indices = []
    n = len(vecs)
    pairs = list(range(n - 1))
    if closed:
        pairs.append(n - 1)  # last→first

    for i in pairs:
        p1 = vecs[i]
        p2 = vecs[(i + 1) % n]
        idx = sketch.addGeometry(Part.LineSegment(p1, p2), construction)
        indices.append(idx)

    # Coincident to chain them
    for i in range(len(indices) - 1):
        sketch.addConstraint(Sketcher.Constraint("Coincident", indices[i], 2, indices[i + 1], 1))
    if closed and len(indices) > 1:
        sketch.addConstraint(Sketcher.Constraint("Coincident", indices[-1], 2, indices[0], 1))

    return indices


def _init_exec_ctx(ctx: dict):
    """Populate ctx with standard FreeCAD modules and sketch/BIM helpers."""
    # Module imports are expensive — only run once per session.
    if "App" not in ctx:
        try:
            import FreeCAD
            ctx["App"] = FreeCAD
            ctx["FreeCAD"] = FreeCAD
        except Exception:
            pass
        try:
            import FreeCADGui
            ctx["Gui"] = FreeCADGui
            ctx["FreeCADGui"] = FreeCADGui
        except Exception:
            pass
        try:
            import Part
            ctx["Part"] = Part
        except Exception:
            pass
        import importlib
        for mod in ("Sketcher", "Draft", "Arch", "PartDesign", "FEM", "Path", "Mesh"):
            try:
                ctx[mod] = importlib.import_module(mod)
            except Exception:
                pass

    # Helpers are cheap to re-assign and must always be present, even in sessions
    # that were initialised before new helpers were added.
    ctx["sketch_line"]            = _sketch_line
    ctx["sketch_connect"]         = _sketch_connect
    ctx["sketch_constrain_point"] = _sketch_constrain_point
    ctx["sketch_arc"]             = _sketch_arc
    ctx["sketch_circle"]          = _sketch_circle
    ctx["sketch_polygon"]         = _sketch_polygon
    ctx["get_sketch_geometry"]    = get_sketch_geometry
    ctx["check_sketch"]           = check_sketch
    ctx["add_wall_from_sketch"]   = add_wall_from_sketch
    ctx["add_opening"]            = add_opening
    ctx["create_opening"]         = add_opening   # common AI alias
    ctx["create_bim_document"]    = create_bim_document
    ctx["delete_object"]          = delete_object
    ctx["save_document"]          = save_document
    ctx["open_document"]          = open_document
    ctx["read_file"]              = read_file
    ctx["export_file"]            = export_file

    # Refresh doc/recompute every call so they always reflect the active document.
    try:
        active = ctx.get("App") or __import__("FreeCAD")
        doc = active.ActiveDocument
        if doc is not None:
            ctx["doc"] = doc
            ctx["recompute"] = doc.recompute
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Timeout helper (Unix only — SIGALRM on main/GUI thread)
# ---------------------------------------------------------------------------

def _run_with_timeout(fn, seconds=30):
    if platform.system() == "Windows":
        fn()
        return
    import signal

    def _handler(signum, frame):
        raise TimeoutError(
            f"Code execution timed out after {seconds}s — possible infinite loop."
        )

    old = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        fn()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# ---------------------------------------------------------------------------
# Core tools
# ---------------------------------------------------------------------------

def execute_python(code: str, exec_ctx: dict) -> dict:
    App = _app()

    normalized = code.replace(" ", "").replace("'", '"')
    if 'os.walk("/")' in normalized or 'os.walk(os.path.sep)' in normalized:
        return {
            "error": "Scanning '/' is blocked to prevent freezing FreeCAD. "
                     "Only search within specific project paths."
        }

    _init_exec_ctx(exec_ctx)

    try:
        compiled = compile(code, "<ai_generated>", "exec")
    except SyntaxError as e:
        return {
            "error": f"SyntaxError on line {e.lineno}: {e.msg}\n  {e.text}",
            "output": "",
        }

    buf = io.StringIO()

    def _run():
        with contextlib.redirect_stdout(buf):
            exec(compiled, exec_ctx)  # noqa: S102

    try:
        _run_with_timeout(_run, seconds=30)
    except SystemExit as e:
        return {"error": f"Code called exit({e.code}) — use return or raise instead.", "output": buf.getvalue()}
    except TimeoutError as e:
        return {"error": str(e), "output": buf.getvalue()}
    except Exception:
        return {"error": traceback.format_exc(), "output": buf.getvalue()}

    result = {"status": "ok"}
    output = buf.getvalue()
    if output:
        result["output"] = output

    # Auto-report any objects left in an invalid/error state after execution
    try:
        doc = App.ActiveDocument
        if doc:
            invalid = [
                obj.Label for obj in doc.Objects
                if hasattr(obj, "State") and obj.State and "Invalid" in obj.State
            ]
            if invalid:
                result["recompute_errors"] = invalid
    except Exception:
        pass

    return result


def read_file(path: str) -> dict:
    MAX_BYTES = 64 * 1024  # 64 KB — enough for any text document
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read(MAX_BYTES)
        truncated = len(content) == MAX_BYTES
        return {"content": content, "truncated": truncated}
    except FileNotFoundError:
        return {"error": f"File not found: {path}"}
    except Exception:
        return {"error": traceback.format_exc()}


def open_document(path: str) -> dict:
    App = _app()
    try:
        doc = App.openDocument(path)
        INTERNAL_TYPES = frozenset({"App::Origin", "App::Line", "App::Plane"})
        objects = [
            {"name": o.Name, "label": o.Label, "type": o.TypeId}
            for o in doc.Objects if o.TypeId not in INTERNAL_TYPES
        ]
        return {"status": "ok", "document": doc.Name, "objects": objects}
    except Exception:
        return {"error": traceback.format_exc()}


def save_document(path: str | None = None) -> dict:
    App = _app()
    doc = App.ActiveDocument
    if doc is None:
        return {"error": "No active document to save."}
    try:
        if path:
            doc.saveAs(path)
        else:
            if not doc.FileName:
                return {"error": "Document has no filename — provide a path."}
            doc.save()
        return {"status": "ok", "saved_to": doc.FileName}
    except Exception:
        return {"error": traceback.format_exc()}


def delete_object(name: str) -> dict:
    App = _app()
    doc = App.ActiveDocument
    if doc is None:
        return {"error": "No active document."}
    obj = doc.getObject(name) or next(
        (o for o in doc.Objects if o.Label == name), None
    )
    if obj is None:
        return {"error": f"Object not found: {name!r}"}
    label = obj.Label
    internal_name = obj.Name
    try:
        doc.removeObject(internal_name)
        doc.recompute()
        return {"status": "ok", "deleted": label}
    except Exception:
        return {"error": traceback.format_exc()}


def undo(steps: int = 1) -> dict:
    App = _app()
    doc = App.ActiveDocument
    if doc is None:
        return {"error": "No active document."}
    try:
        for _ in range(steps):
            doc.undo()
        doc.recompute()
        return {"status": "ok", "steps_undone": steps}
    except Exception:
        return {"error": traceback.format_exc()}


def export_file(path: str, object_names: list | None = None) -> dict:
    App = _app()
    doc = App.ActiveDocument
    if doc is None:
        return {"error": "No active document."}
    try:
        if object_names:
            objs = [doc.getObject(n) or next(
                (o for o in doc.Objects if o.Label == n), None
            ) for n in object_names]
            objs = [o for o in objs if o is not None]
        else:
            objs = doc.Objects
        if not objs:
            return {"error": "No objects to export."}
        import importlib
        ext = path.rsplit(".", 1)[-1].lower()
        # FreeCAD's export dispatcher
        importlib.import_module("FreeCAD")  # ensure available
        App.export(objs, path)
        return {"status": "ok", "exported_to": path, "object_count": len(objs)}
    except Exception:
        return {"error": traceback.format_exc()}


def get_document_info() -> dict:
    App = _app()
    Gui = _gui()
    doc = App.ActiveDocument
    if doc is None:
        return {"document": None, "message": "No active document"}

    # Filter out internal origin geometry that adds noise
    INTERNAL_TYPES = frozenset({"App::Origin", "App::Line", "App::Plane"})
    all_objects = [o for o in doc.Objects if o.TypeId not in INTERNAL_TYPES]
    total = len(all_objects)
    objects = [
        {"name": o.Name, "label": o.Label, "type": o.TypeId}
        for o in all_objects[:50]
    ]

    try:
        sel = [
            {"name": s.ObjectName, "type": s.Object.TypeId}
            for s in Gui.Selection.getSelectionEx()
            if s.Object
        ]
    except Exception:
        sel = []

    try:
        active_wb = Gui.activeWorkbench().name()
    except Exception:
        active_wb = "unknown"

    result = {
        "document": doc.Name,
        "label": doc.Label,
        "total_objects": total,
        "objects": objects,
        "selection": sel,
        "active_workbench": active_wb,
    }
    if total > 50:
        result["note"] = (
            f"Showing first 50 of {total} objects. "
            "Use execute_python to query specific objects."
        )
    return result


def list_workbenches() -> dict:
    Gui = _gui()
    try:
        return {"workbenches": list(Gui.listWorkbenches().keys())}
    except Exception as e:
        return {"error": str(e)}


def switch_workbench(name: str) -> dict:
    Gui = _gui()
    try:
        wbs = Gui.listWorkbenches()
        # Exact match first
        if name in wbs:
            Gui.activateWorkbench(name)
            return {"status": "ok", "active": name}
        # Case-insensitive match; also try appending/stripping "Workbench"
        nl = name.lower()
        for key in wbs:
            kl = key.lower()
            if kl == nl or kl == nl + "workbench" or kl.removesuffix("workbench") == nl:
                Gui.activateWorkbench(key)
                return {"status": "ok", "active": key}
        return {"error": f"Workbench '{name}' not found. Available: {list(wbs.keys())}"}
    except Exception as e:
        return {"error": str(e)}


def list_commands(workbench: str = "") -> dict:
    Gui = _gui()
    try:
        if workbench:
            wb = Gui.listWorkbenches().get(workbench)
            if wb is None:
                return {"error": f"Workbench '{workbench}' not found"}
            cmds = getattr(wb, "listCommands", lambda: [])()
        else:
            cmds = Gui.activeWorkbench().listCommands()
        return {"commands": list(cmds)}
    except Exception as e:
        return {"error": str(e)}


def fit_view() -> dict:
    Gui = _gui()
    try:
        Gui.SendMsgToActiveView("ViewFit")
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}


def recompute() -> dict:
    App = _app()
    doc = App.ActiveDocument
    if doc is None:
        return {"error": "No active document"}
    try:
        failed = doc.recompute()
        if failed:
            labels = [o.Label for o in failed if hasattr(o, "Label")]
            return {"status": "errors", "recompute_errors": labels}
        return {"status": "ok"}
    except Exception as e:
        return {"error": str(e)}


def get_sketch_geometry(sketch_name: str) -> dict:
    """Return every line/arc in a sketch with its label and coordinates.

    Labels come from the constraint names set by sketch_line (e.g. a constraint
    named 'LoftWall_start_x' maps geometry index → label 'LoftWall').
    Use this to inspect what is already in the sketch before adding more lines,
    or to resume after an interrupted session instead of deleting and restarting.
    """
    App = _app()
    doc = App.ActiveDocument
    if doc is None:
        return {"error": "No active document"}
    sketch = doc.getObject(sketch_name)
    if sketch is None:
        sketch = next((o for o in doc.Objects if o.Label == sketch_name), None)
    if sketch is None:
        return {"error": f"Sketch '{sketch_name}' not found"}
    try:
        import Part

        # Build geo_index → wall_label from named constraints
        geo_labels: dict[int, str] = {}
        for c in sketch.Constraints:
            name = c.Name or ""
            idx = c.First
            if idx < 0 or not name:
                continue
            for marker in ("_start_", "_end_"):
                if marker in name:
                    label = name[: name.index(marker)]
                    if idx not in geo_labels:
                        geo_labels[idx] = label
                    break

        segments = []
        for i, geo in enumerate(sketch.Geometry):
            entry = {
                "index": i,
                "label": geo_labels.get(i, ""),
                "construction": bool(getattr(geo, "Construction", False)),
            }
            if isinstance(geo, Part.LineSegment):
                s, e = geo.StartPoint, geo.EndPoint
                entry["type"] = "line"
                entry["start"] = (round(s.x, 3), round(s.y, 3))
                entry["end"]   = (round(e.x, 3), round(e.y, 3))
            elif isinstance(geo, Part.ArcOfCircle):
                c = geo.Center
                entry["type"]   = "arc"
                entry["center"] = (round(c.x, 3), round(c.y, 3))
                entry["radius"] = round(geo.Radius, 3)
            else:
                entry["type"] = type(geo).__name__
            segments.append(entry)

        return {
            "status": "ok",
            "sketch_name": sketch.Name,
            "segment_count": len(segments),
            "segments": segments,
        }
    except Exception:
        return {"error": traceback.format_exc()}


def check_sketch(sketch_name: str) -> dict:
    """Report the constraint status of a sketch before using it as a wall base."""
    App = _app()
    doc = App.ActiveDocument
    if doc is None:
        return {"error": "No active document"}
    # Accept internal Name or user Label
    sketch = doc.getObject(sketch_name)
    if sketch is None:
        sketch = next((o for o in doc.Objects if o.Label == sketch_name), None)
    if sketch is None:
        return {"error": f"Sketch '{sketch_name}' not found (tried Name and Label)"}
    try:
        solver_status = sketch.solve()
        # FreeCAD API varies across versions — try method then property fallback
        try:
            redundant = list(sketch.getRedundancies())
        except AttributeError:
            redundant = list(getattr(sketch, "RedundantConstraints", []))
        try:
            conflicting = list(sketch.getConflicts())
        except AttributeError:
            conflicting = list(getattr(sketch, "ConflictingConstraints", []))
        result = {
            "status": "ok",
            "sketch_name": sketch.Name,
            "sketch_label": sketch.Label,
            "geometry_count": sketch.GeometryCount,
            "constraint_count": sketch.ConstraintCount,
            "solver_status": solver_status,
            "redundant_constraints": redundant,
            "conflicting_constraints": conflicting,
        }
        if conflicting:
            result["fully_constrained"] = False
            result["action"] = (
                "STOP — conflicting constraints detected. "
                "Remove the listed constraints and re-add them with correct signed values."
            )
        elif solver_status == 0 or (solver_status < 0 and not conflicting):
            # solver_status == 0: fully constrained, no issues
            # solver_status < 0: FreeCAD reports "solved but has redundant constraints"
            result["fully_constrained"] = True
            note = " (redundant constraints from shared endpoints are harmless)" if redundant else ""
            result["action"] = f"Sketch is fully constrained — safe to call add_wall_from_sketch.{note}"
        else:
            result["fully_constrained"] = False
            result["action"] = f"Under-constrained ({solver_status} DOF remaining) — add more constraints."
        return result
    except Exception:
        return {"error": traceback.format_exc()}


def search_wiki(query: str) -> dict:
    """Search the local FreeCAD wiki mirror for documentation on a command or topic.

    Returns the Scripting section (Python API examples) of the best-matching page,
    plus a short description.  Use this when you need the exact API syntax for a
    Sketcher constraint, Arch command, Draft tool, etc.
    """
    import os, re

    mod_dir = os.path.dirname(os.path.abspath(__file__))
    wiki_dir = os.path.join(mod_dir, "FreeCAD-Documentation-Project", "wiki")
    if not os.path.isdir(wiki_dir):
        return {"error": "Wiki documentation directory not found."}

    query_norm = query.strip().replace(" ", "_")
    query_lower = query_norm.lower()

    all_pages = [f for f in os.listdir(wiki_dir) if f.endswith(".wikitext")]

    # Score filename matches
    matches = []
    for page in all_pages:
        name = page[:-9].lower()
        if query_lower == name:
            score = 100
        elif name.startswith(query_lower):
            score = 80
        elif query_lower in name:
            score = 60
        else:
            parts = [p for p in query_lower.replace("_", " ").split() if len(p) > 2]
            if parts and all(p in name for p in parts):
                score = 30
            else:
                score = 0
        if score > 0:
            matches.append((score, page))

    if not matches:
        parts = [p for p in query_lower.replace("_", " ").split() if len(p) > 2]
        suggestions = sorted(
            set(f[:-9] for f in all_pages if any(p in f.lower() for p in parts))
        )[:10]
        return {
            "error": f"No wiki page found for '{query}'.",
            "suggestions": suggestions or ["Try a broader term, e.g. 'Sketcher_Constraint' or 'Arch_Wall'"],
        }

    matches.sort(reverse=True)
    page_file = matches[0][1]
    path = os.path.join(wiki_dir, page_file)

    # Follow #REDIRECT
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(256)
        redirect = re.match(r'#REDIRECT\s*\[\[([^\]]+)\]\]', head.strip())
        if redirect:
            target = redirect.group(1).replace(" ", "_") + ".wikitext"
            target_path = os.path.join(wiki_dir, target)
            if os.path.exists(target_path):
                path = target_path
                page_file = target
    except Exception:
        pass

    MAX_BYTES = 16 * 1024
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read(MAX_BYTES)
    except Exception as e:
        return {"error": f"Could not read wiki page: {e}"}

    def _clean(text: str) -> str:
        text = re.sub(r'<translate>|</translate>|<languages/>', '', text)
        text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        # Unwrap {{Code|code=...}} blocks
        text = re.sub(r'\{\{Code\|code=(.*?)\}\}', r'\1', text, flags=re.DOTALL)
        # Strip remaining templates
        text = re.sub(r'\{\{[^{}]*\}\}', '', text)
        # Simplify wiki links [[target|label]] → label
        text = re.sub(r'\[\[(?:[^|\]]*\|)?([^\]]+)\]\]', r'\1', text)
        text = re.sub(r"'{2,}", '', text)
        text = re.sub(r'\[\[.*?\]\]', '', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    # Pull out Description and Scripting sections
    desc_match = re.search(r'==\s*Description\s*==(.+?)(?===|\Z)', raw, re.DOTALL | re.IGNORECASE)
    script_match = re.search(r'==\s*Scripting\s*==(.+?)(?===|\Z)', raw, re.DOTALL | re.IGNORECASE)

    result: dict = {"page": page_file[:-9]}
    if desc_match:
        result["description"] = _clean(desc_match.group(1))[:600]
    if script_match:
        result["scripting"] = _clean(script_match.group(1))[:3000]
    else:
        result["scripting"] = "(no Scripting section on this page)"

    if len(matches) > 1:
        result["related_pages"] = [m[1][:-9] for m in matches[1:6]]

    return result


def get_skill(name: str) -> dict:
    import os
    safe_name = os.path.basename(name).lower()
    mod_dir = os.path.dirname(os.path.abspath(__file__))
    skill_path = os.path.join(mod_dir, "skills", f"{safe_name}.md")
    if not os.path.exists(skill_path):
        available = [
            f[:-3] for f in os.listdir(os.path.join(mod_dir, "skills"))
            if f.endswith(".md")
        ]
        return {"error": f"Skill '{safe_name}' not found. Available: {available}"}
    try:
        with open(skill_path, "r", encoding="utf-8") as f:
            return {"status": "ok", "skill": safe_name, "content": f.read()}
    except Exception as e:
        return {"error": f"Failed to read skill: {e}"}


# ---------------------------------------------------------------------------
# BIM helper tools
# ---------------------------------------------------------------------------

def create_bim_document(name: str = "BIMProject") -> dict:
    """Create a new document with a Site → Building → Floor hierarchy."""
    App = _app()
    try:
        import Arch
        doc = App.newDocument(name)
        site = Arch.makeSite(name="ProjectSite")
        site.Label = "Project Site"
        building = Arch.makeBuilding(name="MainBuilding")
        building.Label = "Building"
        site.addObject(building)
        floor = Arch.makeBuildingPart(name="Floor_1")
        floor.Label = "Floor 1"
        floor.Placement = App.Placement(
            App.Vector(0, 0, 0), App.Rotation(0, 0, 0)
        )
        building.addObject(floor)
        doc.recompute()
        return {
            "status": "ok",
            "document": doc.Name,
            "site": site.Label,
            "building": building.Label,
            "floor": floor.Label,
        }
    except Exception as e:
        return {"error": traceback.format_exc()}


def add_wall_from_sketch(
    sketch_name: str,
    height_mm: float,
    width_mm: float,
    floor_label: str = "",
    align: str = "Center",
) -> dict:
    """Create an Arch Wall from an existing sketch and optionally add it to a floor."""
    App = _app()
    try:
        import Arch
        doc = App.ActiveDocument
        if doc is None:
            return {"error": "No active document"}
        sketch = doc.getObject(sketch_name)
        if sketch is None:
            sketch = next((o for o in doc.Objects if o.Label == sketch_name), None)
        if sketch is None:
            return {"error": f"Sketch '{sketch_name}' not found (tried Name and Label)"}
        wall = Arch.makeWall(sketch, height=height_mm, width=width_mm, align=align)
        wall.Label = f"Wall_{sketch_name}"
        if floor_label:
            floor = next(
                (o for o in doc.Objects if o.Label == floor_label), None
            )
            if floor:
                floor.addObject(wall)
            else:
                return {"error": f"Floor with label '{floor_label}' not found"}
        doc.recompute()
        failed = [o.Label for o in doc.Objects
                  if hasattr(o, "State") and o.State and "Invalid" in o.State]
        result = {"status": "ok", "wall": wall.Label, "wall_name": wall.Name}
        if failed:
            result["recompute_errors"] = failed
        return result
    except Exception:
        return {"error": traceback.format_exc()}


def add_opening(
    wall_label: str,
    width_mm: float,
    height_mm: float,
    pos_x: float,
    pos_y: float,
    sill_mm: float = 0.0,
    rotation_deg: float = 0.0,
    label: str = "Opening",
    is_door: bool = True,
) -> dict:
    """
    Create a door or window and host it in a wall.

    pos_x, pos_y: absolute X/Y position in mm (place on the wall face).
    sill_mm: height above floor (Z offset).
    rotation_deg: rotation around Z axis in degrees.
    """
    App = _app()
    try:
        import Arch, Draft
        doc = App.ActiveDocument
        if doc is None:
            return {"error": "No active document"}

        wall = next((o for o in doc.Objects if o.Label == wall_label), None)
        if wall is None:
            return {"error": f"Wall with label '{wall_label}' not found"}

        # Build a closed rectangular profile in the XY plane
        p1 = App.Vector(0, 0, 0)
        p2 = App.Vector(width_mm, 0, 0)
        p3 = App.Vector(width_mm, height_mm, 0)
        p4 = App.Vector(0, height_mm, 0)
        profile = Draft.makeWire([p1, p2, p3, p4], closed=True)

        opening = Arch.makeWindow(profile, name=label)
        opening.Label = label

        rot = App.Rotation(App.Vector(0, 0, 1), rotation_deg)
        opening.Placement = App.Placement(
            App.Vector(pos_x, pos_y, sill_mm), rot
        )

        Arch.addComponents(opening, wall)

        # Add to same floor container as the wall, if any
        for obj in doc.Objects:
            if hasattr(obj, "Group") and wall in obj.Group:
                obj.addObject(opening)
                break

        doc.recompute()
        failed = [o.Label for o in doc.Objects
                  if hasattr(o, "State") and o.State and "Invalid" in o.State]
        result = {
            "status": "ok",
            "opening": opening.Label,
            "opening_name": opening.Name,
            "hosted_in": wall_label,
        }
        if failed:
            result["recompute_errors"] = failed
        return result
    except Exception:
        return {"error": traceback.format_exc()}


# ---------------------------------------------------------------------------
# Dispatch factory
# ---------------------------------------------------------------------------

def make_dispatch(exec_ctx: dict) -> dict:
    """Return a tool-name → callable mapping bound to the given exec context."""
    return {
        "execute_python":      lambda args: execute_python(exec_ctx=exec_ctx, **args),
        "read_file":           lambda args: read_file(**args),
        "open_document":       lambda args: open_document(**args),
        "save_document":       lambda args: save_document(**args),
        "delete_object":       lambda args: delete_object(**args),
        "undo":                lambda args: undo(**args),
        "export_file":         lambda args: export_file(**args),
        "get_document_info":   lambda args: get_document_info(),
        "list_workbenches":    lambda args: list_workbenches(),
        "switch_workbench":    lambda args: switch_workbench(**args),
        "list_commands":       lambda args: list_commands(**args),
        "fit_view":            lambda args: fit_view(),
        "recompute":           lambda args: recompute(),
        "get_skill":           lambda args: get_skill(**args),
        "search_wiki":         lambda args: search_wiki(**args),
        "get_sketch_geometry": lambda args: get_sketch_geometry(**args),
        "check_sketch":        lambda args: check_sketch(**args),
        "create_bim_document": lambda args: create_bim_document(**args),
        "add_wall_from_sketch": lambda args: add_wall_from_sketch(**args),
        "add_opening":         lambda args: add_opening(**args),
    }


# Kept for any legacy import; panel should use make_dispatch() instead.
TOOL_DISPATCH = make_dispatch({})
