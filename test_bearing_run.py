# SPDX-License-Identifier: LGPL-2.1-or-later
"""Drive one agent turn against a real provider and judge what it BUILT.

    freecadcmd test_bearing_run.py                        # default: 608 bearing
    BEARING_DOC=Widget BEARING_PROMPT="..." freecadcmd test_bearing_run.py
    BEARING_MAX_STEPS=8 BEARING_MIN_SOLIDS=1 freecadcmd test_bearing_run.py

Env: BEARING_PROMPT, BEARING_DOC, BEARING_LOG, BEARING_MAX_STEPS (0 = the
ai_max_steps setting), BEARING_MIN_SOLIDS.

The previous run of this test printed "Run Completed Successfully" after 28
calls, 19 errors and zero geometry — the model gave up, and the harness reported
the giving-up as a pass. Nothing here trusts the transcript: the verdict comes
from counting solids in the document at the end. Stop reason "end" with no solid
is a FAIL, which is the whole point.

Exit code is 0 only when the document holds at least ``--min-solids`` solids.
"""

import json
import os
import sys
import time

import FreeCAD

# This must happen BEFORE rpc_server is imported. There may be a second,
# differently-pathed checkout of this addon on sys.path, and headless import
# could pick THAT one — so an edit here was tested against stale code and the
# run looked fine. Claim the name first, then check we got it.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
for _stale in [m for m in list(sys.modules)
               if m == "freecad.AIAssistant" or m.startswith("freecad.AIAssistant.")]:
    del sys.modules[_stale]

import freecad.AIAssistant.rpc_server as _pkg

if not os.path.abspath(_pkg.__file__).startswith(_HERE + os.sep):
    raise SystemExit(
        f"WRONG COPY: 'rpc_server' resolved to {_pkg.__file__}, not {_HERE}. "
        "Another install of this addon is shadowing it (check Mod/ for a second "
        "checkout). Testing the wrong tree is worse than not testing."
    )

from freecad.AIAssistant.rpc_server.agent import AgentSession
from freecad.AIAssistant.rpc_server.console import _ai_config
from freecad.AIAssistant.rpc_server.providers import ProviderError, make_provider
from freecad.AIAssistant.rpc_server.settings import load_settings
from freecad.AIAssistant.rpc_server.tools import call_tool, describe_tools

BEARING_608 = (
    "Build a parametric 608 skate bearing in a new document called 'Bearing608'. "
    "Bore 8 mm, outside diameter 22 mm, width 7 mm, 8 balls of 3.969 mm on a "
    "15 mm pitch circle. Drive every dimension from a VarSet so the whole "
    "bearing resizes when a variable changes: inner ring, outer ring, and the "
    "ball set patterned around Z."
)


def _solids(doc) -> int:
    """Solids across the document. The only claim the transcript cannot fake."""
    total = 0
    for obj in doc.Objects:
        shape = getattr(obj, "Shape", None)
        if shape is None:
            continue
        try:
            total += len(shape.Solids)
        except Exception:
            pass
    return total


class _Args:
    """Options come from the environment, not flags.

    freecadcmd runs its own boost::program_options over the whole command line
    before the script sees it, and answers anything it does not recognise with
    its usage text — including flags placed after ``--pass``. Environment
    variables go around it instead of fighting it.
    """

    prompt = os.environ.get("BEARING_PROMPT") or BEARING_608
    doc = os.environ.get("BEARING_DOC", "Bearing608")
    log = os.environ.get("BEARING_LOG", "test_bearing_run.log")
    max_steps = int(os.environ.get("BEARING_MAX_STEPS", "0"))   # 0 => settings
    min_solids = int(os.environ.get("BEARING_MIN_SOLIDS", "2"))


def main() -> int:
    args = _Args()

    out = open(args.log, "w")

    def say(line: str = "") -> None:
        # FreeCAD swallows plain print() from a launched script in both faces;
        # Console gets through, and the log file is what actually gets read.
        FreeCAD.Console.PrintMessage(line + "\n")
        out.write(line + "\n")
        out.flush()

    try:
        provider = make_provider(_ai_config())
    except ProviderError as exc:
        say(f"BLOCKED: no usable provider — {exc}")
        return 2

    # No dispatch_to_gui: freecadcmd has no GUI thread to marshal onto, and
    # call_tool's own transaction wrapper is what the panel relies on anyway.
    calls = errors = 0
    by_tool: dict[str, list[int]] = {}
    started = time.time()

    settings = load_settings()
    steps = args.max_steps or int(settings.get("ai_max_steps") or 25)
    session = AgentSession(provider, run_tool=call_tool,
                           tools=describe_tools(settings.get("ai_tool_profile")),
                           max_steps=steps)

    say(f"=== {args.doc}: {args.prompt[:60]}... ===")
    say(f"model {settings.get('ai_model') or 'default'} via "
        f"{settings.get('ai_provider')}, max_steps {steps}, "
        f"profile {settings.get('ai_tool_profile')}")
    say()

    stop = "no-done-event"
    for event in session.run(args.prompt):
        kind = event["type"]
        if kind == "assistant" and event["text"].strip():
            say(f"[say] {event['text'][:400]}")
        elif kind == "tool_call":
            calls += 1
            name = event["name"]
            by_tool.setdefault(name, [0, 0])
            by_tool[name][0] += 1
            say(f"[call {calls}] {name}: {json.dumps(event['arguments'])[:600]}")
        elif kind == "tool_result":
            if event["is_error"]:
                errors += 1
                by_tool[event["name"]][1] += 1
                say(f"    ERR {event['content'][:300]}")
            else:
                say(f"    ok  {event['content'][:200]}")
        elif kind == "done":
            stop = event["stop"]
        elif kind == "error":
            stop = "error"
            say(f"[halt] {event['message'][:300]}")

    doc = FreeCAD.getDocument(args.doc) if args.doc in FreeCAD.listDocuments() else None
    solids = _solids(doc) if doc else 0
    objects = len(doc.Objects) if doc else 0

    say()
    say("=== verdict ===")
    say(f"stop reason : {stop}")
    say(f"tool calls  : {calls}  ({errors} errors, {calls - errors} ok)")
    say(f"elapsed     : {time.time() - started:.0f}s")
    for name, (n, err) in sorted(by_tool.items(), key=lambda kv: -kv[1][0]):
        say(f"  {name:24s} {n:3d} calls, {err:3d} errors")
    # execute_code share is the number that says whether the typed tools are
    # being used at all. The run this test was written against was 24/28.
    raw = by_tool.get("execute_code", [0, 0])[0]
    say(f"execute_code share: {raw}/{calls}" if calls else "execute_code share: n/a")
    say(f"document    : {args.doc} — {objects} objects, {solids} solids")

    if solids >= args.min_solids:
        say(f"PASS: built {solids} solids (needed {args.min_solids}).")
        return 0
    say(f"FAIL: {solids} solids, needed {args.min_solids}. A run that builds "
        "nothing is a failure however politely it stopped.")
    return 1


def _run_and_exit() -> None:
    """Run, record the code, and get out of the way in either face.

    Under `freecad` (GUI) a bare sys.exit only raises SystemExit into FreeCAD's
    script runner: the window stays open and the shell sees 0 whatever happened.
    The exit code is the verdict, so it is written to a file the caller can read
    and the window is closed explicitly.
    """
    code = main()
    with open(os.environ.get("BEARING_EXIT", "test_bearing_exit"), "w") as fh:
        fh.write(str(code) + "\n")
    try:
        import FreeCADGui

        window = FreeCADGui.getMainWindow()
    except Exception:
        window = None
    if window is not None:          # GUI: ask Qt to quit, then fall through
        window.close()
        FreeCADGui.getMainWindow().deleteLater()
        os._exit(code)              # Qt would otherwise keep the loop alive
    sys.exit(code)


# No __main__ guard: freecadcmd imports the script as a module named after the
# file ("test_bearing_run"), so `__name__ == "__main__"` is never true and the
# guarded version ran nothing and exited 0 — a green light for a test that never
# started, which is the same lie this runner exists to stop telling.
_run_and_exit()
