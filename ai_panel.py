"""The AI Assistant dock panel: a Qt face over the shared tool registry.

The panel owns no tools, no provider code and no settings of its own. It drives
``rpc_server.agent.AgentSession`` — the same agent loop the ``AI()`` console
function and the MCP server use — so a tool added to the registry appears here
with no change to this file.

Threading: the agent runs on a worker thread so the network wait does not freeze
FreeCAD, and every tool call is marshalled back onto the GUI thread via
``dispatch_to_gui``, which is where FreeCAD requires document access to happen.
"""

import json

try:
    from PySide2.QtWidgets import (
        QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
        QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
        QSizePolicy, QTextBrowser, QVBoxLayout, QWidget, QDockWidget,
    )
    from PySide2.QtCore import QEvent, QObject, Qt, QThread, Signal
    from PySide2.QtGui import QTextCursor
except ImportError:
    from PySide.QtGui import (
        QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
        QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
        QSizePolicy, QTextBrowser, QTextCursor, QVBoxLayout, QWidget,
        QDockWidget,
    )
    from PySide.QtCore import QEvent, QObject, Qt, QThread, Signal

from rpc_server.agent import AgentSession
from rpc_server.gui_dispatch import dispatch_to_gui, ensure_gui_pump
from rpc_server.providers import ProviderError, make_provider
from rpc_server.settings import load_settings, save_settings
from rpc_server.tools import call_tool

# The provider table and model lookup live with the setup wizard; sharing them
# keeps the panel and the AI() console offering the same services.
from rpc_server.console import _PRESETS, _fetch_models, _launch_cli, external_clis

_TOOL_TIMEOUT = 600   # a recompute or solver run is the slow part, not the queue

_panel_instance = None


def get_panel():
    global _panel_instance
    if _panel_instance is None:
        import FreeCADGui

        mw = FreeCADGui.getMainWindow()
        _panel_instance = AIChatPanel(mw)
        mw.addDockWidget(Qt.RightDockWidgetArea, _panel_instance)
    return _panel_instance


class _AgentWorker(QThread):
    """Drive one user turn on a worker thread, re-emitting the agent's events."""

    event = Signal(object)

    def __init__(self, session, prompt):
        super().__init__()
        self._session = session
        self._prompt = prompt

    def run(self):
        try:
            for event in self._session.run(self._prompt):
                self.event.emit(event)
        except Exception as e:
            # A worker dying silently is the worst outcome: the transcript would
            # simply stop with no indication anything went wrong.
            self.event.emit({"type": "error", "message": f"{type(e).__name__}: {e}"})


class _ModelLoader(QThread):
    """Fetch the model list off the GUI thread — an unreachable server hangs."""

    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, base_url, api_key):
        super().__init__()
        self._base_url = base_url
        self._api_key = api_key

    def run(self):
        try:
            models = _fetch_models(self._base_url, self._api_key)
        except Exception as e:
            self.failed.emit(str(e))
            return
        self.loaded.emit(models)


class _SendFilter(QObject):
    """Submit on Ctrl+Enter; plain Enter stays a newline."""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def eventFilter(self, obj, event):
        if (
            event.type() == QEvent.KeyPress
            and event.key() in (Qt.Key_Return, Qt.Key_Enter)
            and event.modifiers() & Qt.ControlModifier
        ):
            self._callback()
            return True
        return False


class AIChatPanel(QDockWidget):
    def __init__(self, parent=None):
        super().__init__("AI Assistant", parent)
        self.setObjectName("AIAssistantPanel")
        self.setMinimumWidth(320)

        self._session = None
        self._worker = None
        self._said_something = False

        root = QWidget()
        self.setWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        layout.addWidget(self._build_settings_box())

        self._chat = QTextBrowser()
        self._chat.setOpenExternalLinks(False)
        self._chat.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self._chat, stretch=1)

        layout.addWidget(self._build_input_box())
        self._refresh_settings_fields()

    # -- settings ----------------------------------------------------------

    def _build_settings_box(self) -> QWidget:
        box = QGroupBox("Settings")
        box.setCheckable(True)
        box.setChecked(False)
        form = QFormLayout(box)
        form.setContentsMargins(4, 4, 4, 4)

        self._service = QComboBox()
        # Services that answer in this panel first, then the CLIs that answer in
        # their own terminal. One dropdown because it is one decision: who is
        # doing the thinking for this session.
        self._service.addItems(list(_PRESETS))
        self._cli_choices = external_clis()
        self._service.insertSeparator(self._service.count())
        self._service.addItems(list(self._cli_choices))
        self._service.currentIndexChanged.connect(self._on_service_changed)
        form.addRow("Service:", self._service)

        self._cmd_label = QLabel("Command:")
        self._cmd = QLineEdit()
        self._cmd.setPlaceholderText("claude")
        self._cmd.setToolTip(
            "Run in a terminal, with a generated AGENT_BRIEFING.md describing "
            "this FreeCAD session, its RPC port and its tools"
        )
        form.addRow(self._cmd_label, self._cmd)

        self._url_label = QLabel("Base URL:")
        self._url = QLineEdit()
        self._url.setPlaceholderText("http://localhost:11434/v1")
        form.addRow(self._url_label, self._url)

        self._key_label = QLabel("API key:")
        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.Password)
        self._key.setPlaceholderText("blank = use $ANTHROPIC_API_KEY / $OPENAI_API_KEY")
        form.addRow(self._key_label, self._key)

        self._model = QComboBox()
        self._model.setEditable(True)
        self._model.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        load_btn = QPushButton("Load")
        load_btn.setToolTip("List the models this service offers")
        load_btn.clicked.connect(self._on_load_models)
        self._load_btn = load_btn
        form.addRow("Model:", self._row(self._model, load_btn))

        self._save_btn = QPushButton("Save")
        self._save_btn.clicked.connect(self._on_save_settings)
        form.addRow("", self._save_btn)
        return box

    @staticmethod
    def _row(*widgets) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        for w in widgets:
            row.addWidget(w)
        return holder

    def _refresh_settings_fields(self):
        """Load the saved settings into the form, matching them to a preset."""
        s = load_settings()
        name = next(
            (n for n, p in _PRESETS.items()
             if p["provider"] == s.get("ai_provider")
             and p.get("base_url", "") == s.get("ai_base_url", "")),
            list(_PRESETS)[0],
        )
        self._service.setCurrentIndex(max(0, self._service.findText(name)))
        self._url.setText(s.get("ai_base_url", ""))
        self._key.setText(s.get("ai_api_key", ""))
        model = s.get("ai_model", "")
        if model and self._model.findText(model) < 0:
            self._model.addItem(model)
        self._model.setCurrentText(model)
        self._on_service_changed()

    def _current_cli(self):
        """The external CLI selected in Service, or None for the built-in agent."""
        return self._cli_choices.get(self._service.currentText())

    def _on_service_changed(self, *_):
        cli = self._current_cli()
        for w in (self._cmd_label, self._cmd):
            w.setVisible(cli is not None)
        if cli is not None:
            # An external CLI brings its own credentials, model and loop; the
            # only thing this panel decides is what to run.
            for w in (self._url_label, self._url, self._key_label, self._key):
                w.setVisible(False)
            self._model.setEnabled(False)
            self._load_btn.setEnabled(False)
            self._save_btn.setText("Launch")
            if cli["cmd"]:
                self._cmd.setText(cli["cmd"])
            return

        self._model.setEnabled(True)
        self._load_btn.setEnabled(True)
        self._save_btn.setText("Save")
        preset = _PRESETS[self._service.currentText()]
        is_compat = preset["provider"] == "openai_compat"
        # A preset with a fixed base URL owns it; only "Other" is worth editing.
        editable_url = is_compat and not preset["base_url"]
        for w in (self._url_label, self._url):
            w.setVisible(editable_url)
        needs_key = preset.get("key_from") is not None
        for w in (self._key_label, self._key):
            w.setVisible(needs_key)
        if preset["base_url"]:
            self._url.setText(preset["base_url"])

    def _settings_from_form(self) -> dict:
        preset = _PRESETS[self._service.currentText()]
        return {
            "ai_provider": preset["provider"],
            "ai_base_url": preset["base_url"] or self._url.text().strip(),
            "ai_api_key": self._key.text().strip(),
            "ai_model": self._model.currentText().strip(),
        }

    def _on_save_settings(self):
        cli = self._current_cli()
        if cli is not None:
            self._launch_external(cli)
            return
        settings = load_settings()
        settings.update(self._settings_from_form())
        save_settings(settings)
        # The provider is built from settings, so a change must start a new
        # session rather than continue one bound to the old endpoint.
        self._session = None
        self._say_system("Settings saved.")

    def _launch_external(self, cli: dict):
        """Run the chosen CLI in a terminal, briefed on this FreeCAD session.

        The RPC server is started first: the briefing hands out a port, and a CLI
        that finds it dead has no way in and no way to say why.
        """
        cli = {**cli, "cmd": self._cmd.text().strip() or cli["cmd"]}
        if not cli["cmd"]:
            self._say_system("Type the command to run first.")
            return
        try:
            from rpc_server.rpc_server import start_rpc_server

            self._say_system(start_rpc_server())
        except Exception as e:
            self._say_system(f"Could not start the RPC server: {e}")
        ok, message = _launch_cli(cli)
        self._say_system(message if ok else f"Launch failed: {message}")

    def _on_load_models(self):
        form = self._settings_from_form()
        preset = _PRESETS[self._service.currentText()]
        if preset.get("models"):
            self._on_models_loaded(preset["models"])
            return
        self._load_btn.setEnabled(False)
        self._load_btn.setText("…")
        self._loader = _ModelLoader(form["ai_base_url"], form["ai_api_key"])
        self._loader.loaded.connect(self._on_models_loaded)
        self._loader.failed.connect(self._on_models_failed)
        self._loader.start()

    def _on_models_loaded(self, models):
        self._load_btn.setEnabled(True)
        self._load_btn.setText("Load")
        current = self._model.currentText()
        self._model.clear()
        self._model.addItems(models)
        self._model.setCurrentText(current if current in models else (models[0] if models else ""))
        self._say_system(f"{len(models)} model(s) available.")

    def _on_models_failed(self, message):
        self._load_btn.setEnabled(True)
        self._load_btn.setText("Load")
        self._say_system(f"Could not list models: {message}")

    # -- input and the agent run -------------------------------------------

    def _build_input_box(self) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._wizard_btn = QPushButton("BIM Wizard…")
        self._wizard_btn.setToolTip("Lay out a floorplan wall by wall, without the AI")
        self._wizard_btn.clicked.connect(self._on_wizard)
        layout.addWidget(self._wizard_btn)

        self._input = QPlainTextEdit()
        self._input.setPlaceholderText("Ask FreeCAD anything… (Ctrl+Enter to send)")
        self._input.setMaximumHeight(80)
        self._filter = _SendFilter(self._on_send)
        self._input.installEventFilter(self._filter)
        layout.addWidget(self._input)

        self._send_btn = QPushButton("Send")
        self._send_btn.clicked.connect(self._on_send)
        clear_btn = QPushButton("Clear")
        clear_btn.setToolTip("Forget the conversation and start over")
        clear_btn.clicked.connect(self._on_clear)
        layout.addWidget(self._row(clear_btn, self._send_btn))
        return holder

    def _ensure_session(self):
        """One session per conversation, so context carries across sends."""
        if self._session is not None:
            return self._session
        settings = load_settings()
        provider = make_provider({
            "provider": settings.get("ai_provider", "anthropic"),
            "model": settings.get("ai_model") or None,
            "base_url": settings.get("ai_base_url", ""),
            "api_key": settings.get("ai_api_key", ""),
        })
        # Nothing drains the queue unless the pump is up, and the panel has to
        # work with the RPC server switched off.
        ensure_gui_pump()
        self._session = AgentSession(provider, run_tool=self._run_tool)
        return self._session

    def _run_tool(self, name: str, arguments: dict):
        return dispatch_to_gui(lambda: call_tool(name, arguments), timeout=_TOOL_TIMEOUT)

    def _on_send(self):
        text = self._input.toPlainText().strip()
        if not text:
            return
        if self._worker is not None and self._worker.isRunning():
            self._say_system("A run is already in progress — wait for it to finish.")
            return
        cli = self._current_cli()
        if cli is not None:
            # Otherwise this would quietly answer from whichever service was
            # saved last, which is not the one shown in the dropdown.
            self._say_system(
                f"{self._service.currentText()} runs in its own terminal — press "
                "Launch in Settings and type there. Pick a service above to chat here."
            )
            return

        try:
            session = self._ensure_session()
        except ProviderError as e:
            self._say_system(f"{e}  Set a provider and key in Settings above.")
            return

        self._input.clear()
        self._say_user(text)
        self._set_busy(True)
        self._said_something = False

        self._worker = _AgentWorker(session, text)
        self._worker.event.connect(self._on_event)
        self._worker.finished.connect(self._on_run_finished)
        self._worker.start()

    def _on_event(self, event):
        kind = event["type"]
        if kind == "assistant":
            if event["text"].strip():
                self._said_something = True
                self._say_assistant(event["text"])
        elif kind == "tool_call":
            self._say_html(
                f'<code style="color:#6f42c1">▶ {self._esc(event["name"])}'
                f'({self._esc(_short(event["arguments"]))})</code>'
            )
        elif kind == "tool_result":
            colour = "#dc3545" if event["is_error"] else "#6c757d"
            body = event["content"] if event["is_error"] else _short(event["result"], 300)
            self._say_html(
                f'<code style="color:{colour}">◀ {self._esc(body)}</code>'
            )
        elif kind == "error":
            self._say_system(f"Error: {event['message']}")
        elif kind == "done" and not self._said_something:
            # A clean finish and a stall look identical otherwise.
            self._say_system(f"(finished without a summary, stop={event['stop']})")

    def _on_run_finished(self):
        self._set_busy(False)
        self._input.setFocus()

    def _on_clear(self):
        self._session = None
        self._chat.clear()
        if self._worker is not None and self._worker.isRunning():
            # The thread is blocked in a network read; let it finish into a
            # session nothing points at any more rather than killing it.
            self._worker.event.disconnect(self._on_event)
            self._say_system("Cleared. The run in flight will be discarded.")
        self._set_busy(False)

    def _set_busy(self, busy: bool):
        self._input.setEnabled(not busy)
        self._send_btn.setEnabled(not busy)
        self._send_btn.setText("Working…" if busy else "Send")

    # -- transcript --------------------------------------------------------

    def _say_html(self, html: str):
        self._chat.append(f"<p>{html}</p>")
        bar = self._chat.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _say_user(self, text):
        self._say_html(f'<b style="color:#155724">You:</b> {self._esc(text)}')

    def _say_assistant(self, text):
        self._say_html(f'<b style="color:#2c5aa0">AI:</b> {self._esc(text)}')

    def _say_system(self, text):
        self._say_html(f'<i style="color:#6c757d">{self._esc(text)}</i>')

    @staticmethod
    def _esc(text) -> str:
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )

    def _on_wizard(self):
        _BimWizardDialog(self).exec_()


def _short(value, limit: int = 120) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


class _BimWizardDialog(QDialog):
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

    def __init__(self, panel, parent=None):
        super().__init__(parent or panel)
        self._panel = panel
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
        self._start_btn = QPushButton("Create Document & Sketch")
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
        self._build_btn = QPushButton("Check Sketch & Build")
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

    def _call(self, name: str, **arguments):
        """Registry call on the GUI thread — the dialog is already on it."""
        return call_tool(name, arguments)

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
