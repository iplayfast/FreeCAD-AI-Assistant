"""AI Assistant workbench: chat panel, AI() console, and the MCP RPC server.

All three faces drive the one tool registry in ``rpc_server`` — the panel and
the console call it directly, the RPC server exposes it to external MCP clients
such as Claude Code.
"""

import os
import sys

import FreeCAD
import FreeCADGui as Gui

_ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
if _ADDON_DIR not in sys.path:
    sys.path.insert(0, _ADDON_DIR)

_ICON = os.path.join(_ADDON_DIR, "icons", "ai_workbench.svg")

# AI() / AI_config() / AI_verbose() in the Python console, no import needed.
try:
    from rpc_server.console import register_console_globals

    register_console_globals()
except Exception as e:  # never let this break GUI startup
    FreeCAD.Console.PrintWarning(f"[AI] console setup failed: {e}\n")


class AITogglePanelCommand:
    def GetResources(self):
        return {
            "Pixmap": _ICON,
            "MenuText": "Toggle AI Panel",
            "ToolTip": "Show or hide the AI Assistant chat panel",
        }

    def Activated(self):
        from ai_panel import get_panel

        panel = get_panel()
        panel.setVisible(not panel.isVisible())

    def IsActive(self):
        return True


Gui.addCommand("AITogglePanelCommand", AITogglePanelCommand())


class AIAssistantWB(Gui.Workbench):
    MenuText = "AI Assistant"
    ToolTip = "AI assistant, console and MCP server for FreeCAD"

    def GetIcon(self):
        return _ICON

    def Initialize(self):
        from rpc_server import rpc_server  # noqa: F401  (registers the RPC commands)

        commands = [
            "AITogglePanelCommand",
            "Start_RPC_Server",
            "Stop_RPC_Server",
            "Toggle_Auto_Start",
        ]
        self.appendToolbar("AI Assistant", commands)
        self.appendMenu("AI Assistant", commands)

    def Activated(self):
        from ai_panel import get_panel

        get_panel().show()

    def Deactivated(self):
        pass

    def GetClassName(self):
        return "Gui::PythonWorkbench"


Gui.addWorkbench(AIAssistantWB())


def _auto_start_rpc():
    try:
        from rpc_server import rpc_server

        if not rpc_server.load_settings().get("auto_start_rpc", False):
            return
        FreeCAD.Console.PrintMessage(f"[AI] {rpc_server.start_rpc_server()}\n")
    except Exception as e:
        FreeCAD.Console.PrintWarning(f"[AI] RPC auto-start failed: {e}\n")


from PySide import QtCore  # noqa: E402  (Qt is only available in GUI mode)

QtCore.QTimer.singleShot(0, _auto_start_rpc)
