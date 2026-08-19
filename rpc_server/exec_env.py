"""Persistent namespace for user-supplied code (execute_code / execute_code_async).

Kept separate from any module's own ``globals()`` so executed code can't clobber
the server's imports/functions, while still carrying state across calls. Shared
by the RPC layer and the tool registry so there is exactly one such namespace.
"""

from typing import Any

import FreeCAD
import FreeCADGui

from rpc_server.gui_dispatch import post_to_gui

try:
    import Part
except ImportError:  # optional in a stripped build; never break addon load
    Part = None

try:
    import Sketcher
except ImportError:  # as above
    Sketcher = None

try:
    from rpc_server.build_helpers import HELPERS
except ImportError:  # needs Part/Sketcher; the namespace works fine without it
    HELPERS = {}

_exec_globals: dict[str, Any] | None = None


def exec_namespace() -> dict[str, Any]:
    global _exec_globals
    if _exec_globals is None:
        _exec_globals = {
            "FreeCAD": FreeCAD,
            "App": FreeCAD,
            "FreeCADGui": FreeCADGui,
            "Gui": FreeCADGui,
            # For execute_code_async: run the document-writing part of a
            # background computation on the GUI thread, e.g.
            #   result = shapeA.fuse(shapeB)          # heavy, background-safe
            #   post_to_gui(lambda: _apply(result))   # touches the document
            "post_to_gui": post_to_gui,
        }
        # Pre-imported because nearly all geometry scripting needs them, and a
        # NameError costs a whole retry round trip. Sketcher earns its place
        # because createSketch invites sketch work and constraints live there.
        if Part is not None:
            _exec_globals["Part"] = Part
        if Sketcher is not None:
            _exec_globals["Sketcher"] = Sketcher
        # PartDesign shortcuts (createSketch/createPad/...) — see build_helpers.
        _exec_globals.update(HELPERS)
    return _exec_globals
