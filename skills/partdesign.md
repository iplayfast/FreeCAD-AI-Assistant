# PartDesign — the default way to build in this workbench

Solid modelling here means **PartDesign**: a Body, sketches attached to that
body's origin planes, and features (Pad, Pocket, Polar Pattern) that consume
them. The deliverable is a document the user can open, edit a dimension in, and
recompute — not a shape that happens to look right once.

Two rules before anything else:

- **Use the tools, not Python.** Every step below is a single tool call, each
  wrapped in its own undo step. `execute_code` is for what no tool covers.
- **Never build geometry outside FreeCAD and import it.** A mesh or a STEP file
  has no sketches and no parameters; the user cannot edit it.

## The five calls

| Step | Tool | Notes |
|------|------|-------|
| 1 | `create_document(name)` | Returns the name FreeCAD *actually* used — it sanitises ("Bearing 608" → "Bearing_608"). Use the returned name from here on. |
| 2 | `create_varset(doc_name, ...)` | An `App::VarSet` holding every dimension. Do this before the geometry, not after. |
| 3 | `create_body(doc_name, name)` | A `PartDesign::Body`. Everything else lives inside it. |
| 4 | `create_sketch(doc_name, body_name, name, plane, offset)` | `plane` is one of the body's own origin planes: `XY_Plane`, `XZ_Plane`, `YZ_Plane`. `name` gets "Sketch" appended — ask for `Profile`, refer to `ProfileSketch` afterwards. |
| 5 | `sketch_circle` / `sketch_polygon` | Fully constrained geometry in sketch coordinates. |
| 6 | `pad` / `pocket` / `polar_pattern` | The solid. |

Then `check_sketch_geometry` and `check_shape` to confirm what you built, and
`get_active_screenshot` to look at it.

## Recipes worth knowing

**A hollow ring, tube, bushing or bearing race is ONE sketch and ONE pad.** Put
two concentric circles in the same sketch and pad it: FreeCAD pads the annulus
between them. Do not pad a disc and then pocket the bore — that is two features
and one more chance to get the direction wrong.

```
create_sketch(doc_name="Demo", body_name="Race", name="Profile", plane="XY_Plane")
sketch_circle(doc_name="Demo", sketch_name="ProfileSketch", radius=11)   # outer
sketch_circle(doc_name="Demo", sketch_name="ProfileSketch", radius=8)    # bore
pad(doc_name="Demo", body_name="Race", sketch_name="ProfileSketch", length=7)
```

**A symmetric pad** wants either `symmetric=true`, or a sketch at
`offset=-length/2`. Do not set `Placement` on an attached sketch — it is
overwritten on the next recompute. `offset` moves it along the plane normal via
`AttachmentOffset`, which survives.

**Pocket direction is the usual silent failure.** `pocket` defaults to
`reversed=true`, cutting *along* the sketch normal into material padded above
the plane. FreeCAD's own default is the opposite and, for the common "sketch on
XY, pad upward, now bore from the same plane" layout, removes nothing at all —
the recompute succeeds and the volume is unchanged. If a pocket "worked" but the
volume did not drop, this is why. `through_all` ignores the depth entirely.

**A ring of holes** is one pocket plus `polar_pattern`, not N pockets. The
pattern repeats a *feature*; pass what `pocket` returned, not the sketch. Its
axis comes from the feature's own profile, so it usually needs no axis argument.

**Balls, pins and rollers** are `create_sphere` and `create_cylinder` — solid
features inside the body, positioned by `x`/`y`/`z`. A ring of balls is one
sphere at `PitchDia/2` plus a polar pattern around Z.

## Parameters: what makes it parametric

Dimensions belong in a VarSet, and features reference them by expression. Then
changing the bore in one place rebuilds the model.

- `create_varset(doc_name, name, variables)` creates it and sets the values in
  one step.
- Numbers become `App::PropertyLength`, so `8` means 8 mm and arithmetic keeps
  its units. Pass an explicit type for counts (`App::PropertyInteger`) and
  angles (`App::PropertyAngle`).
- A VarSet asked for a name already taken becomes `Vars001`, and an expression
  naming `Vars` then reads the *other* one. Use the name the tool hands back.

## When you do need execute_code

Some things have no tool: a loft, a boolean between two bodies, a spline. Then
`execute_code` runs Python **inside the user's live FreeCAD** — no sandbox, no
confirmation. Use it for CAD, never for the filesystem or the network.

The workbench's helpers are already in that namespace. Use them instead of
hand-writing FreeCAD's C++ bindings, and never import OpenCASCADE directly:

| Helper | Signature |
|--------|-----------|
| `readyPart` | `readyPart(doc, name, recreate=False)` → the Body, created if absent |
| `originPlane` | `originPlane(body, role="XY_Plane")` |
| `createSketch` | `createSketch(body, name="", plane="XY_Plane", offset=0.0)` |
| `addCircle` | `addCircle(sketch, radius, x=0.0, y=0.0)` → geometry index |
| `addPolygon` | `addPolygon(sketch, points, closed=True, constrain=True)` |
| `constrainPoint` | `constrainPoint(sketch, geo, point, x, y)` |
| `createPad` | `createPad(body, sketch, height, name="", symmetric=False)` |
| `createPocket` | `createPocket(body, sketch, height, name="", reversed_=True, through_all=False)` |
| `createSphere` | `createSphere(body, radius, name="Sphere", x=0, y=0, z=0)` |
| `createCylinder` | `createCylinder(body, radius, height, name="Cylinder", x=0, y=0, z=0)` |
| `createPolar` | `createPolar(body, feature, count, angle=360, sketch=None, name="")` |
| `readyVars` | `readyVars(doc, name="Vars")` → the VarSet |
| `addVar` | `addVar(varset, name, value, ...)` → the expression path, e.g. `"Vars.Bore"` |
| `bindExpression` | `bindExpression(obj, prop, expression)` → the value it evaluates to |
| `fcVec`, `toPolar`, `toCart`, `clamp`, `vectorDistance`, `makeBspline` | geometry odds and ends |

`snake_case` aliases exist for all of them (`create_pad`, `add_var`, …), as do
`FreeCAD`/`App`, `Part`, `Sketcher`, `Quantity` and `Units`.

Each helper is create-or-set, so re-running a script is idempotent — where
`addProperty` on its own would raise "already exists".

```python
doc = App.getDocument("Demo")
vars = readyVars(doc)
bore = addVar(vars, "Bore", 8)                 # "Vars.Bore"
body = readyPart(doc, "Race")
sk = createSketch(body, "Profile")
addCircle(sk, 11); addCircle(sk, 8)
pad = createPad(body, sk, 7)
bindExpression(pad, "Length", "Vars.Width")    # now driven by the VarSet
doc.recompute()
```

## Before you say you are done

- `check_sketch_geometry` — an under-constrained sketch is a model that moves
  when the user drags it.
- `check_shape` — a feature can recompute "clean" and still leave the body with
  no usable solid.
- `get_log` — recompute complaints and OCC failures land in the Report view and
  nowhere else, including for steps that reported success.
- `get_active_screenshot` — look at what you built before describing it.
