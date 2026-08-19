import os
import re
import threading
import traceback
from datetime import datetime

_log_lock = threading.Lock()

def get_log_path() -> str:
    try:
        import FreeCAD
        return os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "AI", "ai_assistant.log")
    except Exception:
        # Fallback to the module directory if FreeCAD is not initialized/imported
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_assistant.log")

def censor_string(text: str, api_key: str = None) -> str:
    if not isinstance(text, str):
        return text
    if api_key and len(api_key) > 4:
        text = text.replace(api_key, "[REDACTED_API_KEY]")
    # Censor common authorization headers and Bearer tokens
    text = re.sub(r'(?i)"authorization"\s*:\s*"Bearer\s+[^"]+"', '"authorization": "Bearer [REDACTED]"', text)
    text = re.sub(r'(?i)Bearer\s+sk-[a-zA-Z0-9\-]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'(?i)Bearer\s+sk-ant-[a-zA-Z0-9\-]+', 'Bearer [REDACTED]', text)
    return text

def censor_data(data, api_key: str = None):
    if isinstance(data, str):
        return censor_string(data, api_key)
    elif isinstance(data, dict):
        return {
            k: "[REDACTED]" if k.lower() in ("authorization", "api-key", "apikey", "x-api-key")
            else censor_data(v, api_key)
            for k, v in data.items()
        }
    elif isinstance(data, list):
        return [censor_data(item, api_key) for item in data]
    return data

def _write_log(level: str, message: str):
    try:
        path = get_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with _log_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{timestamp} [{level}] {message}\n")
    except Exception as e:
        print(f"Failed to write to log: {e}")

def log_info(message: str, api_key: str = None):
    censored = censor_string(message, api_key)
    _write_log("INFO", censored)

def log_error(message: str, exc: Exception = None, api_key: str = None):
    msg = message
    if exc:
        msg += f"\nException: {exc}\nTraceback:\n{traceback.format_exc()}"
    censored = censor_string(msg, api_key)
    _write_log("ERROR", censored)

def log_request(url: str, headers: dict, payload: dict, api_key: str = None):
    import json
    censored_headers = censor_data(headers, api_key)
    censored_payload = censor_data(payload, api_key)
    msg = (
        f"API Request:\n"
        f"  URL: {url}\n"
        f"  Headers: {json.dumps(censored_headers)}\n"
        f"  Payload: {json.dumps(censored_payload, indent=2)}"
    )
    _write_log("REQ", msg)

def log_response(status_code: int, body: str, api_key: str = None):
    # Try parsing response body as json to pretty-print
    import json
    try:
        data = json.loads(body)
        censored_data_body = censor_data(data, api_key)
        formatted_body = json.dumps(censored_data_body, indent=2)
    except Exception:
        formatted_body = censor_string(body, api_key)
        
    msg = (
        f"API Response:\n"
        f"  Status Code: {status_code}\n"
        f"  Body:\n{formatted_body}"
    )
    _write_log("RESP", msg)

def open_log_file() -> bool:
    path = get_log_path()
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} === AI Assistant Log Started ===\n")
            
    try:
        from PySide2.QtCore import QUrl
        from PySide2.QtGui import QDesktopServices
    except ImportError:
        try:
            from PySide.QtCore import QUrl
            from PySide.QtGui import QDesktopServices
        except ImportError:
            return False
            
    return QDesktopServices.openUrl(QUrl.fromLocalFile(path))
