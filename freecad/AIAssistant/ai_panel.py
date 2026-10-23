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
import re

# FreeCAD's own ``PySide`` alias, not a hardcoded binding name: it always
# matches whatever Qt binding FreeCADGui itself is built against (PySide2 or
# PySide6 depending on the build), so widgets it hands us — getMainWindow(),
# in particular — type-check correctly against classes imported from here.
# Importing ``PySide2`` directly can succeed (FreeCAD ships a compat module
# under that name) while still producing a class hierarchy shiboken refuses to
# accept a PySide6-wrapped main window against.
from PySide.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QSizePolicy, QTextBrowser, QVBoxLayout, QWidget, QDockWidget,
)
from PySide.QtCore import QEvent, QObject, Qt, QThread, Signal
from PySide.QtGui import QPalette, QTextCursor

from .rpc_server.agent import AgentSession, DEFAULT_SYSTEM, INCOMPLETE_STOPS, select_tools
from .rpc_server.gui_dispatch import dispatch_to_gui, ensure_gui_pump
from .rpc_server.providers import ProviderError, make_provider, set_wire_logger
from .rpc_server.settings import load_settings, save_settings
from .rpc_server.tools import PROFILES, call_tool, describe_tools

# The provider table and model lookup live with the setup wizard; sharing them
# keeps the panel and the AI() console offering the same services.
from .rpc_server.console import (
    _PERMISSION_LABELS,
    _PRESETS,
    _fetch_models,
    discover_local_servers,
    external_clis,
    launch_agent_cli,
    scan_clis,
)

_TOOL_TIMEOUT = 600   # a recompute or solver run is the slow part, not the queue

# One wire body, clipped. The tool schema alone is ~29,000 characters and
# repeats on every step, so an uncapped dump makes the transcript widget
# unusable long before it tells you anything the first copy did not.
_WIRE_LIMIT = 20000

# Not a PROFILES entry: it is not a fixed set of categories but an extra
# round trip that picks per request. Chosen from whichever profile is
# configured, so the two compose.
_AUTO = "Auto — pick per request"

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

    def __init__(self, session, prompt, select=False):
        super().__init__()
        self._session = session
        self._prompt = prompt
        self._select = select

    def run(self):
        try:
            if self._select:
                # Off the GUI thread, because it is another HTTP round trip.
                picked = select_tools(self._session.provider, self._prompt,
                                      self._session.all_tools)
                self._session.tools = picked
                self.event.emit({"type": "selected",
                                 "names": [t["name"] for t in picked]})
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


def _profile_tokens(profile: str) -> int:
    """Rough token cost of a profile's schema. ~4 chars per token.

    Crude, but the profiles are hundreds of tokens apart, so the estimate only
    has to land in the right gap.
    """
    return len(json.dumps(describe_tools(profile))) // 4


def _profile_that_fits(window: int) -> str:
    """The largest tool profile whose schema leaves room in ``window`` tokens.

    Budget is half the window, since the schema is resent on every step and the
    conversation and the tool results have to live in the other half.
    """
    if not window:
        return ""
    budget = window / 2
    fitting = [n for n in PROFILES if _profile_tokens(n) <= budget]
    return max(fitting, key=lambda n: len(describe_tools(n)), default="")


def _window_from_error(message: str) -> int:
    """The context size a server named while refusing, if it named one."""
    found = re.search(r"context size \((\d+)", message) or re.search(r'"n_ctx":\s*(\d+)', message)
    return int(found.group(1)) if found else 0


def _same_endpoint(url: str) -> str:
    """Key for "these two URLs are the same server".

    Discovery reports 127.0.0.1 while the Ollama preset is written as localhost,
    so comparing the strings listed the same server twice.
    """
    return (url or "").replace("//localhost:", "//127.0.0.1:").rstrip("/")


class _ServerScanner(QThread):
    """Find running local model servers off the GUI thread.

    A second or so of probing ports nobody is serving on is not something to do
    on the thread that paints the window.
    """

    found = Signal(object)

    def run(self):
        try:
            self.found.emit(discover_local_servers())
        except Exception:
            self.found.emit([])          # discovery is a convenience, never fatal


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
    # The wire tap fires on the worker thread doing the HTTP call. Touching a
    # widget from there is undefined; a queued signal is the supported way over.
    wire = Signal(object)

    def __init__(self, parent=None):
        super().__init__("AI Assistant", parent)
        self.setObjectName("AIAssistantPanel")
        self.setMinimumWidth(320)

        self._session = None
        self._worker = None
        self._said_something = False
        self.wire.connect(self._on_wire)

        root = QWidget()
        self.setWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        layout.addWidget(self._build_settings_box())

        self._chat = QTextBrowser()
        self._chat.setOpenExternalLinks(False)
        self._chat.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # The transcript is read-only, but by default it renders on the same
        # Base colour as the input box below it, so the two read as one field and
        # you end up typing into the transcript and wondering why nothing appears.
        # Painting it on Window recesses it the way every other read-only view in
        # FreeCAD is recessed. Palette roles, not literal colours, so this still
        # holds on a light theme.
        recessed = self._chat.palette()
        base = recessed.color(QPalette.Base)
        # Derived from Base rather than set to Window: on this theme Window and
        # Base are both near-black, so borrowing Window changed nothing visible.
        # Going lighter on a dark theme and darker on a light one always moves.
        recessed.setColor(
            QPalette.Base, base.lighter(180) if base.lightness() < 128 else base.darker(108)
        )
        self._chat.setPalette(recessed)
        layout.addWidget(self._chat, stretch=1)

        layout.addWidget(self._build_input_box())
        self._refresh_settings_fields()
        self._report_cli_scan()
        # Runs on a worker thread, so opening the panel is not held up by it.
        self._on_detect()

    # -- settings ----------------------------------------------------------

    def _build_settings_box(self) -> QWidget:
        box = QGroupBox("Settings")
        form = QFormLayout(box)
        form.setContentsMargins(4, 4, 4, 4)

        self._service = QComboBox()
        # Endpoints this panel talks to itself first, then the CLIs that answer
        # in their own terminal. One dropdown because it is one decision: who is
        # doing the thinking for this session.
        #
        # A vendor that ships a CLI is offered *as* that CLI and not also as a
        # keyed endpoint — Claude and Antigravity are the external CLI rows below, so
        # listing them twice would ask for an API key to reach a tool that is
        # already signed in. What is left here is what has no CLI to launch:
        # local and self-hosted servers.
        self._services = {n: p for n, p in _PRESETS.items() if not p.get("cli")}
        self._service.addItems(list(self._services))
        self._cli_choices = external_clis()
        self._service.insertSeparator(self._service.count())
        self._service.addItems(list(self._cli_choices))
        self._service.currentIndexChanged.connect(self._on_service_picked)
        self._detect_btn = QPushButton("Detect")
        self._detect_btn.setToolTip(
            "Look for local model servers running right now. Ports are not fixed "
            "— Unsloth's llama.cpp picks a new one each start — so this asks "
            "everything listening on loopback rather than guessing."
        )
        self._detect_btn.clicked.connect(self._on_detect)
        form.addRow("Service:", self._row(self._service, self._detect_btn))

        self._profile_label = QLabel("Tools:")
        self._profile = QComboBox()
        self._profile.addItems(list(PROFILES) + [_AUTO])
        self._profile.setToolTip(
            "How much of the tool registry to describe to the model. The full "
            "set is resent on every step, so a narrower profile is what makes "
            "a small local context window usable."
        )
        form.addRow(self._profile_label, self._profile)

        self._verbose = QCheckBox("Show the JSON sent to and received from the model")
        self._verbose.setToolTip(
            "Every request and reply, as JSON, in the transcript. Bodies over "
            f"{_WIRE_LIMIT} characters are clipped."
        )
        self._verbose.toggled.connect(self._on_verbose_toggled)
        form.addRow("Verbose:", self._verbose)

        self._cmd_label = QLabel("Command:")
        self._cmd = QLineEdit()
        self._cmd.setPlaceholderText("claude")
        self._cmd.setToolTip(
            "Run in a terminal, with a generated AGENT_BRIEFING.md describing "
            "this FreeCAD session, its RPC port and its tools"
        )
        form.addRow(self._cmd_label, self._cmd)

        self._perms_label = QLabel("Asks before:")
        self._perms = QComboBox()
        for mode, label in _PERMISSION_LABELS.items():
            self._perms.addItem(label, mode)
        self._perms.setToolTip(
            "How much a launched CLI may do without stopping to ask. Driving "
            "FreeCAD is dozens of tool calls, and confirming 'pad' forty times "
            "is how people learn to confirm without reading — but execute_code "
            "runs unsandboxed Python inside FreeCAD and can do anything to this "
            "machine, so the middle option keeps that one behind a prompt."
        )
        form.addRow(self._perms_label, self._perms)

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
        # Kept as handles so the row can be hidden outright, not merely greyed
        # out, for services that pick their own model.
        self._model_label = QLabel("Model:")
        self._model_row = self._row(self._model, load_btn)
        form.addRow(self._model_label, self._model_row)

        self._rescan_btn = QPushButton("Rescan local workbenches")
        self._rescan_btn.setToolTip(
            "Cycle every installed workbench once to find what commands it "
            "offers (e.g. which addon has a Bevel Gear) and cache the result "
            "to disk, so the agent does not have to read addon source to "
            "find what is already installed. Runs automatically the first "
            "time; press this again after installing or updating an addon."
        )
        self._rescan_btn.clicked.connect(self._on_rescan_workbenches)
        form.addRow("", self._rescan_btn)

        self._skill_btn = QPushButton("Install agent skill")
        self._skill_btn.setToolTip(
            "Make this addon's FreeCAD skill available to every agent CLI "
            "installed here (Claude Code, Codex, Antigravity/agy) by linking "
            "it into their skills directory, so a session started anywhere "
            "knows how to drive this server. A CLI launched from this panel "
            "already finds it without this, because it starts in the addon "
            "directory. Linked, not copied — editing the addon's SKILL.md "
            "updates all of them."
        )
        self._skill_btn.clicked.connect(self._on_install_skill)
        form.addRow("", self._skill_btn)

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
        name = self._service_matching(s) or list(self._services)[0]
        self._service.blockSignals(True)
        self._service.setCurrentIndex(max(0, self._service.findText(name)))
        self._service.blockSignals(False)
        self._url.setText(s.get("ai_base_url", ""))
        self._key.setText(s.get("ai_api_key", ""))
        model = s.get("ai_model", "")
        if model and self._model.findText(model) < 0:
            self._model.addItem(model)
        self._model.setCurrentText(model)
        wanted = s.get("ai_tool_profile", "Everything")
        self._profile.setCurrentIndex(max(0, self._profile.findText(wanted)))
        mode = s.get("ai_cli_permissions", "freecad")
        self._perms.setCurrentIndex(max(0, self._perms.findData(mode)))
        self._on_service_changed()

    def _service_matching(self, settings):
        """The dropdown entry that corresponds to these saved settings, if any."""
        return next(
            (n for n, p in self._services.items()
             if p["provider"] == settings.get("ai_provider")
             and _same_endpoint(p.get("base_url")) == _same_endpoint(settings.get("ai_base_url"))),
            None,
        )

    def _select_saved_service(self):
        """Re-resolve the saved service once discovery has added its entry.

        _refresh_settings_fields runs in __init__, before the scan has finished,
        so a saved server that is only found by scanning matches nothing and the
        form falls back to the first entry in the list. The dropdown then said
        Ollama while the settings said llama-server, and saving in that state
        wrote Ollama's URL against the other one's model - which surfaced much
        later as the endpoint reporting a model it had never heard of.
        """
        settings = load_settings()
        name = self._service_matching(settings)
        if name is None:
            saved_url = settings.get("ai_base_url", "")
            if saved_url and not self._current_cli():
                self._say_system(
                    f"The saved service at {saved_url} is not running, so "
                    f"{self._service.currentText()!r} is selected instead. Pick "
                    "a service and press Save."
                )
            return
        if self._service.currentText() == name:
            return
        index = self._service.findText(name)
        if index < 0:
            return
        # Not a user pick: keep the saved model rather than clearing it.
        self._service.blockSignals(True)
        self._service.setCurrentIndex(index)
        self._service.blockSignals(False)
        self._on_service_changed()
        self._say_system(f"Selected the saved service, {name}.")

    def _on_service_picked(self, *_):
        """A user changed Service, as opposed to settings being restored.

        The model list belongs to the service that produced it. Leaving it in
        place across a change let a model from one endpoint be saved against
        another, which fails only later, at request time, as the endpoint
        reporting a model it has never heard of.
        """
        self._model.clear()
        preset = self._services.get(self._service.currentText())
        if preset and preset.get("models"):
            self._model.addItems(preset["models"])
            self._model.setCurrentText(preset["models"][0])
        else:
            self._model.setCurrentText("")
        self._on_service_changed()

    def _current_cli(self):
        """The external CLI selected in Service, or None for the built-in agent."""
        return self._cli_choices.get(self._service.currentText())

    def _on_service_changed(self, *_):
        cli = self._current_cli()
        for w in (self._cmd_label, self._cmd, self._perms_label, self._perms):
            w.setVisible(cli is not None)
        if cli is not None:
            # An external CLI is already configured and signed in: it brings its
            # own credentials, its own model choice and its own loop. Asking for
            # any of that here would imply this panel supplies it, so hide the
            # rows rather than grey them out. The only decision left is what to run.
            for w in (self._url_label, self._url, self._key_label, self._key,
                      self._model_label, self._model_row,
                      self._profile_label, self._profile):
                w.setVisible(False)
            self._save_btn.setText("Launch")
            if cli["cmd"]:
                self._cmd.setText(cli["cmd"])
            return

        for w in (self._model_label, self._model_row,
                  self._profile_label, self._profile):
            w.setVisible(True)
        self._save_btn.setText("Save")
        preset = self._services[self._service.currentText()]
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
        preset = self._services[self._service.currentText()]
        return {
            "ai_provider": preset["provider"],
            "ai_base_url": preset["base_url"] or self._url.text().strip(),
            "ai_api_key": self._key.text().strip(),
            "ai_model": self._model.currentText().strip(),
            "ai_tool_profile": self._profile.currentText(),
        }

    def _on_rescan_workbenches(self):
        """Cycle every workbench once to find its commands, then cache the result.

        Synchronous and on the GUI thread, not a worker thread: activating a
        workbench touches toolbars and Coin3D state that only the GUI thread
        may own. processEvents() flushes the "Scanning..." message first since
        the sweep itself (tens of workbenches) then blocks for a few seconds.
        """
        from PySide.QtWidgets import QApplication

        self._rescan_btn.setEnabled(False)
        self._say_system("Scanning installed workbenches for their commands…")
        QApplication.processEvents()
        try:
            workbenches = call_tool("list_workbenches", {"force": True})
        except Exception as e:
            self._say_system(f"Workbench scan failed: {e}")
        else:
            addons = sum(1 for w in workbenches if w["kind"] == "addon")
            commands = sum(len(w["commands"]) for w in workbenches)
            self._say_system(
                f"Found {len(workbenches)} workbenches ({addons} addons), "
                f"{commands} commands between them. Cached for next time — "
                "press this again after installing or updating an addon."
            )
        finally:
            self._rescan_btn.setEnabled(True)

    def _on_install_skill(self):
        """Link the shipped skill into every agent CLI's skills directory."""
        from .rpc_server.console import install_skill

        try:
            results = install_skill()
        except Exception as e:
            self._say_system(f"Installing the skill failed: {e}")
            return
        # Report every agent, including the ones skipped: "nothing happened for
        # Codex" is the answer to "why does my Codex session not see it", and a
        # button that silently does less than its label says is worse than one
        # that says so.
        lines = [f"  {r['agent']}: {r['status']}"
                 + (f" → {r['path']}" if r["status"] in ("linked", "copied") else "")
                 for r in results]
        self._say_system("FreeCAD skill install:\n" + "\n".join(lines))

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

    def _on_verbose_toggled(self, checked: bool):
        """Install or clear the provider wire tap.

        The tap is a single global in providers.base, shared with the console's
        AI_verbose(), so the last one to set it wins. That is fine for a debug
        toggle and beats two parallel logging paths for the same bytes.
        """
        set_wire_logger((lambda d, u, b: self.wire.emit((d, u, b))) if checked else None)
        self._say_system(
            "Verbose on: every request and reply will be shown as JSON."
            if checked else "Verbose off."
        )

    def _on_wire(self, payload):
        """Render one side of a provider exchange. Runs on the GUI thread."""
        direction, url, body = payload
        arrow = {"request": "→", "response": "←", "error": "✗"}.get(direction, "·")
        try:
            text = json.dumps(body, indent=2, default=str)
        except Exception:
            text = str(body)
        if len(text) > _WIRE_LIMIT:
            text = f"{text[:_WIRE_LIMIT]}\n… {len(text) - _WIRE_LIMIT} more characters"
        self._say_html(
            f'<b style="color:#6c757d">[wire {arrow}] {self._esc(url)}</b>'
            f'<pre style="white-space:pre-wrap; font-size:small">{self._esc(text)}</pre>'
        )

    def _on_detect(self):
        self._detect_btn.setEnabled(False)
        self._scanner = _ServerScanner()
        self._scanner.found.connect(self._on_servers_found)
        self._scanner.start()

    def _on_servers_found(self, servers):
        """Add anything discovered to the local half of the dropdown.

        Discovered servers are shaped like presets, so selecting one goes through
        exactly the same path as Ollama — no second code path for "found" versus
        "configured". Their model list comes from the probe, so Load needs no
        network call for them.
        """
        self._detect_btn.setEnabled(True)
        known = {_same_endpoint(p.get("base_url")) for p in self._services.values()}
        added = []
        for server in servers:
            if _same_endpoint(server["base_url"]) in known:
                continue                  # already offered, e.g. Ollama's preset
            needs_key = server.get("needs_key")
            label = f"{server['name'] or 'local server'} :{server['port']}"
            if needs_key:
                label += " (needs key)"
            elif server.get("n_ctx"):
                label += f" ({server['n_ctx']} ctx)"
            index = len(self._services)   # the separator sits here; push it down
            self._services[label] = {
                "provider": "openai_compat",
                "base_url": server["base_url"],
                # None hides the key row; "" shows it and asks for one. A local
                # server that answered 401 is the second kind.
                "key_from": "" if needs_key else None,
                "models": server["models"],
                # Distinguishes "answered 401" from "Other", which also shows a
                # key row but is perfectly usable without one.
                "needs_key": bool(needs_key),
                # Per-request window, where the server reports one. Lets the
                # profile be checked before a request rather than after a refusal.
                "n_ctx": server.get("n_ctx"),
                "slots": server.get("slots"),
            }
            self._service.insertItem(index, label)
            if needs_key:
                added.append(f"{label} — needs an API key, then press Load")
            else:
                note = f"{label} — {len(server['models'])} model(s)"
                if server.get("slots", 1) and (server.get("slots") or 1) > 1:
                    note += (f", {server['n_ctx']} tokens per request across "
                             f"{server['slots']} slots")
                added.append(note)
        if added:
            self._say_system("Local model servers found: " + "; ".join(added) + ".")
        else:
            self._say_system(
                "No additional local model servers are listening." if servers
                else "No local model servers are listening."
            )
        # Only now can a saved server that exists solely on a scanned port be
        # matched to a dropdown entry.
        self._select_saved_service()

    def _report_cli_scan(self):
        """Show the PATH scan behind the external-CLI rows in the dropdown.

        Without this the dropdown just silently omits what it could not find, and
        "not installed" is indistinguishable from "FreeCAD was launched from a
        desktop icon and never got your shell PATH" — which is the more likely
        cause of the two, and the one with a fix.
        """
        rows = scan_clis()
        found = [c for c in rows if c["path"]]
        listed = "".join(
            f'  {c["cmd"]:<12}{"✓ " + self._esc(c["path"]) if c["path"] else "✗ not on PATH"}<br>'
            for c in rows
        )
        self._say_html(
            f'<i style="color:#6c757d">Agent CLIs found on PATH '
            f'({len(found)}/{len(rows)}):</i><br><code>{listed}</code>'
        )
        if len(found) < len(rows):
            self._say_system(
                "A CLI you have installed but that shows ✗ usually means FreeCAD "
                "was started from a desktop launcher, which does not pass on your "
                "shell PATH — start it from a terminal, or use the Shell command "
                "row with a full path."
            )

    def _launch_external(self, cli: dict):
        """Run the chosen CLI in a terminal, briefed on this FreeCAD session.

        The RPC server is started first: the briefing hands out a port, and a CLI
        that finds it dead has no way in and no way to say why.
        """
        cli = {**cli, "cmd": self._cmd.text().strip() or cli["cmd"]}
        if not cli["cmd"]:
            self._say_system("Type the command to run first.")
            return
        # Launch is this row's Save: with a CLI selected the button never reaches
        # the settings path below, so the permission choice would be read from
        # disk as whatever it was before the user changed the dropdown.
        settings = load_settings()
        settings["ai_cli_permissions"] = self._perms.currentData()
        save_settings(settings)
        ok, message = launch_agent_cli(cli)
        self._say_system(message if ok else f"Launch failed: {message}")

    def _on_load_models(self):
        form = self._settings_from_form()
        name = self._service.currentText()
        preset = self._services[name]
        if preset.get("models"):
            self._on_models_loaded(preset["models"])
            return
        # This server already told us it wants a credential. Asking it again
        # without one just returns the same 401 with its body spelled out in the
        # log, which reads as a fault rather than as "you have not filled this in".
        if preset.get("needs_key") and not form["ai_api_key"]:
            self._say_system(
                f"{name} refused the model list without a credential. Paste its "
                "API key in the API key box above and press Load again."
            )
            return
        self._load_btn.setEnabled(False)
        self._load_btn.setText("…")
        # A network round trip to a server that may be paging a model in, or may
        # be down and heading for a timeout. A greyed-out button does not say
        # that anything is happening, let alone what or where.
        self._say_system(
            f"Searching {self._service.currentText()} for available models at "
            f"{form['ai_base_url'] or 'the provider default'} — this can take a "
            "few seconds."
        )
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
        if not models and self._services[self._service.currentText()].get("needs_key"):
            # _fetch_models turns any failure into an empty list, so a rejected
            # key and a server with nothing loaded look identical from here. For
            # one that authenticates, the key is much the likelier of the two.
            self._say_system("No models came back — the API key was probably rejected.")
            return
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

        # A titled frame, because the transcript above and this box render on the
        # same colour under most themes and are otherwise two identical dark
        # rectangles — which is how you end up typing into the transcript.
        # A border is a cue that survives any palette; a shade is not.
        ask = QGroupBox("Your message")
        ask_layout = QVBoxLayout(ask)
        ask_layout.setContentsMargins(4, 4, 4, 4)

        self._input = QPlainTextEdit()
        self._input.setPlaceholderText("Ask FreeCAD anything… (Ctrl+Enter to send)")
        self._input.setMaximumHeight(80)
        self._filter = _SendFilter(self._on_send)
        self._input.installEventFilter(self._filter)
        ask_layout.addWidget(self._input)
        layout.addWidget(ask)

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
            "timeout": settings.get("ai_timeout"),
        })
        # Nothing drains the queue unless the pump is up, and the panel has to
        # work with the RPC server switched off.
        ensure_gui_pump()
        profile = settings.get("ai_tool_profile")
        self._auto = profile == _AUTO
        if self._auto:
            profile = "Everything"
        # Discovery asked the server how big its window is, so a request that
        # cannot fit is knowable here rather than by being refused. Sending it
        # anyway to honour the setting means a guaranteed failure, and saying so
        # and sending it anyway is worse — it fails while explaining that it will.
        # Narrow it for this session instead, and leave the saved setting alone.
        window = (self._services.get(self._service.currentText()) or {}).get("n_ctx")
        chosen, note = profile, ""
        if window and _profile_tokens(profile) > window / 2:
            fits = _profile_that_fits(window)
            if fits:
                chosen = fits
                note = (
                    f"{profile!r} is ~{_profile_tokens(profile)} tokens and this server "
                    f"gives {window} per request, so this session uses {fits!r} "
                    f"(~{_profile_tokens(fits)}). Set Tools to {fits!r} to keep it, or "
                    "give the server a larger context — note --parallel N splits the "
                    "window between N slots."
                )
            else:
                note = (
                    f"Every profile is larger than this server's {window}-token window. "
                    "Give it a bigger context (llama.cpp's -c, divided by --parallel N)."
                )

        tools = describe_tools(chosen)
        # Cached if a scan already ran; scans fresh (slow, one-time) otherwise -
        # either way the model starts already knowing what's installed, instead
        # of having to call list_workbenches itself before building.
        from .rpc_server.tool_handlers import format_addon_summary
        addon_summary = format_addon_summary(call_tool("list_workbenches", {}))
        system = f"{DEFAULT_SYSTEM}\n\n{addon_summary}" if addon_summary else DEFAULT_SYSTEM
        self._session = AgentSession(provider, run_tool=self._run_tool, tools=tools,
                                     system=system,
                                     max_steps=int(settings.get("ai_max_steps") or 25))
        head = f"Describing {len(tools)} of {len(describe_tools())} tools to the model ({chosen})"
        if window:
            head += f", ~{_profile_tokens(chosen)} of this server's {window} tokens"
        self._say_system(head + ".")
        if note:
            self._say_system(note)
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

        self._worker = _AgentWorker(session, text, select=getattr(self, "_auto", False))
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
        elif kind == "selected":
            names = event["names"]
            self._say_system(
                f"Chose {len(names)} of {len(self._session.all_tools)} tools for this "
                f"request: {', '.join(names)}."
            )
        elif kind == "error":
            self._say_system(f"Error: {event['message']}")
            hint = self._error_hint(event["message"])
            if hint:
                self._say_system(hint)
        elif kind == "done" and (
            not self._said_something or event["stop"] in INCOMPLETE_STOPS
        ):
            # A clean finish and a stall look identical otherwise, and a reply
            # cut off mid-sentence is not "finished" just because a few words
            # of it made it out before the model ran out of room.
            stop = event["stop"]
            label = "cut off" if stop in INCOMPLETE_STOPS else "finished without a summary"
            self._say_system(f"({label}, stop={stop})")

    @staticmethod
    def _error_hint(message: str) -> str:
        """Turn the two failures seen in practice into the action that fixes them.

        Both arrive as a provider's raw HTTP body, which states the fact and not
        the remedy — and in both cases the remedy is a control in this panel.
        """
        low = message.lower()
        if "context" in low and ("exceed" in low or "too long" in low or "larger" in low):
            fits = _profile_that_fits(_window_from_error(message))
            return (
                "The request is bigger than that server's context window, so it was "
                "refused before generating anything — the tool schema is most of it. "
                + (f"Set Tools to {fits!r}, which fits. " if fits else "Narrow Tools above. ")
                + "Or give the server a bigger context: llama.cpp's -c, and note "
                "--parallel N divides that window between N slots."
            )
        if "model" in low and "not found" in low:
            return (
                "That model is not on this server. Press Load to list what it does "
                "have, choose one, and Save."
            )
        return ""

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


def _short(value, limit: int = 120) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…"
