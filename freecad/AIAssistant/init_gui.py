"""AI Assistant workbench: chat panel, AI() console, and the MCP RPC server.

All three faces drive the one tool registry in ``rpc_server`` — the panel and
the console call it directly, the RPC server exposes it to external MCP clients
such as Claude Code.
"""

import os

import FreeCAD
import FreeCADGui as Gui

# This file is at <addon-root>/freecad/AIAssistant/init_gui.py, so the addon
# root is three directories up.
_ADDON_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AI_ICON = os.path.join(_ADDON_ROOT, "icons", "ai_assistant.svg")
_AI_BIM_ICON = os.path.join(_ADDON_ROOT, "icons", "bim_wizard.svg")
_AI_CLI_ICON = os.path.join(_ADDON_ROOT, "icons", "agent_cli.svg")

# AI() / AI_config() / AI_verbose() in the Python console, no import needed.
try:
    from .rpc_server.console import register_console_globals

    register_console_globals()
except Exception as e:  # never let this break GUI startup
    FreeCAD.Console.PrintWarning(f"[AI] console setup failed: {e}\n")


class AITogglePanelCommand:
    ICON = _AI_ICON

    def GetResources(self):
        return {
            "Pixmap": self.ICON,
            "MenuText": "Toggle AI Panel",
            "ToolTip": "Show or hide the AI Assistant chat panel",
        }

    def Activated(self):
        from .ai_panel import get_panel

        panel = get_panel()
        panel.setVisible(not panel.isVisible())

    def IsActive(self):
        return True


Gui.addCommand("AITogglePanelCommand", AITogglePanelCommand())


class AIBimWizardCommand:
    ICON = _AI_BIM_ICON

    def GetResources(self):
        return {
            "Pixmap": self.ICON,
            "MenuText": "BIM Wizard",
            "ToolTip": "Lay out a floorplan wall by wall — no AI, no API key",
        }

    def Activated(self):
        import FreeCADGui

        from .bim_wizard import BimWizardDialog

        BimWizardDialog(FreeCADGui.getMainWindow()).exec_()

    def IsActive(self):
        return True


Gui.addCommand("AIBimWizardCommand", AIBimWizardCommand())


class AILaunchAgentCommand:
    ICON = _AI_CLI_ICON

    def GetResources(self):
        return {
            "Pixmap": self.ICON,
            "MenuText": "Launch Agent CLI",
            "ToolTip": (
                "Open Claude Code or Antigravity CLI in a terminal, briefed on this "
                "FreeCAD session, its RPC port and its tools"
            ),
        }

    def Activated(self):
        import FreeCAD

        from .rpc_server.console import launch_agent_cli

        ok, message = launch_agent_cli()
        say = FreeCAD.Console.PrintMessage if ok else FreeCAD.Console.PrintWarning
        say(f"[AI] {message}\n")

    def IsActive(self):
        return True


Gui.addCommand("AILaunchAgentCommand", AILaunchAgentCommand())


class AIAssistantWB(Gui.Workbench):
    MenuText = "AI Assistant"
    ToolTip = "AI assistant, console and MCP server for FreeCAD"
    ICON = _AI_ICON

    def GetIcon(self):
        return self.ICON

    def Initialize(self):
        from .rpc_server import rpc_server  # noqa: F401  (registers the RPC commands)

        commands = [
            "AITogglePanelCommand",
            "AILaunchAgentCommand",
            "AIBimWizardCommand",
            "Stop_RPC_Server",
            "Toggle_Auto_Start",
        ]
        self.appendToolbar("AI Assistant", commands)
        self.appendMenu("AI Assistant", commands)

    def Activated(self):
        from .ai_panel import get_panel

        get_panel().show()

    def Deactivated(self):
        pass

    def GetClassName(self):
        return "Gui::PythonWorkbench"


Gui.addWorkbench(AIAssistantWB())


def _auto_start_rpc():
    # Runs off a timer, i.e. after this file's globals are gone — import locally.
    import FreeCAD

    try:
        # From the module that owns it, not through rpc_server: reaching it only
        # ever worked because that module happened to import it, so pruning an
        # unused import there broke auto-start from here.
        from .rpc_server import rpc_server
        from .rpc_server.settings import load_settings

        if not load_settings().get("auto_start_rpc", False):
            return
        FreeCAD.Console.PrintMessage(f"[AI] {rpc_server.start_rpc_server()}\n")
    except Exception as e:
        FreeCAD.Console.PrintWarning(f"[AI] RPC auto-start failed: {e}\n")


from PySide import QtCore  # noqa: E402  (Qt is only available in GUI mode)

QtCore.QTimer.singleShot(0, _auto_start_rpc)
