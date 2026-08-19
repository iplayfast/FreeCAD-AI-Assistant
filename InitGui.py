"""AI Assistant workbench: chat panel, AI() console, and the MCP RPC server.

All three faces drive the one tool registry in ``rpc_server`` — the panel and
the console call it directly, the RPC server exposes it to external MCP clients
such as Claude Code.
"""

import os
import sys

import FreeCAD
import FreeCADGui as Gui

try:
    _ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    # FreeCAD execs InitGui.py without binding __file__; the code object still
    # carries the path, so read it back off the frame.
    import inspect

    _ADDON_DIR = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
# FreeCAD already put every Mod/* dir on sys.path, ours included — but after
# Mod/FreeCADMCP, which still holds the retired copy of the rpc_server package.
# A plain "insert if absent" is therefore a no-op and `import rpc_server` picks
# up the old engine (31 tools, no BIM). Hoist ours to the front instead.
while _ADDON_DIR in sys.path:
    sys.path.remove(_ADDON_DIR)
sys.path.insert(0, _ADDON_DIR)

# FreeCAD execs this file into the shared __main__ namespace with *separate*
# globals and locals dicts. Everything assigned above therefore lands in locals,
# where class and function bodies cannot see it — they resolve free names through
# globals only. Publish the one value the class bodies below need, namespaced
# because that globals dict is shared with every other addon's InitGui.
globals()["_AI_ICON"] = os.path.join(_ADDON_DIR, "icons", "ai_workbench.svg")
globals()["_AI_BIM_ICON"] = os.path.join(_ADDON_DIR, "icons", "bim_wizard.svg")
globals()["_AI_CLI_ICON"] = os.path.join(_ADDON_DIR, "icons", "agent_cli.svg")

# AI() / AI_config() / AI_verbose() in the Python console, no import needed.
try:
    from rpc_server.console import register_console_globals

    register_console_globals()
except Exception as e:  # never let this break GUI startup
    FreeCAD.Console.PrintWarning(f"[AI] console setup failed: {e}\n")


class AITogglePanelCommand:
    # Bound onto the class while the file runs: methods are called long after,
    # by which time nothing from this file's namespace is reachable.
    ICON = _AI_ICON  # noqa: F821  (injected into globals above)

    def GetResources(self):
        return {
            "Pixmap": self.ICON,
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


class AIBimWizardCommand:
    ICON = _AI_BIM_ICON  # noqa: F821  (see AITogglePanelCommand.ICON)

    def GetResources(self):
        return {
            "Pixmap": self.ICON,
            "MenuText": "BIM Wizard",
            "ToolTip": "Lay out a floorplan wall by wall — no AI, no API key",
        }

    def Activated(self):
        # Local imports: this method runs long after the file's namespace is
        # gone, so `Gui` from the top of the file is not reachable here.
        import FreeCADGui

        from bim_wizard import BimWizardDialog

        BimWizardDialog(FreeCADGui.getMainWindow()).exec_()

    def IsActive(self):
        return True


Gui.addCommand("AIBimWizardCommand", AIBimWizardCommand())


class AILaunchAgentCommand:
    ICON = _AI_CLI_ICON  # noqa: F821  (see AITogglePanelCommand.ICON)

    def GetResources(self):
        return {
            "Pixmap": self.ICON,
            "MenuText": "Launch Agent CLI",
            "ToolTip": (
                "Open Claude Code or Gemini CLI in a terminal, briefed on this "
                "FreeCAD session, its RPC port and its tools"
            ),
        }

    def Activated(self):
        import FreeCAD

        from rpc_server.console import launch_agent_cli

        ok, message = launch_agent_cli()
        say = FreeCAD.Console.PrintMessage if ok else FreeCAD.Console.PrintWarning
        say(f"[AI] {message}\n")

    def IsActive(self):
        return True


Gui.addCommand("AILaunchAgentCommand", AILaunchAgentCommand())


class AIAssistantWB(Gui.Workbench):
    MenuText = "AI Assistant"
    ToolTip = "AI assistant, console and MCP server for FreeCAD"
    ICON = _AI_ICON  # noqa: F821  (see AITogglePanelCommand.ICON)

    def GetIcon(self):
        return self.ICON

    def Initialize(self):
        from rpc_server import rpc_server  # noqa: F401  (registers the RPC commands)

        commands = [
            "AITogglePanelCommand",
            "AILaunchAgentCommand",
            "AIBimWizardCommand",
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
    # Runs off a timer, i.e. after this file's globals are gone — import locally.
    import FreeCAD

    try:
        from rpc_server import rpc_server

        if not rpc_server.load_settings().get("auto_start_rpc", False):
            return
        FreeCAD.Console.PrintMessage(f"[AI] {rpc_server.start_rpc_server()}\n")
    except Exception as e:
        FreeCAD.Console.PrintWarning(f"[AI] RPC auto-start failed: {e}\n")


from PySide import QtCore  # noqa: E402  (Qt is only available in GUI mode)

QtCore.QTimer.singleShot(0, _auto_start_rpc)
