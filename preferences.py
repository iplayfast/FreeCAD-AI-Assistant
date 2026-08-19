import FreeCAD
import json

_PARAM_PATH = "User parameter:BaseApp/Preferences/Mod/AIWorkbench"
_DEFAULT_MODELS = [
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-8",
]

# Local server presets: display name → default port
LOCAL_SERVERS = {
    "Ollama":           11434,
    "llama.cpp":        8080,
    "LM Studio":        1234,
    "Jan":              1337,
    "text-gen-webui":   5000,
    "Custom":           None,
}


def _grp():
    return FreeCAD.ParamGet(_PARAM_PATH)


def get_api_key() -> str:
    return _grp().GetString("ApiKey", "")


def set_api_key(v: str):
    _grp().SetString("ApiKey", v)


def get_model() -> str:
    return _grp().GetString("Model", "claude-sonnet-4-6")


def set_model(v: str):
    _grp().SetString("Model", v)


def get_base_url() -> str:
    return _grp().GetString("BaseUrl", "")


def set_base_url(v: str):
    _grp().SetString("BaseUrl", v)


def get_connection_mode() -> str:
    """Return 'remote' or 'local'."""
    return _grp().GetString("ConnectionMode", "remote")


def set_connection_mode(v: str):
    _grp().SetString("ConnectionMode", v)


def get_local_server() -> str:
    """Return the display name of the selected local server preset."""
    return _grp().GetString("LocalServer", "Ollama")


def set_local_server(v: str):
    _grp().SetString("LocalServer", v)


def get_local_port() -> int:
    return _grp().GetInt("LocalPort", 11434)


def set_local_port(v: int):
    _grp().SetInt("LocalPort", int(v))


def available_models() -> list:
    raw = _grp().GetString("ModelList", "")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return list(_DEFAULT_MODELS)


def set_model_list(models: list):
    _grp().SetString("ModelList", json.dumps(models))
