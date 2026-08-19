"""Qt Command classes for the MCP Addon workbench menu.

Defines the three toolbar/menu entries (Start, Stop, Toggle Auto-Start), plus
the post-startup sync that reflects saved settings on the checkable items.

``register_commands()`` and ``schedule_toggle_sync()`` are invoked from
``rpc_server.py`` at import time to preserve current side-effect behavior.
"""

import FreeCAD
import FreeCADGui
from PySide import QtCore, QtWidgets

from rpc_server.settings import load_settings, save_settings


class StartRPCServerCommand:
    def GetResources(self):
        return {"MenuText": "Start RPC Server", "ToolTip": "Start RPC Server"}

    def Activated(self):
        from . import rpc_server  # late import: avoids circular at module load
        msg = rpc_server.start_rpc_server()
        FreeCAD.Console.PrintMessage(msg + "\n")

    def IsActive(self):
        return True


class StopRPCServerCommand:
    def GetResources(self):
        return {"MenuText": "Stop RPC Server", "ToolTip": "Stop RPC Server"}

    def Activated(self):
        from . import rpc_server
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
    FreeCADGui.addCommand("Start_RPC_Server", StartRPCServerCommand())
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
        QtCore.QTimer.singleShot(2000, lambda: _sync_toggle_states(retries_left - 1))


def schedule_toggle_sync() -> None:
    QtCore.QTimer.singleShot(2000, _sync_toggle_states)
