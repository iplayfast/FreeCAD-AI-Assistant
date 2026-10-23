# SPDX-License-Identifier: LGPL-2.1-or-later
"""Qt Command classes for the MCP Addon workbench menu.

Defines the two toolbar/menu entries (Stop, Toggle Auto-Start), plus the
post-startup sync that reflects saved settings on the checkable items.

``register_commands()`` and ``schedule_toggle_sync()`` are invoked from
``rpc_server.py`` at import time to preserve current side-effect behavior.
"""

import FreeCAD
import FreeCADGui

from .settings import load_settings, save_settings

# PySide is imported inside the two functions that touch Qt, not here. Importing
# it at module scope kills freecadcmd outright — the process exits before any
# traceback — which made rpc_server.py (this module's importer) impossible to
# load headless, and so impossible to include in run_checks.


# There is no Start command. Launching an agent CLI starts the server itself —
# the briefing promises a live port, so it has to — and auto_start covers the
# rest. A button whose job is already done by the only action that needs it is
# one more thing to explain. Stop stays: the port accepts arbitrary Python, and
# closing it without quitting FreeCAD has to be possible.
class StopRPCServerCommand:
    def GetResources(self):
        return {"MenuText": "Stop RPC Server",
                "ToolTip": "Stop the RPC server. Launching an agent CLI starts it again."}

    def Activated(self):
        from . import rpc_server  # late import: avoids circular at module load
        msg = rpc_server.stop_rpc_server()
        FreeCAD.Console.PrintMessage(msg + "\n")

    def IsActive(self):
        return True


class ToggleAutoStartCommand:
    def GetResources(self):
        return {
            "MenuText": "Auto-Start Server",
            "ToolTip": "Automatically start the RPC server when FreeCAD launches.",
            "Checkable": True,
        }

    def Activated(self, checked=0):
        settings = load_settings()
        settings["auto_start_rpc"] = bool(checked)
        save_settings(settings)

        if settings["auto_start_rpc"]:
            FreeCAD.Console.PrintMessage(
                "MCP RPC server will start automatically on next FreeCAD launch.\n"
            )
        else:
            FreeCAD.Console.PrintMessage(
                "MCP RPC server auto-start disabled.\n"
            )

    def IsActive(self):
        return True


def register_commands() -> None:
    # Headless (freecadcmd) has a FreeCADGui module with no addCommand, and this
    # runs at rpc_server import time — so without this guard the whole RPC layer
    # was unimportable outside the GUI, and therefore untestable by run_checks.
    # There are no toolbars to add commands to anyway.
    if not hasattr(FreeCADGui, "addCommand"):
        return
    FreeCADGui.addCommand("Stop_RPC_Server", StopRPCServerCommand())
    FreeCADGui.addCommand("Toggle_Auto_Start", ToggleAutoStartCommand())


# Map command objectName -> settings key. Matching on objectName rather than
# the localized menu text keeps this working under translation.
_TOGGLE_COMMANDS = {
    "Toggle_Auto_Start": "auto_start_rpc",
}
_SYNC_MAX_RETRIES = 10  # ~20 s at 2 s/retry before giving up


def _sync_toggle_states(retries_left: int = _SYNC_MAX_RETRIES) -> None:
    """Sync checkable menu items with saved settings on startup.

    The menu actions are created asynchronously, so retry a bounded number of
    times until they exist rather than polling forever.
    """
    try:
        settings = load_settings()
        main_window = FreeCADGui.getMainWindow()
        found = 0
        from PySide import QtWidgets

        for action in main_window.findChildren(QtWidgets.QAction):
            key = _TOGGLE_COMMANDS.get(action.objectName())
            if key is not None:
                action.setChecked(bool(settings.get(key, False)))
                found += 1
        if found == len(_TOGGLE_COMMANDS):
            return
    except Exception:
        pass
    if retries_left > 0:
        from PySide import QtCore

        QtCore.QTimer.singleShot(2000, lambda: _sync_toggle_states(retries_left - 1))


def schedule_toggle_sync() -> None:
    if not hasattr(FreeCADGui, "addCommand"):
        return          # headless: no menu actions to sync
    from PySide import QtCore

    QtCore.QTimer.singleShot(2000, _sync_toggle_states)
