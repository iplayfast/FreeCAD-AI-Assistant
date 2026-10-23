# SPDX-License-Identifier: LGPL-2.1-or-later
"""Run every module's ``demo()`` self-check, and report which ones failed.

    freecadcmd run_checks.py      # all of them, ~1s
    python3 run_checks.py         # only the FreeCAD-free ones

Most of these checks need a FreeCAD to talk to, so before this existed the only
way to run them was to paste an import into the FreeCAD console — which meant
they were not run, which meant they rotted: the first headless run found a
missing guard in ``createPolar`` and an assertion still expecting an error
message that had been improved months earlier.

``freecadcmd`` is FreeCAD without a GUI. Anything needing the main window
(``get_log``'s Report view scrape) has to fall back cleanly there anyway, so a
module that cannot run headless is usually telling you about a real gap rather
than about the runner.
"""

import importlib
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
CWD = os.path.abspath(os.getcwd())

# Every module with a demo(). Registry and handlers first: a break there fails
# everything after it, and seeing that first is the useful ordering.
MODULES = (
    "freecad.AIAssistant.rpc_server.tools",
    "freecad.AIAssistant.rpc_server.serialize",
    "freecad.AIAssistant.rpc_server.property_mapper",
    "freecad.AIAssistant.rpc_server.providers",
    "freecad.AIAssistant.rpc_server.agent",
    "freecad.AIAssistant.rpc_server.build_helpers",
    "freecad.AIAssistant.rpc_server.tool_handlers",
    "freecad.AIAssistant.rpc_server.partdesign_handlers",
    "freecad.AIAssistant.rpc_server.inspect_handlers",
    "freecad.AIAssistant.rpc_server.bim_handlers",
    "freecad.AIAssistant.rpc_server.console",
    "freecad.AIAssistant.rpc_server.mcp_stdio",
    "freecad.AIAssistant.rpc_server.rpc_server",
)


def main() -> int:
    # Two ways this suite can test an addon other than the one you are editing,
    # and both of them print "13 passed".
    #
    # 1. freecadcmd imports the *script* by basename through sys.path, and every
    #    Mod/* directory is on it. Run `freecadcmd run_checks.py` from a
    #    checkout outside Mod/ and FreeCAD runs the installed Mod/AI/
    #    run_checks.py instead — your file is never opened. Only the cwd knows
    #    where you meant; __file__ already points at the other copy.
    # 2. `freecad` is a namespace package FreeCAD populates from every
    #    Mod/*/freecad before this runs, so the sys.path.insert below does not
    #    win either: the modules come from the installed addon too.
    #
    # Same green light the __main__ guard gives, one level up - so check both.
    if CWD != HERE and os.path.isdir(os.path.join(CWD, "freecad", "AIAssistant")):
        print(f"run_checks: ABORT - you are in\n"
              f"  {CWD}\n"
              f"  but FreeCAD ran the copy in\n"
              f"  {HERE}\n"
              f"  (it resolves the script name through sys.path, and every Mod/ "
              f"entry is on it).\n"
              f"  Nothing here would test your edits. Run the suite from the "
              f"copy FreeCAD\n  loads, or symlink this checkout into Mod/.",
              file=sys.stderr, flush=True)
        return 1

    sys.path.insert(0, HERE)
    failed, skipped = [], []

    for name in MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError as e:
            # No FreeCAD (plain python3): not a failure, just out of reach here.
            skipped.append((name, str(e)))
            continue
        # Case 2 from main()'s note: the script is the right one, the modules
        # are somebody else's.
        loaded = os.path.abspath(getattr(module, "__file__", "") or "")
        if not loaded.startswith(HERE + os.sep):
            print(f"run_checks: ABORT - imported {name} from\n"
                  f"  {loaded}\n"
                  f"  instead of {HERE}. Nothing here would test your edits.\n"
                  f"  Run it from the copy FreeCAD loads, or symlink this "
                  f"checkout into Mod/.", file=sys.stderr, flush=True)
            return 1
        try:
            module.demo()
        except Exception:
            failed.append((name, traceback.format_exc()))

    for name, tb in failed:
        print(f"\n===== FAIL {name}\n{tb}", file=sys.stderr)

    ran = len(MODULES) - len(skipped) - len(failed)
    parts = [f"{ran} passed"]
    if failed:
        parts.append(f"{len(failed)} FAILED ({', '.join(n for n, _ in failed)})")
    if skipped:
        parts.append(f"{len(skipped)} skipped, no FreeCAD (run under freecadcmd)")
    # flush before returning into sys.exit: freecadcmd drops whatever is still
    # buffered when SystemExit is raised, so an unflushed summary is a run that
    # looks like it printed nothing at all.
    print("run_checks: " + ", ".join(parts), flush=True)
    sys.stderr.flush()
    return 1 if failed else 0


# Called unconditionally, not under `if __name__ == "__main__"`: freecadcmd
# imports the script under its own filename ('run_checks'), never '__main__', so
# the usual guard silently runs nothing and exits 0 — a green light for a suite
# that never executed. Nothing imports this file; running it IS the point.
sys.exit(main())
