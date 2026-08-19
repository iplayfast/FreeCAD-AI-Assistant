"""The AI console: an ``AI(...)`` function for FreeCAD's Python console.

Instead of a Qt dock, the assistant is driven from FreeCAD's built-in Python
console:

    AI("make a 10 mm cube and a cylinder next to it")
    AI_config(provider="openai_compat", base_url="http://localhost:11434/v1", model="…")
    AI_config()          # print the current AI settings

``register_console_globals()`` binds ``AI`` / ``AI_config`` into builtins at
addon load so they're available with no import. ``AI`` runs synchronously on the
GUI thread — like every other console statement — so tools call ``call_tool``
directly (no dispatch, no worker thread) and output prints inline. The GUI is
blocked during the network call, exactly as it is for any long console command.
"""

import json
import os
import threading

import FreeCAD

from rpc_server.agent import AgentSession
from rpc_server.gui_dispatch import dispatch_to_gui, ensure_gui_pump, post_to_gui
from rpc_server.providers import ProviderError, get_json, make_provider, set_wire_logger
from rpc_server.settings import load_settings, save_settings

_AI_KEYS = ("ai_provider", "ai_model", "ai_base_url", "ai_api_key")

# The wizard's first question. Everything except Anthropic speaks the
# OpenAI-compatible wire format, so one adapter covers the rest.
# ponytail: a dict, not a plugin registry — add a row to add a provider.
_PRESETS = {
    "Claude (Anthropic)": {
        "provider": "anthropic",
        "base_url": "",
        # No OpenAI-style /models endpoint, so these are offered directly.
        "models": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"],
        "key_from": "console.anthropic.com",
        "cli": {"cmd": "claude", "name": "Claude Code", "config": "~/.claude.json",
                "alts": ("claude-code",)},
    },
    "Gemini (Google)": {
        "provider": "openai_compat",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_from": "aistudio.google.com/apikey",
        # prompt_flag: gemini takes its opening prompt behind -i; claude and
        # codex take it positionally, which is the default here.
        "cli": {"cmd": "gemini", "name": "Gemini CLI", "config": "~/.gemini/settings.json",
                "prompt_flag": "-i", "alts": ("gemini-cli",)},
    },
    "OpenAI": {
        "provider": "openai_compat",
        "base_url": "https://api.openai.com/v1",
        "key_from": "platform.openai.com/api-keys",
        "cli": {"cmd": "codex", "name": "Codex CLI", "config": "~/.codex/config.toml",
                "alts": ("codex-cli",)},
    },
    "Ollama (local)": {
        "provider": "openai_compat",
        "base_url": "http://localhost:11434/v1",
        "key_from": None,   # local, no credential
    },
    "Other (OpenAI-compatible)": {
        "provider": "openai_compat",
        "base_url": "",     # asked for
        "key_from": "",     # asked for, may be left blank
    },
}

_DEFAULT_MODEL_CHOICE = "(provider default)"

# The dropdown row for "run whatever command I type", and the file plus opening
# line that tell whatever gets launched what it is connected to.
_SHELL_CHOICE = "Shell command (external CLI)"
_BRIEFING_NAME = "AGENT_BRIEFING.md"
_BRIEFING_PROMPT = (
    "Read AGENT_BRIEFING.md in this directory before doing anything else. It "
    "describes the live FreeCAD session you are driving, the port to reach it "
    "on, and the tools you can call."
)

_KEYS_URL = "https://console.anthropic.com/settings/keys"

# Terminal emulators and the flag each uses to run a command, most-portable
# first. ponytail: a lookup table, not a plugin system — every one of these
# takes `<term> <flag> claude`, so the launcher is one Popen.
_TERMINALS = (
    ("x-terminal-emulator", "-e"),   # Debian/Ubuntu alternatives symlink
    ("gnome-terminal", "--"),
    ("konsole", "-e"),
    ("xfce4-terminal", "-x"),
    ("alacritty", "-e"),
    ("kitty", ""),
    ("xterm", "-e"),
)


def _ai_config() -> dict:
    s = load_settings()
    return {
        "provider": s.get("ai_provider", "anthropic"),
        "model": s.get("ai_model") or None,   # None => provider default
        "base_url": s.get("ai_base_url", ""),
        "api_key": s.get("ai_api_key", ""),
    }


def _key_source(settings: dict) -> str:
    """Where make_provider will get the API key — mirrors its fallback order.

    FreeCAD started from a desktop launcher does not inherit your shell env, so
    an $ANTHROPIC_API_KEY that works in a terminal is often absent here. Saying
    so beats a bare "API key not set".
    """
    env = "OPENAI_API_KEY" if settings.get("ai_provider") == "openai_compat" else "ANTHROPIC_API_KEY"
    if settings.get("ai_api_key"):
        return "settings file"
    if os.environ.get(env):
        return f"${env}"
    return f"NOT SET (checked settings file and ${env})"


def _ask_choice(title: str, label: str, items: list[str], current: str = "") -> str | None:
    """Pick one of ``items``. None if the user cancelled."""
    from PySide import QtWidgets

    index = items.index(current) if current in items else 0
    value, ok = QtWidgets.QInputDialog.getItem(None, title, label, items, index, False)
    return value if ok else None


def _ask_text(title: str, label: str, default: str = "", secret: bool = False) -> str | None:
    """Free-text answer. None if the user cancelled (blank is a valid answer)."""
    from PySide import QtWidgets

    mode = QtWidgets.QLineEdit.Password if secret else QtWidgets.QLineEdit.Normal
    value, ok = QtWidgets.QInputDialog.getText(None, title, label, mode, default)
    return value.strip() if ok else None


def _has_freecad_mcp(config_path: str) -> bool:
    """Does this CLI's config register a 'freecad' MCP server?

    Substring match, not a parse: the three CLIs use three formats (JSON here,
    TOML there) and all we need to know is whether the entry is present at all.
    """
    try:
        with open(os.path.expanduser(config_path), encoding="utf-8", errors="replace") as f:
            return "freecad" in f.read().lower()
    except OSError:
        return False


def _addon_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def scan_clis() -> list:
    """Every agent CLI in ``_PRESETS``, each with where it was found or None.

    Split out from ``external_clis`` so the panel can *report* the scan instead
    of only acting on it. A CLI missing from the dropdown has two very different
    causes that look identical from the dropdown: it isn't installed, or FreeCAD
    was started from a desktop launcher and never inherited the shell PATH that
    has it. Showing the scan tells those apart.
    """
    import shutil

    def resolve(cli: dict) -> dict:
        # These ship under more than one name — Gemini is `gemini` from the npm
        # package but `gemini-cli` from a hand-rolled npx wrapper, and looking
        # only for the first spelling reports a CLI the user has as missing.
        # ``cmd`` becomes whichever one exists, so the launcher runs that.
        for candidate in (cli["cmd"], *cli.get("alts", ())):
            found = shutil.which(candidate)
            if found:
                return {**cli, "cmd": candidate, "path": found}
        return {**cli, "path": None}

    return [resolve(p["cli"]) for p in _PRESETS.values() if p.get("cli")]


def external_clis() -> dict:
    """Label -> cli dict for every agent CLI that is installed.

    The panel offers these in the same dropdown as the API-key services. They are
    the other half of the choice: a local LLM answers *inside* the panel, while
    these run in their own terminal on the user's existing subscription and reach
    FreeCAD from outside. Only installed ones are listed — offering to launch a
    CLI that isn't there is a dead end. The free-form row is always last, so any
    other command can be pointed at the same interface.
    """
    choices = {
        f"{cli['name']} (external CLI)": cli
        for cli in scan_clis() if cli["path"]
    }
    choices[_SHELL_CHOICE] = {"cmd": "", "name": "shell command", "config": ""}
    return choices


def write_briefing() -> str:
    """Write the file an external agent CLI reads to learn what it is driving.

    A CLI launched from the panel arrives knowing nothing: it has to be told that
    a live FreeCAD is on the other end, which port reaches it, and what it may
    call. A file rather than an argv prompt because the tool list runs to a few
    thousand characters — and because the user can read it too when a run goes
    sideways. Regenerated on every launch, so the tool list can never go stale.
    """
    from rpc_server import tools
    from rpc_server.settings import RPC_HOST, RPC_PORT

    url = f"http://{RPC_HOST}:{RPC_PORT}"
    try:
        # GUI-only (it imports FreeCADGui), which is where this is called for
        # real; headless, the port above is still the right thing to publish.
        from rpc_server import rpc_server as _rpc
    except Exception:
        running, methods = False, []
    else:
        running = _rpc.rpc_server_instance is not None
        methods = sorted(
            m for m in dir(_rpc.FreeCADRPC)
            if not m.startswith("_") and callable(getattr(_rpc.FreeCADRPC, m))
        )
    try:
        registry = tools.describe_tools()
    except Exception as e:      # a broken handler must not block the launch
        registry = []
        tool_note = f"(could not list tools: {e})"
    else:
        tool_note = ""

    lines = [
        "# FreeCAD AI Workbench — briefing for an external agent",
        "",
        "You were launched from the AI Assistant panel of a **running FreeCAD",
        "session**, and your job is to drive that session for the user. This file",
        "describes the interface you have to it. It is generated at launch, so it",
        "matches the FreeCAD that is running right now.",
        "",
        "## Reaching FreeCAD",
        "",
        "Preferred: the `freecad` MCP server, if it is registered with you. Its",
        "tools are the ones listed below and need no setup beyond that entry.",
        "",
        f"Otherwise, talk to the XML-RPC server on **{url}** (loopback only).",
        f"Status at launch: **{'RUNNING' if running else 'NOT RUNNING'}**.",
        "" if running else
        "It is off: the user starts it with 'Start RPC Server' on the AI Assistant\n"
        "toolbar in FreeCAD. Nothing below will answer until they do — say so\n"
        "rather than guessing at another way in.",
        "",
        "```python",
        "import xmlrpc.client",
        f'fc = xmlrpc.client.ServerProxy("{url}", allow_none=True)',
        'fc.ping()',
        'fc.list_documents()',
        'fc.call_tool("create_box", {"length": 10, "width": 10, "height": 10})',
        "```",
        "",
        "### XML-RPC methods",
        "",
        ", ".join(f"`{m}`" for m in methods) or "_(list unavailable at generation time)_",
        "",
        "`call_tool(name, arguments)` is the one that matters — it reaches the same",
        "registry the built-in panel uses, so everything below is available through",
        "it. `describe_tools()` returns the same list machine-readably.",
        "",
        f"## Tools ({len(registry)}) {tool_note}".rstrip(),
        "",
    ]
    for tool in sorted(registry, key=lambda t: t["name"]):
        summary = " ".join((tool.get("description") or "").split())
        if len(summary) > 160:
            summary = summary[:157].rstrip() + "..."
        lines.append(f"- **{tool['name']}** — {summary}")

    lines += [
        "",
        "## Rules",
        "",
        "- `execute_code` runs arbitrary Python inside the user's live FreeCAD.",
        "  There is no sandbox and no confirmation step. Use it for CAD work, not",
        "  for touching the filesystem or the network.",
        "- Each `call_tool` is wrapped in one undo step, so the user can back out",
        "  of anything you do one action at a time. Prefer several small calls to",
        "  one large `execute_code` for that reason alone.",
        "- The user is watching the same model in the GUI. Say what you changed.",
        "- Ask before deleting or overwriting anything you did not create.",
        "",
    ]

    path = os.path.join(_addon_dir(), _BRIEFING_NAME)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def launch_agent_cli(cli: dict = None) -> tuple:
    """Open an agent CLI on this FreeCAD session, briefed, with the port up.

    Shared by the panel's Launch button and the toolbar command so both do the
    same three things in the same order. Starting the RPC server first is the
    point of the wrapper: the briefing publishes a port, and a CLI that finds it
    dead has no way in and no way to explain why.

    ``cli`` omitted means "whatever is installed" — straight through if there is
    only one, otherwise ask.
    """
    from rpc_server.rpc_server import start_rpc_server

    if cli is None:
        installed = [c for c in scan_clis() if c["path"]]
        if not installed:
            wanted = ", ".join(sorted({c["cmd"] for c in scan_clis()}))
            return False, f"no agent CLI on PATH (looked for {wanted})."
        if len(installed) == 1:
            cli = installed[0]
        else:
            chosen = _ask_choice(
                "Launch agent CLI", "Drive FreeCAD from:", [c["name"] for c in installed]
            )
            if chosen is None:
                return False, "cancelled."
            cli = next(c for c in installed if c["name"] == chosen)

    try:
        started = start_rpc_server()
    except Exception as e:
        started = f"RPC server did not start ({e})"
    ok, message = _launch_cli(cli)
    return ok, f"{started} {message}"


def _launch_cli(cli: dict) -> tuple:
    """Open a terminal running an agent CLI in the addon directory.

    These CLIs reach this addon through the ``freecad`` MCP server or the RPC
    port, so they need no API key of their own — they drive FreeCAD over the same
    server that is already running. That only holds if the MCP server is
    registered with *that* CLI, so say so when it isn't.

    Returns ``(ok, message)`` and also prints the message, because both callers
    want it: the wizard writes to the console, the panel into the chat log.
    """
    import shlex
    import shutil
    import subprocess

    def done(ok: bool, message: str) -> tuple:
        print(f"[AI_config] {message}")
        return ok, message

    cmd, name = cli["cmd"], cli["name"]
    if not cmd:
        return done(False, "no command given.")
    argv = shlex.split(cmd)
    if not shutil.which(argv[0]):
        return done(False, f"`{argv[0]}` not found on PATH — install {name} first.")

    notes = []
    if cli.get("config") and not _has_freecad_mcp(cli["config"]):
        notes.append(
            f"no 'freecad' MCP server in {cli['config']}, so {name} must use the "
            "RPC port described in the briefing"
        )

    briefing = write_briefing()
    # The briefing is handed over two ways because no single one reaches every
    # CLI: an opening prompt for the ones whose flag we know, and an env var that
    # any command can read.
    if cli.get("prompt_flag"):
        argv += [cli["prompt_flag"], _BRIEFING_PROMPT]
    elif cli.get("config"):     # a known CLI: takes its opening prompt positionally
        argv.append(_BRIEFING_PROMPT)
    else:
        notes.append(f"read $FREECAD_AI_BRIEFING ({os.path.basename(briefing)})")

    env = {**os.environ, "FREECAD_AI_BRIEFING": briefing}
    addon_dir = _addon_dir()
    for term, flag in _TERMINALS:
        if not shutil.which(term):
            continue
        full = [term, flag, *argv] if flag else [term, *argv]
        try:
            subprocess.Popen(full, cwd=addon_dir, env=env)
        except OSError as e:
            print(f"[AI_config] could not start {term}: {e}")
            continue
        suffix = f" ({'; '.join(notes)})" if notes else ""
        return done(True, f"launched {name} in {term}{suffix}")

    # ponytail: no terminal found (macOS/Windows, or a bare WM) — tell them the
    # command instead of shipping a per-platform launcher nobody asked for.
    return done(
        False,
        f"no terminal emulator found. Run this yourself:\n    cd {addon_dir} && {cmd}",
    )


def _open_keys_page() -> None:
    import webbrowser

    webbrowser.open(_KEYS_URL)
    print(f"[AI_config] opened {_KEYS_URL}")


def _offer_no_key(name: str, preset: dict) -> str:
    """No API key: offer this provider's agent CLI, or the page to buy a key.

    Returns "cli", "browser", or "cancel". The CLI button only appears when that
    CLI is actually installed — Claude Code, Gemini CLI and Codex CLI all speak
    MCP, but offering to launch one that isn't there is just a dead end.
    """
    import shutil

    from PySide import QtWidgets

    cli = preset.get("cli")
    cli = cli if cli and shutil.which(cli["cmd"]) else None

    box = QtWidgets.QMessageBox()
    box.setWindowTitle("AI setup")
    box.setText(f"No API key set for {name}.")
    detail = (
        f"You can drive FreeCAD from {cli['name']} instead — it reaches this "
        "addon through the 'freecad' MCP server and needs no API key.\n\n"
        if cli else ""
    )
    box.setInformativeText(
        detail + "An API key is only needed for the built-in AI() function, and "
        "is billed separately from any subscription."
    )
    launch = box.addButton(f"Launch {cli['name']}", QtWidgets.QMessageBox.AcceptRole) if cli else None
    get_key = (
        box.addButton("Get an API key", QtWidgets.QMessageBox.ActionRole)
        if preset.get("key_from") else None
    )
    box.addButton(QtWidgets.QMessageBox.Cancel)
    box.exec_()

    clicked = box.clickedButton()
    if launch is not None and clicked is launch:
        return "cli"
    return "browser" if get_key is not None and clicked is get_key else "cancel"


def _fetch_models(base_url: str, api_key: str) -> list[str]:
    """Model ids from ``GET {base_url}/models``. Empty list if that doesn't work.

    Ollama, OpenAI and Gemini's OpenAI-compatible endpoint all serve this.
    Failure is normal here (server down, key not yet valid, endpoint absent) and
    just means the wizard falls back to asking for the model name.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = get_json(f"{base_url.rstrip('/')}/models", headers)
    except ProviderError as e:
        print(f"  (could not list models: {e})")
        return []
    entries = resp.get("data") or resp.get("models") or []
    return sorted(str(m.get("id") or m.get("name", "")) for m in entries if isinstance(m, dict))


def _ask_model(base_url: str, api_key: str, current: str, preset: dict) -> str | None:
    """Offer a model list where we can get one, else ask for the name.

    Only Anthropic has a usable "leave it to the provider" default — an
    OpenAI-compatible server needs the model named on every request, so don't
    offer a blank choice there that would only fail later in make_provider.
    """
    allow_default = preset["provider"] == "anthropic"
    models = preset.get("models") or _fetch_models(base_url, api_key)
    if models:
        items = ([_DEFAULT_MODEL_CHOICE] + models) if allow_default else models
        choice = _ask_choice("AI setup", "Model:", items, current or items[0])
        if choice is None:
            return None
        return "" if choice == _DEFAULT_MODEL_CHOICE else choice

    label = "Model name (blank = provider default):" if allow_default else "Model name (required):"
    name = _ask_text("AI setup", label, current)
    if name is None:
        return None
    if not name and not allow_default:
        print("[AI_config] a model name is required for OpenAI-compatible providers")
        return None
    return name


def _run_wizard(settings: dict) -> bool:
    """Ask provider → credentials → model, saving on completion.

    Returns False if the user cancelled at any step, in which case nothing is
    written. Uses Qt dialogs because FreeCAD's Python console is a widget, not a
    stdin REPL — ``input()`` would block the GUI or read the launching terminal.
    """
    names = list(_PRESETS)
    current = next(
        (n for n, p in _PRESETS.items()
         if p["provider"] == settings.get("ai_provider")
         and p.get("base_url", "") == settings.get("ai_base_url", "")),
        names[0],
    )
    name = _ask_choice("AI setup", "Which AI service?", names, current)
    if name is None:
        return False
    preset = _PRESETS[name]

    base_url = preset["base_url"]
    if not base_url and preset["provider"] == "openai_compat":
        base_url = _ask_text(
            "AI setup", "Base URL (OpenAI-compatible, e.g. http://host:11434/v1):",
            settings.get("ai_base_url", ""),
        )
        if base_url is None:
            return False

    api_key = ""
    if preset["key_from"] is not None:
        where = f"\nGet one from {preset['key_from']}" if preset["key_from"] else ""
        api_key = _ask_text(
            "AI setup", f"API key for {name}:{where}", settings.get("ai_api_key", ""), secret=True
        )
        if api_key is None:
            return False
        # Left blank: they may not need a key at all, since this provider's agent
        # CLI already drives the addon over MCP. Offer that before the dead end.
        # (Skipped where blank is legitimate — a local server needing no key.)
        if not api_key and (preset.get("cli") or preset["key_from"]):
            choice = _offer_no_key(name, preset)
            if choice == "cli":
                _launch_cli(preset["cli"])
                return False
            if choice == "browser":
                _open_keys_page()
                api_key = _ask_text("AI setup", f"API key for {name}:", "", secret=True)
                if not api_key:
                    return False
            else:
                return False

    model = _ask_model(base_url, api_key, settings.get("ai_model", ""), preset)
    if model is None:
        return False

    settings.update({
        "ai_provider": preset["provider"], "ai_base_url": base_url,
        "ai_api_key": api_key, "ai_model": model,
    })
    save_settings(settings)
    print(f"[AI_config] configured {name}")
    return True


def _short(value, limit: int = 80) -> str:
    s = json.dumps(value, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


def _emit(line: str) -> None:
    """Write one transcript line to the Report view.

    Not ``print``: the run streams from a GUI timer callback, which is outside
    the Python console's ``sys.stdout`` redirect. That redirect is installed
    only while the console executes a statement, so ``print`` from the callback
    goes to the process's real stdout and is never seen. FreeCAD.Console writes
    to the Report view from any context, which is where addon output belongs
    anyway. (View -> Panels -> Report view, if it isn't open.)
    """
    FreeCAD.Console.PrintMessage(line + "\n")


_VERBOSE = {"on": False, "full": False, "limit": 2000}


def _clip(text: str) -> str:
    limit = _VERBOSE["limit"]
    if _VERBOSE["full"] or len(text) <= limit:
        return text
    return f"{text[:limit]}\n      … {len(text) - limit} more chars (AI_verbose(full=True) for all)"


def _wire_log(direction: str, url: str, body: dict) -> None:
    """Render one side of a provider HTTP exchange to the Report view.

    Requests are summarised rather than dumped: the tool schemas alone are
    several thousand characters and repeat on every turn, which buries the part
    you actually want to read. Tool *names* answer the usual question ("did the
    model even see createPolar?"); full=True dumps everything.
    """
    if direction == "request":
        _emit(f"[wire →] POST {url}")
        _emit(f"      model={body.get('model')}")
        if body.get("system"):
            _emit(f"      system={_clip(str(body['system']))}")
        tools = body.get("tools") or []
        names = [
            t.get("name") or t.get("function", {}).get("name", "?") for t in tools
        ]
        _emit(f"      tools({len(names)})={', '.join(names) or 'none'}")
        messages = body.get("messages") or []
        _emit(f"      messages({len(messages)}):")
        shown = messages if _VERBOSE["full"] else messages[-3:]
        if len(shown) < len(messages):
            _emit(f"        … {len(messages) - len(shown)} earlier turn(s) omitted")
        for m in shown:
            _emit(f"        {m.get('role')}: {_clip(json.dumps(m.get('content'), default=str))}")
    elif direction == "response":
        _emit(f"[wire ←] {_clip(json.dumps(body, indent=1, default=str))}")
    else:
        _emit(f"[wire ✗] {body.get('error')}")


def AI_verbose(on: bool = True, full: bool = False, limit: int = 2000) -> None:
    """Log every provider request and response to the Report view.

    AI_verbose()             -> summaries (tool names, last 3 turns, clipped)
    AI_verbose(full=True)    -> untruncated, every turn, full tool schemas
    AI_verbose(False)        -> off

    Use it when the model's behaviour is the mystery rather than the code's:
    which tools it was actually offered, what it sent back verbatim, and which
    turn a run went wrong on.
    """
    _VERBOSE.update(on=bool(on), full=bool(full), limit=int(limit))
    set_wire_logger(_wire_log if on else None)
    state = "on" + (" (full)" if full else f" (clipped at {limit})") if on else "off"
    _emit(f"[AI_verbose] {state}")
    print(f"[AI_verbose] {state} — output goes to the Report view")


def _print_event(e: dict) -> bool:
    """Render one event. Returns True if it was a non-empty assistant message.

    AI() tracks that so a turn ending with no text still says something —
    otherwise a clean finish and a stalled run look identical.
    """
    t = e["type"]
    if t == "assistant":
        if e["text"].strip():
            _emit(f"[AI] {e['text']}")
            return True
    elif t == "tool_call":
        _emit(f"  → {e['name']}({_short(e['arguments'])})")
    elif t == "tool_result":
        if e["is_error"]:
            _emit(f"  ✗ {e['name']}: {e['content']}")
        else:
            _emit(f"  ✓ {e['name']} {_short(e['result'])}")
    elif t == "error":
        _emit(f"[AI error] {e['message']}")
    # "done" emits nothing here; AI() reports it only when nothing else was said.
    return False


_running = threading.Lock()

# Tool calls are marshalled onto the GUI thread and can be slow (a recompute, a
# solver run), but the GUI thread is now free while the model thinks, so the
# wait is the tool's own runtime rather than queueing behind the network.
_TOOL_TIMEOUT = 600


def AI(prompt: str) -> None:
    """Ask the embedded AI assistant to do something in FreeCAD.

    Returns immediately; the run continues on a worker thread and prints as it
    goes. The model's network waits therefore happen off the GUI thread, so
    FreeCAD stays responsive and you can watch progress instead of facing a
    frozen window. Tool calls are marshalled back onto the GUI thread, which is
    where FreeCAD requires document and ViewObject access to happen.
    """
    from rpc_server.tools import call_tool

    try:
        provider = make_provider(_ai_config())
    except ProviderError as e:
        print(f"[AI] {e}\n      Configure one with AI_config(...) — see AI_config().")
        return

    if not _running.acquire(blocking=False):
        print("[AI] a run is already in progress — wait for it to finish.")
        return

    # Nothing drains the queue unless the pump is up, and the console must work
    # with the RPC server switched off.
    ensure_gui_pump()

    def run_tool(name: str, arguments: dict):
        return dispatch_to_gui(lambda: call_tool(name, arguments), timeout=_TOOL_TIMEOUT)

    session = AgentSession(provider, run_tool=run_tool)
    _emit(f"[you] {prompt}")

    def worker() -> None:
        said_something = False
        try:
            for event in session.run(prompt):
                # Emitting is marshalled to the GUI thread like everything else.
                # Bind the event per-iteration: a bare closure would capture the
                # loop variable and render whatever it holds when the task runs.
                post_to_gui(lambda e=event: _print_event(e))
                if event["type"] == "assistant" and event["text"].strip():
                    said_something = True
                if event["type"] == "done" and not said_something:
                    stop = event["stop"]
                    post_to_gui(
                        lambda s=stop: _emit(f"[AI] (finished without a summary, stop={s})")
                    )
        except Exception as e:
            # A worker thread dying silently is the worst outcome: the transcript
            # would simply stop with no indication anything went wrong.
            message = f"{type(e).__name__}: {e}"
            post_to_gui(lambda m=message: _emit(f"[AI error] {m}"))
        finally:
            _running.release()

    threading.Thread(target=worker, name="FreeCADMCP-AI", daemon=True).start()


def AI_config(**kwargs) -> None:
    """Set up or show AI settings.

    AI_config()                                    -> guided setup (Qt dialogs),
                                                      then print the result
    AI_config(show=True)                           -> print current settings only
    AI_config(provider="anthropic", api_key="…")   -> set directly, no prompts
                                                      (provider/model/base_url/api_key)
    """
    settings = load_settings()
    if not kwargs or kwargs == {"show": True}:
        if not kwargs and not _run_wizard(settings):
            print("[AI_config] no changes saved")
        settings = load_settings()
        print("AI settings:")
        for key in _AI_KEYS:
            val = settings.get(key, "")
            if key == "ai_api_key" and val:
                val = "***set***"
            print(f"  {key} = {val!r}")
        print(f"  api key from: {_key_source(settings)}")
        return

    valid = {f"ai_{k}": v for k, v in kwargs.items() if f"ai_{k}" in _AI_KEYS}
    unknown = [k for k in kwargs if f"ai_{k}" not in _AI_KEYS]
    if unknown:
        print(f"[AI_config] ignored unknown option(s): {', '.join(unknown)}")
        print("            valid: provider, model, base_url, api_key")
    settings.update(valid)
    save_settings(settings)
    for key in valid:
        shown = "***set***" if key == "ai_api_key" and valid[key] else valid[key]
        print(f"  {key} = {shown!r}")


def register_console_globals() -> None:
    """Bind AI / AI_config into builtins so they need no import in the console."""
    import builtins

    builtins.AI = AI
    builtins.AI_config = AI_config
    builtins.AI_verbose = AI_verbose


def demo() -> None:
    """Self-check of the setup wizard with the dialogs stubbed out.

    Needs FreeCAD (settings.py imports it), so run it from the Python console:
      from rpc_server.console import demo; demo()
    """
    import shutil

    import rpc_server.console as m
    from rpc_server import tools
    from rpc_server.settings import RPC_HOST, RPC_PORT

    real = (m._ask_choice, m._ask_text, m.get_json, m.save_settings,
            m._offer_no_key, m._launch_cli, m._open_keys_page)
    saved: list[dict] = []
    launched: list[dict] = []
    m.save_settings = saved.append
    m._launch_cli = launched.append      # never spawn a real terminal in a test
    m._open_keys_page = lambda: None     # never open a real browser in a test

    def scripted(choices: list, texts: list):
        m._ask_choice = lambda *a, **k: choices.pop(0)
        m._ask_text = lambda *a, **k: texts.pop(0)

    try:
        # Anthropic: preset models are offered, so no /models call is made.
        m.get_json = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch expected"))
        scripted(["Claude (Anthropic)", "claude-sonnet-5"], ["sk-test"])
        assert m._run_wizard({}) is True
        assert saved[-1] == {
            "ai_provider": "anthropic", "ai_base_url": "", "ai_api_key": "sk-test",
            "ai_model": "claude-sonnet-5",
        }, saved[-1]

        # Ollama: no key prompt, models listed live from /models.
        m.get_json = lambda url, *a, **k: (
            {"data": [{"id": "qwen3:8b"}, {"id": "llama3:8b"}]}
            if url == "http://localhost:11434/v1/models" else {}
        )
        scripted(["Ollama (local)", "llama3:8b"], [])   # empty texts => key not asked
        assert m._run_wizard({}) is True
        assert saved[-1]["ai_model"] == "llama3:8b", saved[-1]
        assert saved[-1]["ai_api_key"] == "", saved[-1]

        # No "(provider default)" for OpenAI-compatible servers: they require an
        # explicit model, so offering one would only fail later in make_provider.
        offered: list[list[str]] = []
        picks = ["Ollama (local)", "llama3:8b"]

        def choose(title, label, items, current=""):
            offered.append(items)
            return picks.pop(0)

        m._ask_choice = choose
        m._ask_text = lambda *a, **k: ""
        assert m._run_wizard({}) is True
        assert _DEFAULT_MODEL_CHOICE not in offered[-1], offered[-1]
        assert saved[-1]["ai_model"] == "llama3:8b", saved[-1]

        # ...but Anthropic, which has one, still offers it.
        scripted(["Claude (Anthropic)", _DEFAULT_MODEL_CHOICE], ["sk-test"])
        assert m._run_wizard({}) is True and saved[-1]["ai_model"] == "", saved[-1]

        # Unreachable server => fall back to typing the model name.
        m.get_json = lambda *a, **k: (_ for _ in ()).throw(ProviderError("refused"))
        scripted(["Ollama (local)"], ["mistral:7b"])
        assert m._run_wizard({}) is True and saved[-1]["ai_model"] == "mistral:7b", saved[-1]

        # ...and a blank model there is refused rather than saved.
        before_blank = len(saved)
        scripted(["Ollama (local)"], [""])
        assert m._run_wizard({}) is False
        assert len(saved) == before_blank, "blank model must not be saved"

        # Cancelling any step saves nothing.
        before = len(saved)
        scripted([None], [])
        assert m._run_wizard({}) is False
        scripted(["Claude (Anthropic)"], [None])          # cancel at the key
        assert m._run_wizard({}) is False
        scripted(["Claude (Anthropic)", None], ["sk-x"])  # cancel at the model
        assert m._run_wizard({}) is False
        assert len(saved) == before, "cancel must not write settings"

        # _fetch_models tolerates both response shapes and bad rows.
        m.get_json = lambda *a, **k: {"models": [{"name": "b"}, {"id": "a"}, "junk"]}
        assert m._fetch_models("http://x/v1", "") == ["a", "b"]

        # Blank key: offer the provider's agent CLI / a key page / cancel.
        # Not Anthropic-specific — every keyed provider has an MCP-speaking CLI.
        before = len(saved)
        for provider, cli_cmd in (("Claude (Anthropic)", "claude"),
                                  ("Gemini (Google)", "gemini"),
                                  ("OpenAI", "codex")):
            launched.clear()
            m._offer_no_key = lambda name, preset: "cli"
            scripted([provider], [""])              # blank key
            assert m._run_wizard({}) is False
            assert [c["cmd"] for c in launched] == [cli_cmd], (provider, launched)

        m._offer_no_key = lambda name, preset: "cancel"
        scripted(["Claude (Anthropic)"], [""])
        assert m._run_wizard({}) is False

        # "browser" re-asks for the key; a real one entered then proceeds.
        m._offer_no_key = lambda name, preset: "browser"
        scripted(["Claude (Anthropic)", "claude-opus-5"], ["", "sk-after-signup"])
        assert m._run_wizard({}) is True
        assert saved[-1]["ai_api_key"] == "sk-after-signup", saved[-1]

        m._offer_no_key = lambda name, preset: "browser"   # ...still blank => give up
        scripted(["Claude (Anthropic)"], ["", ""])
        assert m._run_wizard({}) is False
        assert len(saved) == before + 1, "only the successful run may save"

        # A key that is supplied never reaches the offer dialog.
        def unexpected(name, preset):
            raise AssertionError("should not be offered")

        m._offer_no_key = unexpected
        scripted(["Claude (Anthropic)", "claude-opus-5"], ["sk-have-one"])
        assert m._run_wizard({}) is True

        # A local server legitimately has no key, so it is never offered either.
        m.get_json = lambda *a, **k: {"data": [{"id": "llama3:8b"}]}
        scripted(["Ollama (local)", "llama3:8b"], [])
        assert m._run_wizard({}) is True

        # _print_event reports only non-empty assistant text, so AI() can tell a
        # silent finish from a hang.
        assert m._print_event({"type": "assistant", "text": "hi"}) is True
        assert m._print_event({"type": "assistant", "text": "   "}) is False
        assert m._print_event({"type": "done", "stop": "end"}) is False
        assert m._print_event({"type": "tool_call", "name": "t", "arguments": {}}) is False

        # Verbose wire logging: summaries name the tools (the usual question is
        # "was the model even offered X?") without dumping their schemas.
        lines: list[str] = []
        real_emit = m._emit
        m._emit = lines.append
        try:
            m.AI_verbose(True)
            lines.clear()
            m._wire_log("request", "http://x/v1/chat/completions", {
                "model": "test-model",
                "tools": [{"function": {"name": "createPolar"}}, {"name": "create_object"}],
                "messages": [{"role": "user", "content": "a"},
                             {"role": "assistant", "content": "b"},
                             {"role": "user", "content": "c"},
                             {"role": "user", "content": "d"}],
            })
            blob = "\n".join(lines)
            assert "test-model" in blob and "tools(2)=createPolar, create_object" in blob, blob
            assert "1 earlier turn(s) omitted" in blob, blob   # last 3 shown by default

            lines.clear()
            m._wire_log("response", "http://x", {"choices": [{"finish_reason": "stop"}]})
            assert "[wire ←]" in lines[0] and "finish_reason" in lines[0], lines

            lines.clear()
            m._wire_log("error", "http://x", {"error": "HTTP 500"})
            assert "HTTP 500" in lines[0], lines

            # Clipping, and full=True defeating it.
            m._VERBOSE.update(full=False, limit=10)
            assert "more chars" in m._clip("x" * 50)
            m._VERBOSE.update(full=True)
            assert m._clip("x" * 50) == "x" * 50
        finally:
            m.AI_verbose(False)
            m._emit = real_emit
        assert m._VERBOSE["on"] is False

        # _has_freecad_mcp: present, absent, and no such file.
        assert m._has_freecad_mcp("~/.claude.json") is True
        assert m._has_freecad_mcp(__file__) is True          # this file says "freecad"
        assert m._has_freecad_mcp("/nonexistent/xyz.json") is False

        # The free-form row is always offered; the named CLIs only when installed,
        # so assert the shape rather than which ones this machine happens to have.
        choices = m.external_clis()
        assert m._SHELL_CHOICE in choices and choices[m._SHELL_CHOICE]["cmd"] == ""
        assert all(shutil.which(c["cmd"]) for n, c in choices.items() if n != m._SHELL_CHOICE)

        # scan_clis reports every known CLI either way, which is what lets the
        # panel say "not on PATH" instead of just dropping the row; external_clis
        # is exactly its installed subset.
        scan = m.scan_clis()
        assert len(scan) >= len(choices) - 1, (scan, choices)
        assert all(("path" in c and c["cmd"]) for c in scan), scan
        assert (sum(1 for c in scan if c["path"])) == len(choices) - 1, (scan, choices)

        # The briefing has to carry the three things an external agent cannot
        # guess: where to connect, how, and what it may call.
        text = open(m.write_briefing(), encoding="utf-8").read()
        assert f"{RPC_HOST}:{RPC_PORT}" in text, "briefing must name the port"
        assert "call_tool" in text and "xmlrpc.client" in text, text[:400]
        for tool in tools.describe_tools():
            assert f"**{tool['name']}**" in text, f"{tool['name']} missing from briefing"
        assert "no sandbox" in text, "briefing must warn about execute_code"
    finally:
        (m._ask_choice, m._ask_text, m.get_json, m.save_settings,
         m._offer_no_key, m._launch_cli, m._open_keys_page) = real

    print("console demo: all checks passed")
