"""Provider-neutral types and the HTTP helper shared by the AI adapters.

The agent loop speaks in these neutral shapes; each provider adapter translates
them to and from its vendor wire format. Transport is stdlib urllib so the
addon adds no dependency to FreeCAD's embedded Python and every provider goes
through one code path.
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class ProviderError(Exception):
    """Any failure talking to a provider (HTTP error, bad response, no creds)."""


# Optional wire tap for diagnosing model behaviour: called with
# ("request"|"response"|"error", url, body_dict). Kept as a hook rather than
# logging here so this module stays FreeCAD-free and unit-testable offline —
# the console installs a formatter that writes to the Report view.
_wire_logger: Any = None


def set_wire_logger(fn) -> None:
    """Install (or clear, with None) the wire tap used by ``post_json``."""
    global _wire_logger
    _wire_logger = fn


def _tap(direction: str, url: str, body: Any) -> None:
    if _wire_logger is None:
        return
    try:
        _wire_logger(direction, url, body)
    except Exception:
        pass  # diagnostics must never break the request they are observing


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class AssistantReply:
    """One assistant turn, normalised across providers."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # "end" (done), "tools" (wants tool results), "refusal", or "length"
    stop: str = "end"


# Neutral conversation: a list of dicts, one of
#   {"role": "user", "content": str}
#   {"role": "assistant", "text": str, "tool_calls": [ToolCall, ...]}
#   {"role": "tool", "tool_call_id": str, "name": str, "content": str, "is_error": bool}
Message = dict[str, Any]


# Generous because the slow case is legitimate, not a fault: a local server has
# to page the model into memory before the first token, and this agent's prompt
# carries the whole 43-tool schema for it to reason over. The wait costs nothing
# — the panel runs it on a worker thread — whereas cutting a working run short
# looks exactly like a broken addon. Override per-install with 'ai_timeout'.
DEFAULT_TIMEOUT = 600


def post_json(
    url: str, headers: dict[str, str], payload: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """POST ``payload`` as JSON and return the parsed JSON response.

    Raises ProviderError with the response body on any HTTP or transport error.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={**headers, "Content-Type": "application/json"},
    )
    _tap("request", url, payload)
    try:
        resp = _send(req, url, timeout)
    except ProviderError as e:
        _tap("error", url, {"error": str(e)})
        raise
    _tap("response", url, resp)
    return resp


def get_json(
    url: str, headers: dict[str, str] | None = None, timeout: float = 30
) -> dict[str, Any]:
    """GET and return the parsed JSON response. Same error contract as post_json.

    Used to list a provider's models (``GET {base_url}/models``), which Ollama,
    OpenAI and Gemini's OpenAI-compatible endpoint all serve the same way.
    """
    return _send(urllib.request.Request(url, method="GET", headers=headers or {}), url, timeout)


def _send(req: urllib.request.Request, url: str, timeout: float) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise ProviderError(f"HTTP {e.code} from {url}: {body[:800]}") from None
    except urllib.error.URLError as e:
        # A connect timeout arrives wrapped in URLError; a *read* timeout does
        # not, so it is handled separately below.
        if isinstance(e.reason, TimeoutError):
            raise _timed_out(url, timeout) from None
        raise ProviderError(f"could not reach {url}: {e.reason}") from None
    except TimeoutError:
        # Bare socket timeout while reading the response. TimeoutError is not a
        # URLError, so before this it escaped _send entirely and reached the user
        # as "TimeoutError: timed out" — true, and of no use to anyone.
        raise _timed_out(url, timeout) from None
    except (ValueError, KeyError) as e:
        raise ProviderError(f"bad response from {url}: {e}") from None


def _timed_out(url: str, timeout: float) -> ProviderError:
    return ProviderError(
        f"no response from {url} within {timeout:g}s. A local model that has to "
        "be paged into memory, or one reasoning over the whole tool list, can "
        "take longer than this — raise 'ai_timeout' in the settings file, or "
        "pick a smaller model."
    )
