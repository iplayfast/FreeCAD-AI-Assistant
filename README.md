# AI Assistant — a FreeCAD workbench

Drive FreeCAD from an AI assistant: a chat panel in the GUI, an `AI()` function in
the Python console, and an MCP/XML-RPC server that external agent CLIs and MCP
clients can connect to.

All three drive **one tool registry**. A tool added to the registry appears in the
panel, in the console and over MCP with no further wiring — which is the point,
because before the registry was shared the three faces could and did see
different sets of tools.

No third-party Python packages. The provider layer is stdlib `urllib`; the only
imports outside the standard library are FreeCAD's own modules and PySide.

## Two ways to run a model

The Service dropdown is one decision: who is doing the thinking.

**Local and self-hosted servers** answer inside the panel. Ollama is preconfigured;
anything else OpenAI-compatible works via *Other*, or is found for you — press
**Detect** and the addon asks every loopback listener whether it serves
`/v1/models`. That is not the same as probing well-known ports: Unsloth's
llama.cpp child takes an ephemeral port that changes whenever you load a
different model, so a fixed list would never find it.

**Agent CLIs** — Claude Code, Antigravity CLI (`agy`), Codex — run in their own terminal on
your existing subscription, and reach FreeCAD from outside. They need no API key
here, because they already have their own. Pick one and press **Launch**, or use
the *Launch Agent CLI* toolbar button.

A launched CLI arrives knowing nothing about the session it has been pointed at,
so `AGENT_BRIEFING.md` is generated against the running FreeCAD and handed over:
how to connect, whether the port is actually up, the XML-RPC method list, every
tool with its description, and the rules that matter. It is regenerated on each
launch, so it can never describe a FreeCAD other than the one running.

## Install

Clone into your FreeCAD `Mod` directory as `AI`, then restart FreeCAD:

```bash
git clone https://github.com/iplayfast/FreeCAD-AI-Workbench.git \
  ~/.local/share/FreeCAD/<version>/Mod/AI
```

On Windows that is `%APPDATA%\FreeCAD\Mod`, on macOS
`~/Library/Application Support/FreeCAD/Mod`.

Needs FreeCAD 0.21+ and Python 3.10+ (FreeCAD's own interpreter — nothing to
install). Developed against FreeCAD 26.3.

Pick **AI Assistant** from the workbench dropdown. The toolbar has three commands:
toggle the chat panel, launch an agent CLI, and the BIM wizard.

### Optional: the wiki mirror

The `search_wiki` tool reads a local mirror of the FreeCAD wiki. Clone it and
symlink it into the addon directory:

```bash
git clone https://github.com/FreeCAD/FreeCAD-Documentation-Project.git
ln -s /path/to/FreeCAD-Documentation-Project FreeCAD-Documentation-Project
```

Without it every other tool still works; `search_wiki` reports that the mirror is
missing.

## The BIM wizard

Lay out a floorplan wall by wall, with no AI and no API key — it drives the same
tool registry directly. Each wall goes into one Sketcher sketch as you enter it,
so you get the same fully-constrained baseline the AI path produces and can mix
the two freely.

## Settings

Stored in `freecad_mcp_settings.json` under FreeCAD's user app data directory,
**not** in this repository:

| Key | Meaning |
|---|---|
| `ai_provider` | `anthropic` or `openai_compat` |
| `ai_base_url` | for `openai_compat`, e.g. `http://localhost:11434/v1` |
| `ai_model` | empty means the provider's default |
| `ai_api_key` | falls back to `$ANTHROPIC_API_KEY` / `$OPENAI_API_KEY` |
| `ai_timeout` | seconds to wait for one completion (default 600) |
| `ai_tool_profile` | which slice of the registry to advertise; see Tool profiles |
| `auto_start_rpc` | start the RPC server when FreeCAD loads |

The timeout is minutes rather than seconds on purpose: a local server may have to
page a model into memory and reason over the whole tool schema before the first
token, and cutting a working run short is indistinguishable from a broken addon.

FreeCAD started from a desktop launcher does not inherit your shell environment,
so an `$ANTHROPIC_API_KEY` that works in a terminal is often absent inside
FreeCAD. The same applies to finding agent CLIs on `PATH` — the panel reports
which it found, so a missing one is diagnosable rather than merely absent.

## Tools

44, in four groups:

- **Documents and objects** — create/open/save/export, undo, object creation and
  editing, dependency graph, `execute_code` and `execute_code_async`, FEM
  analysis, the parts library, screenshots, text labels.
- **PartDesign** — bodies, sketches, circles and polygons, pad, pocket, polar
  pattern.
- **Inspection** (read-only) — shape validity, mass properties, distance,
  interference, cross sections, sketch info, selection, log.
- **BIM and reference** — BIM documents, walls from sketches, openings, sketch
  lines, wiki search, skills, and `lookup_freecad_op`.

### Operations without a dedicated tool

Registering a tool for all ~273 FreeCAD commands would cost ~40,000 tokens of
schema on every request, which is the problem the profiles above exist to solve.
Instead `lookup_freecad_op` searches a bundled map of them — `skills/freecad_ops.json`
— and returns the scripted Python, what must exist first, what usually follows,
and the non-obvious part:

    lookup_freecad_op("revolve a sketch")
      Part_Revolve            doc.addObject('Part::Revolution', ...)
      PartDesign_Revolution   body.newObject('PartDesign::Revolution', ...)

273 operations for one 835-character schema. It matters because the scripted
FreeCAD API changed at 1.0, and a model guessing at it writes code against
attributes that no longer exist.

`describe_tools()` returns the same list machine-readably.

### Tool profiles

The full schema is about 27,000 characters — roughly 6,700 tokens — and it is
re-sent on **every step** of every turn. That is a fifth of a 32k context before
anything is said, and more than the entire window of a local server started with
`-c 2048`. On small local models that is not overhead, it is the reason nothing
works.

The **Tools** dropdown picks how much to describe:

| Profile | Tools | ≈ tokens |
|---|--:|--:|
| Everything | 44 | 6900 |
| Core only | 13 | 1900 |
| PartDesign | 20 | 3100 |
| PartDesign + inspection | 28 | 4200 |
| BIM | 19 | 3050 |
| Core + extras | 23 | 3500 |
| Auto — pick per request | varies | ~1400 |

Every profile includes core — a session that cannot create or save a document is
not a session. A tool's category follows the module that defines it, so new tools
are grouped without being listed anywhere.

**Auto** asks the model which tools the request needs, against a catalogue of
names and one-line summaries (~870 tokens, 13% of the full schema), then runs the
turn with only those. The catalogue is sent once; the schema is sent on every
step, so a six-step turn drops from ~40,000 tokens to ~9,000. More importantly a
selected subset fits a 4096-token server *including* `pad` and `sketch_polygon`,
which the fixed profile that fits that window does not have.

Profiles only affect what is *advertised*. `call_tool` always sees the whole
registry, so narrowing a profile can never break a caller that asks for a tool by
name, and the MCP face is unaffected.

## MCP / XML-RPC

An XML-RPC server on `127.0.0.1:9875`, started from the toolbar or automatically
via `auto_start_rpc`. `call_tool(name, arguments)` reaches the whole registry;
`describe_tools()` enumerates it.

```python
import xmlrpc.client
fc = xmlrpc.client.ServerProxy("http://127.0.0.1:9875", allow_none=True)
fc.ping()
fc.call_tool("create_document", {"name": "Demo"})
fc.call_tool("create_object", {"doc_name": "Demo", "obj_type": "Part::Box",
                               "obj_name": "Box", "properties": {"Length": 10}})
```

### MCP over stdio

`rpc_server/mcp_stdio.py` is a standalone bridge: an MCP-speaking CLI runs it as
a subprocess and gets `tools/list`/`tools/call` forwarded to the XML-RPC server
above, so the registry's tools appear as native tool calls instead of hand-written
XML-RPC Python. It needs no FreeCAD import and no third-party package — plain
stdlib `xmlrpc.client`, spawned with the system's own `python3` — but the RPC
server above still has to be running to answer it.

Register it in the CLI's MCP config:

```json
{"mcpServers": {"freecad": {"command": "python3", "args": ["<addon dir>/rpc_server/mcp_stdio.py"]}}}
```

or, for a TOML config (Codex CLI):

```toml
[mcp_servers.freecad]
command = "python3"
args = ["<addon dir>/rpc_server/mcp_stdio.py"]
```

A CLI launched without this pre-registered gets the exact snippet, filled in
with its own config path, at the top of `AGENT_BRIEFING.md`.

### Security

The server is bound to loopback and must stay there: `execute_code` runs
arbitrary Python inside your live FreeCAD, with no sandbox and no confirmation
step. Do not expose port 9875 to a network.

Every `call_tool` is wrapped in one undo step, so anything an agent does can be
backed out one action at a time.

## Development

Every non-trivial module ends in a `demo()` of plain `assert`s, runnable on its
own. There is no test framework and none is wanted — match the convention.

```bash
python3 rpc_server/tools.py                                  # registry, offline
python3 rpc_server/mcp_stdio.py --demo                       # MCP bridge dispatch, offline
python3 -c "from rpc_server.providers import demo; demo()"   # providers, offline
python3 -c "from rpc_server.agent import demo; demo()"       # agent loop, offline
```

Those three need no FreeCAD. The rest do, and run under `freecadcmd`:

```python
from rpc_server.console import demo; demo()          # setup wizard, discovery
from rpc_server.build_helpers import demo; demo()    # PartDesign helpers
from rpc_server.bim_handlers import demo; demo()     # BIM, end to end
```

`rpc_server/tools.py` is deliberately FreeCAD-free so the registry unit-tests
offline; keep FreeCAD imports inside functions there.

Two `freecadcmd` traps worth knowing before you write a test script:

- `freecadcmd -c "..."` crashes. Write a `.py` file and run `freecadcmd script.py`.
- `print` and `FreeCAD.Console` output silently drops lines and interleaves with
  OCCT progress spam, and the exit code is 0 even when an assertion fails. Write
  results to a file and read it back.

## License

LGPL-2.1. `rpc_server/` came from the separate FreeCADMCP addon this workbench
replaces, and has been substantially rewritten since.
