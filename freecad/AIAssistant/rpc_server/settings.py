"""Persistence of MCP RPC server settings under FreeCAD's user app data dir."""

import json
import os

import FreeCAD


_SETTINGS_FILENAME = "freecad_mcp_settings.json"

# Where the RPC server listens, and so what external agent CLIs are told to
# connect to (see console.write_briefing). Kept here rather than in
# rpc_server.py because that module imports FreeCADGui and cannot be loaded
# outside the GUI, while the briefing has to name the port either way.
RPC_HOST = "127.0.0.1"
RPC_PORT = 9875

_DEFAULT_SETTINGS = {
    "auto_start_rpc": False,
    # AI console (the AI(...) function): provider selection + credentials.
    "ai_provider": "anthropic",   # "anthropic" | "openai_compat"
    "ai_model": "",               # "" => provider default (claude-opus-5 for anthropic)
    "ai_base_url": "",            # for openai_compat, e.g. http://localhost:11434/v1
    "ai_api_key": "",             # falls back to ANTHROPIC_API_KEY / OPENAI_API_KEY env
    # Seconds to wait for one completion. Minutes rather than seconds because a
    # local model may have to be paged into memory before the first token.
    "ai_timeout": 600,
    # Which slice of the tool registry to advertise; see tools.PROFILES. The full
    # set is ~6700 tokens re-sent every step, which small local models cannot fit.
    "ai_tool_profile": "Everything",
    # Model round trips allowed in one turn. A build that keeps making
    # progress can want more; a stuck one is caught by the repeated-failure
    # check long before this.
    "ai_max_steps": 25,
    # How much a launched agent CLI may do without asking. See
    # console.CLI_PERMISSIONS. "ask" is every prompt as the CLI ships;
    # "freecad" pre-approves the modelling tools but not execute_code;
    # "skip" is --dangerously-skip-permissions, which is exactly what it says.
    "ai_cli_permissions": "freecad",
}


def _get_settings_path():
    return os.path.join(FreeCAD.getUserAppDataDir(), _SETTINGS_FILENAME)


def load_settings():
    path = _get_settings_path()
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                settings = json.load(f)
            for key, value in _DEFAULT_SETTINGS.items():
                if key not in settings:
                    settings[key] = value
            return settings
        except Exception as e:
            FreeCAD.Console.PrintWarning(f"Failed to load MCP settings: {e}\n")
    return dict(_DEFAULT_SETTINGS)


def save_settings(settings):
    path = _get_settings_path()
    try:
        with open(path, "w") as f:
            json.dump(settings, f, indent=2)
        # This file holds ai_api_key. The default umask leaves it 0644/0664, so
        # a provider key pasted into the panel becomes readable by every account
        # on the machine. Chmod after writing rather than before: the file may
        # already exist from an older version with the wrong mode.
        os.chmod(path, 0o600)
    except Exception as e:
        FreeCAD.Console.PrintError(f"Failed to save MCP settings: {e}\n")
