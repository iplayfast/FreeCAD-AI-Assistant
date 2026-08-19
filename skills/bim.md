# FreeCAD BIM & Arch Workbench Scripting Guide

This guide describes how to programmatically build BIM models in FreeCAD using Python.

## Quick-start: Built-in BIM helper tools

For common operations, use the dedicated tools instead of writing boilerplate code:

| Tool | What it does |
|------|-------------|
| `create_bim_document(name)` | New doc + Site → Building → Floor hierarchy |
| `add_wall_from_sketch(sketch_name, height_mm, width_mm, floor_label)` | Arch Wall from an existing sketch |
| `add_opening(wall_label, width_mm, height_mm, pos_x, pos_y, sill_mm, rotation_deg, label, is_door)` | Door or window cut into a wall |

Example workflow using helpers:
```
1. create_bim_document("MyProject")
2. [PLAN] Derive all wall coordinates — present table to user, resolve ambiguities
3. [CONFIRM] User approves the coordinate table (or clarifies)
4. execute_python — create sketch + add ALL lines in one call
5. check_sketch("FloorplanSketch")  ← must return fully_constrained=True
6. add_wall_from_sketch("FloorplanSketch", 2438, 152, "Floor 1")
7. add_opening(...)  ← repeat for each door/window
```

## Sketch construction strategy — plan first, execute once

### Phase 1: Calculate all coordinates BEFORE touching FreeCAD

Read the description or dimensions file and derive the absolute (x, y) start and end point
of every wall segment in millimetres. Present them to the user as a table:

```
Wall segments (all values in mm):
  LoftWall:            (0, 0)      → (0, -3263.9)    [south 128.5"]
  NorthWallBathroom:   (0, 0)      → (3810, 0)        [east 150"]
  SouthWall_seg1:      (0, -3263.9)→ (1524, -3263.9)  [east 60"]
  SouthWall_seg2:      (1524,-3263.9)→(1524,-2983.9)  [north 11"]
  ...
```

**If any coordinate is ambiguous or cannot be derived from the description, ask the user
before proceeding.** Do not guess. A single wrong coordinate is invisible until the sketch
looks wrong in FreeCAD.

Only move to Phase 2 once the user has confirmed (or corrected) the table.

### Phase 2: Create the sketch in one execute_python call

**MANDATORY: Do NOT call execute_python until the user has confirmed the coordinate
table from Phase 1. Present the table, then STOP AND WAIT for a reply.**

Once confirmed, create the sketch in a single `execute_python` call.
**Use pre-calculated literal mm floats directly in sketch_line() — do NOT use
Python variables with multi-word names.** Multi-word variable names with hyphens
are a Python syntax error (e.g. `LoftWall-Length` is parsed as subtraction).
Calculate values in comments, pass literals to sketch_line:

```python
# Pre-calculated mm values (IN = 25.4):
# LoftWall:          (0,0) → (0, -3263.9)   [128.5" south]
# NorthWallBathroom: (0,0) → (3810.0, 0)    [150" east]
# SouthWall_seg1:    (0,-3263.9) → (1524.0,-3263.9)  [60" east]
# SouthWall_seg2:    (1524.0,-3263.9) → (1524.0,-2984.5)  [11" north]
# SouthWall_seg3:    (1524.0,-2984.5) → (1765.3,-2984.5)  [9.5" east]

sketch = doc.addObject("Sketcher::SketchObject", "FloorplanSketch")

sketch_line(sketch, 0, 0, 0, -3263.9, label="LoftWall")
sketch_line(sketch, 0, 0, 3810.0, 0, label="NorthWallBathroom")
sketch_line(sketch, 0, -3263.9, 1524.0, -3263.9, label="SouthWall_seg1")
sketch_line(sketch, 1524.0, -3263.9, 1524.0, -2984.5, label="SouthWall_seg2")
sketch_line(sketch, 1524.0, -2984.5, 1765.3, -2984.5, label="SouthWall_seg3")
# ... all remaining segments ...

doc.recompute()
```

Coordinate system: east=+X, north=+Y, south=-Y, west=-X.

Then call `check_sketch` and `add_wall_from_sketch`.

**Never delete a sketch and start over.** If the sketch already exists and you need to
inspect its current state, call `get_sketch_geometry("FloorplanSketch")` first — it returns
every line with its label and coordinates so you can resume rather than restart.

## Core Imports & Document Setup
Always import `FreeCAD` (as `App`), `Arch`, and `Draft`. Check for an active document or create one if missing.

```python
import FreeCAD as App
import Arch
import Draft

# Get or create active document
doc = App.ActiveDocument or App.newDocument("BIMProject")
```

---

## 1. Structural Hierarchy
BIM documents follow an IFC hierarchy: Site -> Building -> Floor (represented by a BuildingPart in modern FreeCAD).

```python
# 1. Create a Site
site = Arch.makeSite(name="ProjectSite")
site.Label = "Project Site"

# 2. Create a Building
building = Arch.makeBuilding(name="OfficeBuilding")
building.Label = "Office Building"
site.addObject(building)

# 3. Create a Floor (using BuildingPart)
floor1 = Arch.makeBuildingPart(name="Floor_1")
floor1.Label = "Floor 1"
# Position floor at Z = 0
floor1.Placement = App.Placement(App.Vector(0, 0, 0), App.Rotation(0, 0, 0))
building.addObject(floor1)
```

---

## 2. Creating Walls (`Arch.makeWall`)
Walls can be created from a Sketcher Sketch (Highly Recommended for parametric layouts, allowing easy modifications via FreeCAD's GUI), a 2D baseline (Draft Line/Wire), or directly via dimensions.

### Method A: From a Sketcher Sketch (Recommended)

**Always use the `sketch_line` and `sketch_connect` helpers** — they are injected into
the execute_python namespace automatically and handle H/V constraints and signed
DistanceX/DistanceY for you. Never call `addGeometry` + `addConstraint` manually.

**sketch_line pins both endpoints to absolute (x, y) coordinates** — no sign
calculation required. A wall going south from (0,0) to (0,−3000) just passes
y2=−3000 and the constraint is applied correctly. This approach is derived from
the GearWorkBench util.py library which has been proven across many parametric models.

```python
# 1. Create the sketch (no Support/MapMode needed for BIM walls)
sketch = doc.addObject("Sketcher::SketchObject", "FloorplanSketch")

# 2. Add all wall baselines using sketch_line.
#    Both endpoints are pinned absolutely — no Coincident or H/V constraints needed.
i0 = sketch_line(sketch, 0, 0, 5000, 0,       label="NorthWall")  # → east
i1 = sketch_line(sketch, 5000, 0, 5000, -3000, label="EastWall")   # → south
i2 = sketch_line(sketch, 5000, -3000, 0, -3000, label="SouthWall") # → west
i3 = sketch_line(sketch, 0, -3000, 0, 0,        label="WestWall")  # → north

# 3. Optional: add Coincident constraints for explicit topology (good practice)
sketch_connect(sketch, i0, 2, i1, 1)
sketch_connect(sketch, i1, 2, i2, 1)
sketch_connect(sketch, i2, 2, i3, 1)
sketch_connect(sketch, i3, 2, i0, 1)

# 4. Recompute and verify before creating wall
doc.recompute()
# check_sketch("FloorplanSketch")  ← MUST return fully_constrained=True
# Redundant constraints from shared endpoints are expected — not a problem.
```

Other helpers available in execute_python:
```python
sketch_arc(sketch, cx, cy, diameter, start_rad, end_rad)  # → {index, start_point, end_point}
sketch_circle(sketch, cx, cy, diameter)                    # → index
sketch_polygon(sketch, [(x1,y1),(x2,y2),...], closed=True) # → [indices]
sketch_constrain_point(sketch, geo_idx, pt_idx, x, y)      # pin any point absolutely
```

### Method B: From Draft baselines (For simple single walls)
```python
# Create a baseline using Draft
p1 = App.Vector(0, 0, 0)
p2 = App.Vector(5000, 0, 0)
baseline = Draft.makeLine(p1, p2)

# Create the wall (baseobj, height, width, align)
wall = Arch.makeWall(baseline, height=3000, width=200, align="Center")
wall.Label = "SingleWall"

# Add the wall to the floor container
floor1.addObject(wall)
```

### Method C: Direct dimensions (Non-parametric)
```python
# Create wall directly without base geometry
wall2 = Arch.makeWall(None, length=4000, width=200, height=3000)
wall2.Label = "East Wall"
wall2.Placement = App.Placement(App.Vector(5000, 0, 0), App.Rotation(App.Vector(0, 0, 1), 90))

floor1.addObject(wall2)
```

---

## 3. Windows & Doors (`Arch.makeWindow`)
In FreeCAD, both windows and doors are constructed using the `Arch.makeWindow` tool. They require a closed 2D wire or sketch to define their profile.

### Step 1: Create a closed profile
```python
# Create 4 points for the window outline
w_p1 = App.Vector(0, 0, 0)
w_p2 = App.Vector(1200, 0, 0)
w_p3 = App.Vector(1200, 1500, 0)
w_p4 = App.Vector(0, 1500, 0)

# Create a closed Draft wire
profile = Draft.makeWire([w_p1, w_p2, w_p3, w_p4], closed=True)
```

### Step 2: Convert to Arch Window and place in a Wall
When you add a window to a wall using `Arch.addComponents()`, FreeCAD automatically performs a boolean subtraction to cut the opening in the wall.

```python
# Create the window
window = Arch.makeWindow(profile, name="Window_1")
window.Label = "Window"

# Position the window relative to the wall (placing it along the wall length)
# Set X offset along wall, Y offset (thickness offset), Z offset (sill height)
window.Placement = App.Placement(App.Vector(1500, 0, 1000), App.Rotation(0, 0, 0))

# Host the window inside the wall (cuts the wall opening)
Arch.addComponents(window, wall)
```

---

## 4. Beams & Columns (`Arch.makeStructure`)
Beams and structural columns are created using the `Arch.makeStructure` command.

```python
# Create a structural column (defined by base profile or length/width/height)
column = Arch.makeStructure(length=400, width=400, height=3000)
column.Label = "Structural Column"
column.Placement = App.Placement(App.Vector(2500, 2500, 0), App.Rotation(0, 0, 0))

floor1.addObject(column)
```

---

## 5. Adding Components & Groups
Always group objects inside their respective level container (`floor1`). You can use either:
*   `floor1.addObject(obj)`
*   `Arch.addComponents(obj, floor1)`

---

## 6. Document Update
After performing any operations, always call recompute:
```python
doc.recompute()
```
