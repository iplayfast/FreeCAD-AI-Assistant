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
    except Exception as e:
        FreeCAD.Console.PrintError(f"Failed to save MCP settings: {e}\n")
