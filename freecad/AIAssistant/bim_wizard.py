"""The BIM Wizard: lay out a floorplan wall by wall, with no AI involved.

Its own dialog and its own workbench command, deliberately separate from the
chat panel. Nothing here talks to a provider or needs an API key — it drives the
same tool registry directly — so hanging it off the AI panel only made it look
like it depended on one.
"""

# FreeCAD's own ``PySide`` alias, not a hardcoded binding name — see the note
# in ai_panel.py's imports for why "PySide2 import succeeds" isn't enough.
from PySide.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QGroupBox, QLabel, QLineEdit,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from .rpc_server.tools import call_tool


class BimWizardDialog(QDialog):
    """Lay out a floorplan wall by wall, with no AI involved.

    Each wall is added to a single Sketcher sketch as it is entered, so the
    result is the same fully-constrained baseline the AI path produces and the
    two can be mixed freely.

    Directions follow FreeCAD's plan view, which is also what the tool
    descriptions tell the model: east is +X and north is +Y. (The previous
    version of this table had east as -X and north as -Y, which mirrored every
    plan it drew.)
    """

    _DIR_LABELS = ["East (+X)", "West (-X)", "North (+Y)", "South (-Y)", "Custom (dX, dY)"]
    _DIR_DELTAS = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    _CUSTOM = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self._doc_name = None
        self._sketch_name = None
        self._walls = []          # (name, x1, y1, x2, y2) in mm
        self._points = {}         # combo label -> (x, y) in mm

        self.setWindowTitle("BIM Wizard")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.addWidget(self._build_setup_box())
        layout.addWidget(self._build_wall_box())
        layout.addWidget(self._build_build_box())

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)

        self._add_point("Origin (0, 0)", 0.0, 0.0)

    # -- construction ------------------------------------------------------

    def _build_setup_box(self) -> QWidget:
        box = QGroupBox("Step 1 — Project")
        form = QFormLayout(box)
        self._project = QLineEdit("BIMProject")
        form.addRow("Project name:", self._project)
        self._units = QComboBox()
        self._units.addItems(["Millimetres", "Inches"])
        form.addRow("Units:", self._units)
        self._start_btn = QPushButton("Create Document && Sketch")   # && so Qt shows a literal ampersand
        self._start_btn.clicked.connect(self._on_start)
        form.addRow("", self._start_btn)
        return box

    def _build_wall_box(self) -> QWidget:
        self._wall_box = QGroupBox("Step 2 — Walls")
        self._wall_box.setEnabled(False)
        form = QFormLayout(self._wall_box)

        self._wall_name = QLineEdit()
        self._wall_name.setPlaceholderText("e.g. SouthWall")
        form.addRow("Wall name:", self._wall_name)

        self._from = QComboBox()
        self._from.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        form.addRow("Start from:", self._from)

        self._direction = QComboBox()
        self._direction.addItems(self._DIR_LABELS)
        self._direction.currentIndexChanged.connect(self._on_direction_changed)
        form.addRow("Direction:", self._direction)

        self._length = QLineEdit()
        self._length.setPlaceholderText("length")
        self._length_label = QLabel("Length:")
        form.addRow(self._length_label, self._length)

        self._dx, self._dy = QLineEdit("0"), QLineEdit("0")
        self._dx_label, self._dy_label = QLabel("dX:"), QLabel("dY:")
        form.addRow(self._dx_label, self._dx)
        form.addRow(self._dy_label, self._dy)
        self._show_custom(False)

        add = QPushButton("Add Wall →")
        add.clicked.connect(self._on_add_wall)
        form.addRow("", add)

        self._wall_list = QLabel("No walls yet.")
        self._wall_list.setWordWrap(True)
        form.addRow("Added:", self._wall_list)
        return self._wall_box

    def _build_build_box(self) -> QWidget:
        self._build_box = QGroupBox("Step 3 — Build")
        self._build_box.setEnabled(False)
        form = QFormLayout(self._build_box)
        self._height = QLineEdit("2400")
        form.addRow("Wall height (mm):", self._height)
        self._thickness = QLineEdit("200")
        form.addRow("Thickness (mm):", self._thickness)
        note = QLabel("Doors and windows: ask in the chat panel once the walls exist.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#6c757d; font-style:italic;")
        form.addRow("", note)
        self._build_btn = QPushButton("Check Sketch && Build")       # (a single & is a mnemonic marker)
        self._build_btn.clicked.connect(self._on_build)
        form.addRow("", self._build_btn)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        form.addRow("", self._status)
        return self._build_box

    # -- helpers -----------------------------------------------------------

    def _to_mm(self, value: float) -> float:
        return value * 25.4 if self._units.currentText() == "Inches" else value

    def _set_status(self, message: str, error: bool = False):
        self._status.setText(message)
        self._status.setStyleSheet(f"color:{'#dc3545' if error else '#155724'};")

    def _call(self, tool, /, **arguments):
        """Registry call on the GUI thread — the dialog is already on it.

        ``tool`` is positional-only: several tools take an argument called
        ``name``, and before this marker ``_call("create_bim_document",
        name=...)`` raised "got multiple values for argument 'name'" — which is
        to say Step 1 could never create a document at all.
        """
        return call_tool(tool, arguments)

    def _add_point(self, label: str, x: float, y: float):
        self._points[label] = (x, y)
        self._from.addItem(label)

    def _show_custom(self, custom: bool):
        for w in (self._length_label, self._length):
            w.setVisible(not custom)
        for w in (self._dx_label, self._dx, self._dy_label, self._dy):
            w.setVisible(custom)

    def _on_direction_changed(self, index):
        self._show_custom(index == self._CUSTOM)

    # -- steps -------------------------------------------------------------

    def _on_start(self):
        name = self._project.text().strip() or "BIMProject"
        try:
            result = self._call("create_bim_document", name=name)
            self._doc_name = result["document_name"]
            self._sketch_name = f"{name}Plan"
            self._call(
                "execute_code",
                code=(
                    f"doc = App.getDocument({self._doc_name!r})\n"
                    f"doc.addObject('Sketcher::SketchObject', {self._sketch_name!r})\n"
                    "doc.recompute()"
                ),
            )
        except Exception as e:
            self._set_status(f"Could not start: {e}", error=True)
            return

        for w in (self._start_btn, self._project, self._units):
            w.setEnabled(False)
        self._wall_box.setEnabled(True)
        self._build_box.setEnabled(True)
        self._set_status(f"'{self._doc_name}' ready — add walls below.")

    def _on_add_wall(self):
        name = self._wall_name.text().strip()
        if not name:
            self._set_status("Give the wall a name.", error=True)
            return
        index = self._direction.currentIndex()
        try:
            if index == self._CUSTOM:
                dx = self._to_mm(float(self._dx.text()))
                dy = self._to_mm(float(self._dy.text()))
            else:
                length = self._to_mm(float(self._length.text()))
                sx, sy = self._DIR_DELTAS[index]
                dx, dy = sx * length, sy * length
        except ValueError:
            self._set_status("That is not a number.", error=True)
            return

        x1, y1 = self._points.get(self._from.currentText(), (0.0, 0.0))
        x2, y2 = x1 + dx, y1 + dy
        try:
            self._call("sketch_line", doc_name=self._doc_name,
                       sketch_name=self._sketch_name,
                       x1=x1, y1=y1, x2=x2, y2=y2, label=name)
        except Exception as e:
            self._set_status(f"Could not add '{name}': {e}", error=True)
            return

        self._walls.append((name, x1, y1, x2, y2))
        self._add_point(f"{name} start ({x1:.0f}, {y1:.0f})", x1, y1)
        end_label = f"{name} end ({x2:.0f}, {y2:.0f})"
        self._add_point(end_label, x2, y2)
        self._from.setCurrentIndex(self._from.findText(end_label))  # chain on

        self._wall_list.setText("\n".join(
            f"• {w[0]}: ({w[1]:.0f}, {w[2]:.0f}) → ({w[3]:.0f}, {w[4]:.0f})"
            for w in self._walls
        ))
        self._wall_name.clear()
        self._length.clear()
        self._set_status(f"'{name}' added — {len(self._walls)} wall(s).")

    def _on_build(self):
        if not self._walls:
            self._set_status("Add at least one wall first.", error=True)
            return
        try:
            info = self._call("sketch_info", doc_name=self._doc_name,
                              sketch_name=self._sketch_name)
        except Exception as e:
            self._set_status(f"Could not check the sketch: {e}", error=True)
            return
        if info["conflicting"]:
            self._set_status(
                f"Conflicting constraints at {info['conflicting']} — fix before building.",
                error=True,
            )
            return
        if not info["fully_constrained"]:
            self._set_status(f"Not fully constrained: {info['dof']} DoF remaining.", error=True)
            return

        try:
            height = float(self._height.text())
            thickness = float(self._thickness.text())
        except ValueError:
            self._set_status("Height and thickness must be numbers.", error=True)
            return

        try:
            result = self._call("add_wall_from_sketch", doc_name=self._doc_name,
                                sketch_name=self._sketch_name, height_mm=height,
                                width_mm=thickness, floor_label="Floor 1")
        except Exception as e:
            self._set_status(f"Could not build: {e}", error=True)
            return

        if result["invalid"]:
            self._set_status(f"Built, but these are invalid: {result['invalid']}", error=True)
            return
        self._set_status(f"Built {result['wall_label']} from {len(self._walls)} segments.")
        self._build_btn.setEnabled(False)
