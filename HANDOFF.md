# Handoff — merging FreeCADMCP into the AI workbench

**Date:** 2026-08-19
**Working dir:** `/home/chris/.local/share/FreeCAD/v26-3/Mod`
**Mode:** ponytail (full) is active via SessionStart hook — laziest solution that works, stdlib/native first, shortest diff, minimal prose.

---

## What this work is

Chris had two overlapping FreeCAD addons: `AI/` (Qt dock chat panel, hand-rolled
tool schemas + provider layer, no VCS, no tests) and `FreeCADMCP/` (git-tracked
tool registry serving both an XML-RPC/MCP server and an in-console `AI()`
assistant, with `demo()` self-checks throughout).

Chris chose to **merge FreeCADMCP's engine into `AI/`**, making one
self-contained workbench that also serves MCP. `FreeCADMCP/` is retired but
deliberately left on disk untouched as the fallback and the only copy of the
git history.

## State: steps 1–5 complete, GUI verification pending

`AI/rpc_server/` is now the engine (**copied**, not moved). `ai_panel.py` was
rewritten 965 → 660 lines to drive `rpc_server.agent.AgentSession` +
`rpc_server.tools.call_tool`. Registry went 16 → 43 tools.

Entry points: `AI/InitGui.py` rewritten to own the workbench, the `AI()` console
globals and the RPC server. `AI/Init.py` gutted (it called
`FreeCAD.addImportType("AI Workbench", "AI")`, advertising the module as a file
format handler it cannot service). `FreeCADMCP/InitGui.py` renamed to
`.disabled` so only one `rpc_server` package loads — **rename it back to
revert the whole merge.**

### The one thing not yet confirmed

**Chris had not restarted FreeCAD as of the last message.** Everything below is
verified headlessly via `freecadcmd` only. The panel has never been rendered.
Ask whether the restart happened and what it did before assuming the merge works.

### Open question awaiting Chris's answer

These are now unreferenced (verified by grep) but **not deleted**, because `AI/`
is not a git repo and deletion would be unrecoverable:

```
ai_backend.py  tool_definitions.py  freecad_tools.py
preferences.py  logger.py  bim_floorplan.py  vendor/     (~1,700 lines + 28 MB)
```

`vendor/` is the bundled Anthropic SDK, replaced by the stdlib-urllib provider
layer. The question put to Chris: `git init` + commit the merged state first,
then delete? No answer yet.

Note `AI/ai_assistant.log` is a 13.8 MB unrotated log from the old `logger.py`;
it dies with that file.

---

## Bugs found and fixed while porting (all pre-existing, all verified)

1. **`build_helpers.constrainPoint` inverted the DistanceX/Y argument order** —
   mirrored every off-axis point through the origin. Asked (100, 50) → got
   (−100, −50). Hid because the default call is `x=0, y=0` (coincident branch)
   and `demo()` asserted constraint *counts*, never a coordinate. Fixed at the
   root; `demo()` now asserts coordinates. Affected `addCircle`, `addPolygon`
   and the registered `sketch_circle`/`sketch_polygon` tools.
2. **`add_opening` never cut anything** — `Normal` was `(0,0,0)`, `HoleDepth`
   `0`, the profile was drawn flat in XY, and `Arch.addComponents` was the wrong
   link. Rebuilt on a Sketch profile standing upright in the wall plane. A
   re-placed Draft wire also yields a **null window Shape** once rotated — that
   dead end is documented in a comment in `bim_handlers.py`, don't re-walk it.
3. **BIM wizard direction table** had East = −X, North = −Y, contradicting its
   own docstring and every tool description. Every wizard plan came out mirrored
   and upside down.
4. **Registration split** (the original ask): `inspect_handlers.py` was imported
   by nothing at all, and `partdesign_handlers` only by `console.AI()`, so the
   MCP face and the console saw different tool sets. Now one `load_handlers()`
   choke point in `rpc_server/tools.py`, called from `describe_tools`/`call_tool`.

## Known regression, accepted

Assistant text now appears **per turn, not per token** — the shared provider
layer is non-streaming; the deleted `ai_backend.py` streamed. Tool calls still
appear as they happen. Chris has been told.

---

## Verification recipes — read before writing test scripts

`freecadcmd` has two traps that cost real time this session:

- **`freecadcmd -c "…"` crashes** with "Application unexpectedly terminated".
  Always write a `.py` file and run `freecadcmd /path/to/script.py`.
- **`FreeCAD.Console` / `print` output silently drops lines** and interleaves
  with OCCT progress spam. For anything you must read back, **write results to a
  file** and `cat` it. Exit code is 0 even when an assertion fails.

Working pattern (scratchpad dir is session-specific; use any tmp path):

```python
import sys, traceback
sys.path.insert(0, "/home/chris/.local/share/FreeCAD/v26-3/Mod/AI")
with open("/tmp/result.txt", "w") as f:
    try:
        from rpc_server.bim_handlers import demo
        demo()
        f.write("PASS\n")
    except Exception:
        f.write("FAIL\n" + traceback.format_exc())
```

Self-checks available (all currently passing):

| Check | How |
|---|---|
| Registry plumbing, offline | `python3 AI/rpc_server/tools.py` |
| Provider translations, offline | `python3 -c "from rpc_server.providers import demo; demo()"` (from `AI/`) |
| Agent loop, offline | `from rpc_server.agent import demo` |
| Setup wizard, offline-ish | `from rpc_server.console import demo` (needs FreeCAD) |
| PartDesign helpers | `from rpc_server.build_helpers import demo` (needs FreeCAD) |
| BIM end-to-end | `from rpc_server.bim_handlers import demo` (needs FreeCAD) |

Tool count sanity: `len(tools.describe_tools())` should be **43**.

---

## Layout

```
AI/
  InitGui.py, Init.py          entry points (rewritten)
  ai_panel.py                  Qt panel — AIChatPanel + _BimWizardDialog (rewritten)
  rpc_server/                  the engine, copied from FreeCADMCP
    tools.py                   registry + load_handlers() choke point
    agent.py, providers/       provider-agnostic agent loop
    console.py                 AI()/AI_config()/AI_verbose(); owns _PRESETS + _fetch_models
    tool_handlers.py           document ops (+ open/save/export/undo/read_file ported here)
    partdesign_handlers.py     owns the shared _doc/_obj helpers
    inspect_handlers.py        read-only inspection (was dead code)
    bim_handlers.py            NEW — BIM, sketch_line, search_wiki, get_skill
    build_helpers.py           PartDesign helpers injected into execute_code
    rpc_server.py              XML-RPC on 127.0.0.1:9875
  skills/bim.md                served by the get_skill tool
  FreeCAD-Documentation-Project/wiki/   served by the search_wiki tool
  <dead files listed above>
FreeCADMCP/                    retired; InitGui.py.disabled; holds the git history
```

`ai_panel.py` imports `_PRESETS` and `_fetch_models` from `rpc_server.console`
(private, same package) rather than duplicating the provider table. Moving them
to `settings.py` would break `console.demo()`, which monkeypatches
`console.get_json` — that was considered and rejected.

Settings live in `FreeCAD.getUserAppDataDir()/freecad_mcp_settings.json`
(`ai_provider`, `ai_model`, `ai_base_url`, `ai_api_key`). **That file contains a
plaintext API key — do not cat it into a transcript.** The old
`ParamGet`-based `preferences.py` is dead.

`FreeCADMCP/` still has ~486 lines of uncommitted work plus two `.bak` files
from before this session. Nothing was committed — the standing rule is to commit
only when Chris asks.

---

## Suggested skills

- **`/ponytail-review`** — before deleting the dead files, on the rewritten
  `ai_panel.py` and `bim_handlers.py`. Both are new code written this session and
  have had no over-engineering pass.
- **`/tdd`** — if extending the registry with new tools. The codebase convention
  is an `assert`-based `demo()` per module, no framework; follow it rather than
  introducing pytest.
- **`/diagnose`** — if the GUI restart surfaces panel or threading problems.
  The threading contract (agent on a worker thread, tools marshalled to the GUI
  thread via `dispatch_to_gui`, GUI pump via `ensure_gui_pump`) is the likely
  source of anything intermittent.
- **`/push-milestone`** — if Chris green-lights `git init` on `AI/`; it handles
  README/CHANGELOG/.gitignore and commits.

Avoid `/graphify`, `/browse` and the design skills — nothing here is web or
visual-design work.

## Conventions worth keeping

- Every non-trivial module ends in a `demo()` of plain `assert`s, runnable
  standalone. Match it.
- Deliberate shortcuts are marked with a `ponytail:` comment naming the ceiling.
- Tool descriptions are written *for the model*, saying when to reach for the
  tool and what fails otherwise — not terse API blurbs. Match that voice.
- `rpc_server/tools.py` is deliberately FreeCAD-free so it unit-tests offline;
  keep FreeCAD imports inside functions there.
