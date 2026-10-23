---
name: freecad
description: Drive a running FreeCAD instance through the freecad MCP server — build parametric PartDesign solids from constrained sketches, script OCCT geometry, measure exact dimensions, run FEM analyses, and reach installed workbenches. Surveys the install first with list_workbenches, because a workbench (fasteners, gears, sheet metal, gridfinity, bearings, surfacing) usually does in one call what would otherwise be hours of hand-modelled geometry. Use when the user mentions FreeCAD, CAD modelling, parametric parts, assemblies, STEP/BREP/STL files, .FCStd documents, or asks to model, measure, modify, or stress-analyse a mechanical part.
---

# FreeCAD

This is the AI Assistant addon's own MCP server (`iplayfast/FreeCAD-AI-Assistant`),
driving a **live FreeCAD GUI** the user is watching. Documents are shared mutable
state: never assume an empty session, never assume you created what you are
editing, and say what you changed.

**Check which server you actually have first.** This one exposes ~51 typed tools
by name — `create_body`, `pad`, `sketch_polygon`, `list_workbenches`. If instead
you have `list_freecad_tools` / `call_freecad_tool`, that is a *different*
FreeCAD MCP server registered under the same name; call `list_freecad_tools`
before anything else to get its real catalog, and ignore the tool names below.

## Survey the install before building anything

```
list_workbenches()
```

Every workbench, built-in and addon, each with the commands it registers and —
for addons — its description and filesystem path. Cached after the first call;
`force=true` re-scans, which the user can also trigger from the panel's "Rescan
local workbenches" button. Pass nothing the rest of the session.

Hand-modelling an M5 cap screw when `fasteners` is installed wastes effort *and*
produces a part that is not to standard. If a listed addon covers the task, use
it. If more than one does (two gear addons is normal), say so and ask which.

## Pick the altitude, stopping at the first rung that fits

1. **A typed tool.** `create_body`, `create_sketch`, `sketch_circle`,
   `sketch_polygon`, `pad`, `pocket`, `polar_pattern`, `create_cylinder`,
   `create_sphere`, `create_varset`, `bind_expression`, `create_text_label`,
   `export_file`. Each call is one undo step the user can back out of, and the
   result stays editable in the tree.
2. **An installed workbench**, reached through `execute_code`. Addon features are
   Python-implemented, so `create_object` cannot see them. `list_workbenches`
   returns the addon's `path`; `read_file` it to learn the API, then drive it.
   The module name is *not* the workbench name — `CurvesWorkbench` imports as
   `freecad.Curves`, `SMWorkbench` as `SheetMetalCmd`. Never guess.
3. **`create_object`** for a C++ type id (`Part::Box`, `PartDesign::Pad`,
   `Draft::Circle`, `Fem::*`).
4. **Hand-written OCCT in `execute_code`** — last resort, not the first move,
   and never a way to model geometry elsewhere and import the result.

## The deliverable is parametric, not merely the right size

A document that happens to measure 40 mm is not the job; one the user can edit
afterwards is. So: `create_varset` with the dimensions up front, then
`bind_expression(obj_name, property_name='Length', expression='Vars.Width')` so
changing a variable rebuilds the model. Use the `expression_paths` that
`create_varset` returns verbatim — do not assemble a path by hand.

`bind_expression`'s argument is `property_name`, matching
`doc_name`/`obj_name`/`sketch_name`/`body_name` everywhere else.

## Sketches

- `sketch_polygon` once with the **whole** point list. Tracing a photo or
  rebuilding a freehand profile with one `sketch_line` per point burns the
  response budget on narration and leaves a half-built document if anything
  fails mid-way.
- Concentric circles in one sketch pad into a hollow tube or ring directly.
- `sketch_info` answers "why will this not pad": remaining degrees of freedom
  plus the indices of conflicting, redundant and malformed constraints.
  `auto_fix=true` drops the redundant ones and adds missing coincidences.
- Position sketches with `create_sketch(offset=…)`, never by writing
  `Placement` — `MapMode='FlatFace'` overwrites it on the next recompute and the
  pad then extrudes from the wrong origin.

## execute_code

Globals are already in scope: `FreeCAD`/`App`, `Gui`, `Part`, `Sketcher`,
`Quantity`, and the build helpers `readyPart`/`create_body`, `createSketch`,
`addCircle`, `createPad`, `createPocket`, `createSphere`, `createCylinder`,
`createPolar`, `readyVars`/`create_varset`, `addVar`. Use them; do not hand-write
raw C++ property bindings or OpenCASCADE imports.

It runs arbitrary Python in the user's live FreeCAD with no sandbox and no
confirmation. Use it for CAD work, not for touching the filesystem or network.

`execute_code_async` runs on a background thread for a long CPU-bound fuse or
loft. Documents and the Coin3D scenegraph are **not** thread-safe: do the heavy
math there, then hand every document write to `post_to_gui(fn)` (in scope).
Report progress with `FreeCAD.Console` and poll with `get_object`.

## Measuring

Prefer the typed tools — they come off the exact B-rep: `mass_properties`
(volume, area, centre of mass, bounding box, and mass with `density` in g/cm3 —
PLA 1.24, PETG 1.27, ABS 1.04), `measure_distance` (accepts subelements like
`Face3`, `Edge7`), `cross_section`, `check_interference`, `check_shape`
(`bop=true` for the slow self-intersection check).

If you read geometry by hand, read feature parameters (`obj.Radius`,
`obj.Height`) for anything curved rather than `Shape.BoundBox`. FreeCAD
tessellates before bounding, so a true r=7 cylinder reports 13.898 mm instead of
14.0 — a 0.7% error that propagates silently. BoundBox is fine for prismatic
shapes. `Shape.Volume`, `Shape.Area` and `Shape.CenterOfMass` are exact.

`doc.recompute()` before trusting any measurement you take in `execute_code`;
the typed builders already recompute.

## When you do not know the FreeCAD API

In this order: `get_skill` (bundled step-by-step guides — `bim`, `partdesign`),
`lookup_freecad_op` (fillet, chamfer, revolution, loft, sweep, boolean, Draft,
Assembly, TechDraw, FEM), `search_wiki` (the bundled wiki mirror's Description
and Scripting sections, Python examples included). All three are cheaper and more
reliable than guessing, and far cheaper than a failed `execute_code`.

`get_log` reads the Report view — the deprecation notices, recompute complaints
and OCC failures that never reach a tool's return value.

## Screenshots and state

`get_active_screenshot` is an explicit, separate call (`view_name`,
`focus_object`, `width`, `height`) and the only thing here that costs image
tokens — one at the end to confirm the result looks right, not per step.
`get_objects` / `get_object` / `get_dependency_graph` / `get_selection` are text
and cheap. `get_selection` is how "fillet THIS edge" becomes an argument.

## Housekeeping

Each tool call is one undo step, so prefer several small calls to one large
`execute_code`. `undo` reverts whole tool calls, including features worth
keeping — it reports the solid count before and after and warns when it destroyed
some, so read that warning rather than undoing again. To change one thing, use
`edit_object` or `delete_object` instead.

Ask before deleting or overwriting anything you did not create. Close probe
documents you opened (`execute_code` with `App.closeDocument(name)` — there is no
close tool) and leave the user's own documents alone. `save_document` saves in
place, or to `path` for a save-as.
