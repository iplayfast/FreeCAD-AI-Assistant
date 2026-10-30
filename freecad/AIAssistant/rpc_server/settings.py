# SPDX-License-Identifier: LGPL-2.1-or-later
"""Persistence of MCP RPC server settings under FreeCAD's user app data dir.

Two things here are secrets rather than settings, and neither is written to the
settings JSON:

  * the provider API key — ``load_api_key``/``save_api_key``, OS keyring where
    there is one, a mode-0600 file where there is not;
  * the XML-RPC access token — ``rpc_token``, which every caller of the RPC
    server must present (see ``rpc_server._LocalOnlyHandler``).

``load_settings`` still reports ``ai_api_key`` and ``save_settings`` still
accepts it, so callers deal in one dict; the key just never lands in the file.
"""

import json
import os
import secrets

import FreeCAD


_SETTINGS_FILENAME = "freecad_mcp_settings.json"
_KEY_FILENAME = "freecad_mcp_key"
_TOKEN_FILENAME = "freecad_mcp_token"

# Where the RPC server listens, and so what external agent CLIs are told to
# connect to (see console.write_briefing). Kept here rather than in
# rpc_server.py because that module imports FreeCADGui and cannot be loaded
# outside the GUI, while the briefing has to name the port either way.
RPC_HOST = "127.0.0.1"
RPC_PORT = 9875

_DEFAULT_SETTINGS = {
    "auto_start_rpc": False,
    # AI console (the AI(...) function): provider selection + credentials.
    #
    # No provider by default, deliberately. Defaulting to "anthropic" meant a
    # machine with ANTHROPIC_API_KEY in its environment would send the user's
    # geometry, dimensions and prompts to a remote API on the first Send,
    # without them having chosen a service or been told anything leaves the
    # machine. Unset, make_provider refuses and the panel/console point at
    # setup, where the choice is made and the egress is disclosed.
    "ai_provider": "",            # "" (unset) | "anthropic" | "openai_compat"
    "ai_model": "",               # "" => provider default (claude-opus-5 for anthropic)
    "ai_base_url": "",            # for openai_compat, e.g. http://localhost:11434/v1
    # Set once the user has acknowledged that a remote service receives their
    # design data; see console.confirm_remote_provider.
    "ai_remote_ack": False,
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

# The keyring entry, if this install has a keyring at all.
_KEY_SERVICE = "FreeCAD AI Assistant"
_KEY_USER = "api_key"


def _get_settings_path():
    return os.path.join(FreeCAD.getUserAppDataDir(), _SETTINGS_FILENAME)


def _secret_path(filename):
    return os.path.join(FreeCAD.getUserAppDataDir(), filename)


def _import_keyring():
    """The ``keyring`` package, or None if it is absent.

    Absent is the normal case: FreeCAD's bundled Python ships without it and
    this addon adds no dependencies. Present (a distro build against the system
    Python, or a user who pip-installed it), it is the right place for a
    credential, so use it — but never require it.
    """
    try:
        import keyring
    except Exception:
        return None
    return keyring


def _write_secret(path, text):
    """Write a secret to its own file, as privately as the OS allows.

    chmod is the only lever POSIX gives here, and it is a no-op on Windows —
    there the protection is the location: ``getUserAppDataDir`` is under the
    user's own profile (%APPDATA%), which Windows ACLs to that account. On
    POSIX ~/.local/share is typically world-readable, so the mode is what keeps
    another account out. Written before any content so a widened mode never
    exposes the secret, and after creation too for a file left behind by an
    older version with the wrong mode.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def key_location() -> str:
    """Where the saved API key actually lives, for the UI to tell the user.

    "" when there is no saved key (the env var may still supply one — see
    providers.make_provider).
    """
    keyring = _import_keyring()
    if keyring is not None:
        try:
            if keyring.get_password(_KEY_SERVICE, _KEY_USER):
                return "the OS keyring"
        except Exception:
            pass
    path = _secret_path(_KEY_FILENAME)
    return f"{path} (unencrypted, mode 0600)" if os.path.exists(path) else ""


def load_api_key() -> str:
    """The saved provider credential, or "" if none is saved."""
    keyring = _import_keyring()
    if keyring is not None:
        try:
            value = keyring.get_password(_KEY_SERVICE, _KEY_USER)
        except Exception:
            value = None       # installed but no usable backend
        if value:
            return value
    try:
        with open(_secret_path(_KEY_FILENAME), "r") as f:
            return f.read().strip()
    except OSError:
        return ""


def save_api_key(key: str) -> str:
    """Store (or, given "", forget) the provider credential. Returns where.

    Never the settings JSON: that file is written with the default umask, is
    backed up and copied around with the rest of a FreeCAD profile, and a
    credential has no business in it.
    """
    keyring = _import_keyring()
    path = _secret_path(_KEY_FILENAME)
    if keyring is not None:
        try:
            if key:
                keyring.set_password(_KEY_SERVICE, _KEY_USER, key)
            else:
                keyring.delete_password(_KEY_SERVICE, _KEY_USER)
        except Exception:
            pass               # no usable backend: fall through to the file
        else:
            if os.path.exists(path):
                os.remove(path)    # don't leave a stale plaintext copy behind
            return "the OS keyring"
    if not key:
        if os.path.exists(path):
            os.remove(path)
        return "nowhere (cleared)"
    _write_secret(path, key)
    return path


def rpc_token() -> str:
    """The shared secret an XML-RPC caller must present, created on first use.

    The RPC server runs ``exec`` of arbitrary Python inside FreeCAD, so being on
    loopback is not enough: every local account and every process on the machine
    can reach a loopback port. The token is carried as the request path
    (``http://127.0.0.1:9875/<token>``) because that costs a caller nothing —
    ``ServerProxy`` takes it in the URL — where a custom header would cost every
    caller an xmlrpc Transport subclass.

    Persistent rather than per-start, so an MCP config written once keeps
    working across FreeCAD restarts. Delete the file to rotate it.
    """
    path = _secret_path(_TOKEN_FILENAME)
    try:
        with open(path, "r") as f:
            token = f.read().strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    _write_secret(path, token)
    return token


def load_settings():
    path = _get_settings_path()
    settings = dict(_DEFAULT_SETTINGS)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                stored = json.load(f)
            # Written in plaintext by versions up to 0.1.0. Move it into the
            # secret store and rewrite the file without it, once.
            legacy_key = stored.pop("ai_api_key", "")
            settings.update(stored)
            if legacy_key:
                save_api_key(legacy_key)
                save_settings(settings)
                FreeCAD.Console.PrintMessage(
                    "[AI] moved the API key out of the settings file into "
                    f"{key_location()}\n"
                )
        except Exception as e:
            FreeCAD.Console.PrintWarning(f"Failed to load MCP settings: {e}\n")
    settings["ai_api_key"] = load_api_key()
    return settings


def save_settings(settings):
    # The key travels with the rest of the form, but is stored apart from it.
    settings = dict(settings)
    if "ai_api_key" in settings:
        save_api_key(settings.pop("ai_api_key"))
    path = _get_settings_path()
    try:
        with open(path, "w") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        FreeCAD.Console.PrintError(f"Failed to save MCP settings: {e}\n")


def demo() -> None:
    """Self-check of the secret stores and the legacy-key migration.

    Points every path at a temp directory, so it never touches the real
    profile. Run: ``freecadcmd run_checks.py``.
    """
    import tempfile

    from . import settings as m

    real_path, real_keyring = m._secret_path, m._import_keyring
    real_settings_path = m._get_settings_path
    with tempfile.TemporaryDirectory() as tmp:
        m._secret_path = lambda name: os.path.join(tmp, name)
        m._get_settings_path = lambda: os.path.join(tmp, _SETTINGS_FILENAME)
        m._import_keyring = lambda: None     # exercise the file fallback
        try:
            assert m.load_api_key() == ""
            assert m.key_location() == ""

            where = m.save_api_key("sk-secret")
            assert where.endswith(_KEY_FILENAME), where
            assert m.load_api_key() == "sk-secret"
            # The one property that matters on POSIX: nobody else can read it.
            assert oct(os.stat(m._secret_path(_KEY_FILENAME)).st_mode)[-3:] == "600"
            assert "unencrypted" in m.key_location()

            # A saved form carries the key; the settings file must not.
            m.save_settings({"ai_provider": "anthropic", "ai_api_key": "sk-form"})
            with open(m._get_settings_path()) as f:
                written = json.load(f)
            assert "ai_api_key" not in written, written
            assert m.load_settings()["ai_api_key"] == "sk-form"
            assert m.load_settings()["ai_provider"] == "anthropic"

            # Unset by default: no provider is chosen for the user.
            assert _DEFAULT_SETTINGS["ai_provider"] == ""

            # A 0.1.0 settings file with the key still in it: migrated out on
            # the next load, and the file rewritten without it.
            with open(m._get_settings_path(), "w") as f:
                json.dump({"ai_provider": "anthropic", "ai_api_key": "sk-legacy"}, f)
            assert m.load_settings()["ai_api_key"] == "sk-legacy"
            with open(m._get_settings_path()) as f:
                assert "ai_api_key" not in json.load(f)

            assert m.save_api_key("") == "nowhere (cleared)"
            assert m.load_api_key() == ""

            # The RPC token: created once, then stable, and private.
            token = m.rpc_token()
            assert len(token) >= 24 and m.rpc_token() == token
            assert oct(os.stat(m._secret_path(_TOKEN_FILENAME)).st_mode)[-3:] == "600"
        finally:
            m._secret_path, m._import_keyring = real_path, real_keyring
            m._get_settings_path = real_settings_path

    print("settings demo: all checks passed", flush=True)
