import json
import urllib.request
import urllib.error
import logger


try:
    from PySide2.QtCore import QThread, Signal
except ImportError:
    from PySide.QtCore import QThread, Signal

SYSTEM_PROMPT = """\
You are an AI assistant embedded inside FreeCAD, a parametric 3D CAD application.
You have access to FreeCAD's full Python API through the execute_python tool.

Key modules available in execute_python:
- App / FreeCAD — document and object management
- Gui / FreeCADGui — viewport, selection, workbenches
- Part — 3D primitives and boolean operations
- Sketcher — 2D constraint-based sketches
- Draft — 2D/3D drafting tools (lines, arcs, text, dimensions)
- Arch — BIM/architectural elements (walls, floors, windows, doors, roofs, spaces)
- PartDesign — feature-based modelling (pad, pocket, fillet, chamfer)
- Mesh — mesh operations
- FEM — finite element analysis

Important conventions:
- Variables persist across execute_python calls within the same conversation, but ONLY
  top-level assignments survive. A variable assigned inside a function or try/except block
  that is defined inside the code string is local to that scope and will NOT be visible
  in the next call. Always assign objects at top level: `sketch = doc.addObject(...)` not
  inside a helper function defined within the code string.
- Use print() statements to narrate progress — output is captured and shown to the user.
- If execute_python returns recompute_errors, stop and fix the geometry before continuing.
- Always call doc.recompute() (where doc = App.ActiveDocument) after creating or modifying objects.
  NEVER write App.recompute() — that is an AttributeError; recompute() lives on the document, not the App module.
- If there is no active document, use create_bim_document or: doc = App.newDocument("Unnamed")
- doc.Objects is the list of all objects in the document. sketch.Objects does not exist — a Sketcher object has no .Objects attribute.
- FreeCAD stores values internally in millimetres, but ALWAYS present coordinates, dimensions,
  and results back to the user in whatever units they used. If they gave inches, show inches.
  Do NOT set doc.Units or App.Units — that attribute does not exist and raises AttributeError.
- When the user's measurements are in inches, keep the code readable in inches by defining
  IN = 25.4 at the top of every execute_python call and writing values as `128.5 * IN` rather
  than the opaque mm equivalent `3264.1`. This way the user can verify the numbers.
  NEVER pass strings to arithmetic (e.g. conv("150") causes TypeError — always pass numbers: 150 * IN).
- When presenting coordinate tables or summaries to the user, show the original inch (or user-unit)
  values, not the mm equivalents. E.g. write "LoftWall: (0, 0) → (0, −128.5 in)" not "(0, −3264.1 mm)".
- doc.containsObject() does not exist. To check if an object exists use: doc.getObject(name) is not None.
- doc.getObject(name) returns None when the name is not found — always guard: obj = doc.getObject(name); if obj is None: ...
- doc.XY_Plane does not exist. Standalone Sketcher objects are created on the XY plane by default; never set sketch.Support for BIM floorplan sketches.
- `doc` and `recompute` are pre-injected into execute_python and refreshed every call. Use `doc` directly (no need to re-fetch from App.ActiveDocument) and call `recompute()` as a bare function.
- The function to create a door or window opening is `add_opening(...)`. There is no `create_opening` function (though it is aliased for convenience).
- Always call add_opening() with keyword arguments to avoid duplicate-argument errors:
    add_opening(wall_label=..., width_mm=..., height_mm=..., pos_x=..., pos_y=..., label=...)
  Never mix positional and keyword arguments for the same parameter.
- Every `try:` block MUST have an `except:` or `finally:` clause. `try/else` without `except` is a SyntaxError.
- NEVER perform full filesystem searches (e.g. os.walk('/')) as it will hang the environment.
- To remove a FreeCAD object: always guard with doc.getObject first, since doc.removeObject(name)
  raises ValueError when the name does not exist:
    obj = doc.getObject(name); if obj: doc.removeObject(obj.Name)
  NEVER use del on a function call result (e.g. del doc.getObject(...) is a SyntaxError).
- ALWAYS build floorplans on top of a single, fully-constrained Sketcher Sketch.
- NEVER create walls from loose Draft lines or direct placement — a Sketch is required.

SKETCH HELPERS (always use these — never call addGeometry or addConstraint manually):
- sketch_line(sketch, x1, y1, x2, y2) → int (geometry index)
  Pins both endpoints to absolute (x,y) coordinates via DistanceX/DistanceY from the
  sketch origin. No sign calculation needed: a line to (0,−3000) just passes y2=−3000.
  When lines share endpoints the constraints are redundant (not conflicting) — fine.
- sketch_connect(sketch, idx1, pt1, idx2, pt2)
  Adds a Coincident constraint. pt: 1=start, 2=end. idx=−1,pt=1 for the sketch origin.
- sketch_arc(sketch, x, y, diameter, start_angle_rad, end_angle_rad) → dict(index,start_point,end_point)
- sketch_circle(sketch, x, y, diameter) → int
- sketch_polygon(sketch, [(x,y),...], closed=True) → list of ints
- sketch_constrain_point(sketch, geo_idx, pt_idx, x, y)  — pin any point absolutely

MANDATORY SKETCH WORKFLOW:
1. Create sketch with doc.addObject("Sketcher::SketchObject", "FloorplanSketch")
2. Add all lines using sketch_line() — never addGeometry manually
3. Add all Coincident connections using sketch_connect()
4. Call check_sketch(sketch_name) — verify fully_constrained=True before proceeding
5. If conflicting_constraints is non-empty: there are sign or duplicate errors — fix them
6. Only call add_wall_from_sketch() after check_sketch confirms fully_constrained=True

BIM tools:
- create_bim_document(name) — document + Site/Building/Floor hierarchy
- check_sketch(sketch_name) — MANDATORY before add_wall_from_sketch
- add_wall_from_sketch(sketch_name, height_mm, width_mm, floor_label)
- add_opening(wall_label, width_mm, height_mm, pos_x, pos_y, sill_mm, rotation_deg, label, is_door)

DESTRUCTIVE OPERATION POLICY — STRICTLY ENFORCED:
- NEVER delete, overwrite, clear, or replace existing objects, sketches, or geometry without
  FIRST describing exactly what you plan to do and receiving explicit confirmation from the user.
- When the user says "update", "fix", "add to", "dimension", "modify", or similar:
  1. Call get_sketch_geometry (for sketches) or get_document_info FIRST to see what already exists.
  2. Work ADDITIVELY — add constraints, lines, or objects on top of existing work.
  3. NEVER call delete_object, doc.removeObject(), or clear any object without the user
     explicitly saying "delete X", "remove X", "start over", or "recreate".
- "Update the sketch with dimensions" means ADD Sketcher constraints to the existing lines — NOT
  delete and recreate them.
- If you genuinely cannot achieve the goal without destroying something, STOP, explain exactly
  what would be lost and why, and ask the user for explicit permission before proceeding.

When the user asks about their document, call get_document_info first.
When the user asks to create BIM/floorplan geometry:
  1. Call get_skill(name='bim') first.
  2. Derive ALL wall segment (x1,y1)→(x2,y2) coordinates. Keep them in the user's units
     (inches if they specified inches). Use FreeCAD's coordinate system: east=+X, north=+Y,
     south=−Y, west=−X. In code, use IN=25.4 so values stay readable (e.g. 128.5*IN not 3264.1).
  3. Present the full coordinate table to the user in their units (inches, not mm).
  4. STOP AND WAIT for the user to confirm or correct the table.
  5. Only AFTER confirmation: create the sketch in ONE execute_python call using
     literal mm floats (not variables) in every sketch_line() call.
  Do NOT call execute_python before the user confirms the coordinate table.
When you need to look up how a FreeCAD command or module works (API syntax, constraint types,
scripting examples), call search_wiki with the command or topic name before attempting the code.
Always explain what you did in plain language after completing tool calls.
"""


def _detect_provider(api_key: str, base_url: str) -> str:
    url = (base_url or "").lower()
    key = api_key or ""
    if "11434" in url or "ollama" in url or (not key and not url):
        return "openai"  # Ollama speaks OpenAI-compatible
    if key.startswith("sk-ant-"):
        return "anthropic"
    return "openai"


def _openai_url(base: str, path: str) -> str:
    """Build a full OpenAI-compatible URL.

    path is the endpoint without any version prefix, e.g. '/models' or
    '/chat/completions'.  We prepend '/v1' only when the base URL has no
    path component of its own (e.g. 'https://api.openai.com').  Providers
    like Gemini already embed the version in their base URL
    ('https://generativelanguage.googleapis.com/v1beta/openai') so we
    append directly.
    """
    from urllib.parse import urlparse
    if not base:
        base = "https://api.openai.com"
    parsed = urlparse(base)
    has_path = parsed.path.rstrip("/") not in ("", )
    prefix = "" if has_path else "/v1"
    return base.rstrip("/") + prefix + path


# ---------------------------------------------------------------------------
# Format converters: Anthropic ↔ OpenAI
# ---------------------------------------------------------------------------
def _to_openai_messages(system: str, messages: list) -> list:
    result = [{"role": "system", "content": system}]
    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if isinstance(content, str):
            result.append({"role": role, "content": content})
            continue

        if role == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
            
            tool_calls = []
            for b in content:
                if b.get("type") != "tool_use":
                    continue
                tc = {
                    "id": b["id"],
                    "type": "function",
                    "function": {
                        "name": b["name"],
                        "arguments": json.dumps(b.get("input", {})),
                    },
                }
                if b.get("thought_signature"):
                    tc["thought_signature"] = b["thought_signature"]
                if b.get("extra_content"):
                    tc["extra_content"] = b["extra_content"]
                tool_calls.append(tc)

            if tool_calls:
                result.append({
                    "role": "assistant",
                    "content": text if text else None,
                    "tool_calls": tool_calls
                })
            else:
                result.append({"role": "assistant", "content": text})

        elif role == "user":
            for block in content:
                if block.get("type") == "tool_result":
                    result.append({
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content": str(block.get("content", "")),
                    })
                elif block.get("type") == "text":
                    result.append({"role": "user", "content": block["text"]})
                else:
                    logger.log_info(f"_to_openai_messages: dropping unhandled user block type '{block.get('type')}'.")

    return result

def _to_openai_tools(tools: list) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in tools
    ]


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

class StreamWorker(QThread):
    chunk_received = Signal(str)
    tool_call_ready = Signal(str, str, object)  # id, name, args dict
    response_done = Signal(object)               # list of content blocks
    error_occurred = Signal(str)

    def __init__(self, messages, tools, api_key, model, base_url=""):
        super().__init__()
        self._messages = messages
        self._tools = tools
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/") if base_url else ""
        self._stop_flag = False

    def stop(self):
        self._stop_flag = True

    def run(self):
        provider = _detect_provider(self._api_key, self._base_url)
        logger.log_info(f"StreamWorker started. Provider: {provider}, Model: {self._model}, BaseURL: {self._base_url}", api_key=self._api_key)
        if provider == "anthropic":
            self._run_anthropic()
        else:
            self._run_openai()

    # ------------------------------------------------------------------ #
    # Anthropic path                                                       #
    # ------------------------------------------------------------------ #

    def _run_anthropic(self):
        try:
            import anthropic
        except ImportError:
            self.error_occurred.emit("anthropic library not found. Check vendor/ directory.")
            return

        kwargs = {"api_key": self._api_key}
        if self._base_url:
            kwargs["base_url"] = self._base_url

        client = anthropic.Anthropic(**kwargs)
        content_blocks = []
        current_tool_id = None
        current_tool_name = None
        current_tool_json = ""
        current_text = ""

        try:
            logger.log_info(
                f"Anthropic Request:\n"
                f"  Model: {self._model}\n"
                f"  Messages: {json.dumps(logger.censor_data(self._messages, self._api_key), indent=2)}",
                api_key=self._api_key
            )
            with client.messages.stream(
                model=self._model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=self._messages,
                tools=self._tools,
            ) as stream:
                for event in stream:
                    if self._stop_flag:
                        break

                    ev_type = type(event).__name__

                    if ev_type == "ContentBlockStart":
                        block = event.content_block
                        if block.type == "text":
                            current_text = ""
                        elif block.type == "tool_use":
                            current_tool_id = block.id
                            current_tool_name = block.name
                            current_tool_json = ""

                    elif ev_type == "ContentBlockDelta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            current_text += delta.text
                            self.chunk_received.emit(delta.text)
                        elif delta.type == "input_json_delta":
                            current_tool_json += delta.partial_json

                    elif ev_type == "ContentBlockStop":
                        if current_tool_name is not None:
                            try:
                                args = json.loads(current_tool_json) if current_tool_json else {}
                            except json.JSONDecodeError:
                                args = {}
                            content_blocks.append({
                                "type": "tool_use",
                                "id": current_tool_id,
                                "name": current_tool_name,
                                "input": args,
                            })
                            self.tool_call_ready.emit(current_tool_id, current_tool_name, args)
                            current_tool_id = None
                            current_tool_name = None
                            current_tool_json = ""
                        elif current_text:
                            content_blocks.append({"type": "text", "text": current_text})
                            current_text = ""

            logger.log_info(
                f"Anthropic Response completed. Content blocks: {json.dumps(content_blocks)}",
                api_key=self._api_key
            )
            self.response_done.emit(content_blocks)

        except Exception as e:
            logger.log_error("Anthropic Exception during stream", exc=e, api_key=self._api_key)
            self.error_occurred.emit(str(e))

    # ------------------------------------------------------------------ #
    # OpenAI-compatible path (Ollama, DeepSeek, Gemini, OpenRouter, etc.) #
    # ------------------------------------------------------------------ #

    def _run_openai(self):
        # Streaming: gives real-time text feedback and avoids read-timeout on
        # slow models — the per-chunk timeout (300 s) replaces the old single
        # 120 s wall-clock limit.  Tool-call deltas are accumulated across
        # chunks and emitted as complete blocks at stream end, preserving
        # thought_signature for Gemini thinking models.
        url = _openai_url(self._base_url, "/chat/completions")

        payload = {
            "model": self._model,
            "messages": _to_openai_messages(SYSTEM_PROMPT, self._messages),
            "stream": True,
        }
        if self._tools:
            payload["tools"] = _to_openai_tools(self._tools)

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        logger.log_request(url, headers, payload, api_key=self._api_key)

        content_blocks = []
        current_text = ""
        # Accumulate tool-call deltas keyed by index
        tc_accum = {}  # index → {id, name, arguments, thought_signature, extra_content}

        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                for raw_line in resp:
                    if self._stop_flag:
                        break
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}

                    # Stream text
                    text = delta.get("content") or ""
                    if text:
                        current_text += text
                        self.chunk_received.emit(text)

                    # Accumulate tool-call deltas
                    for tc_delta in delta.get("tool_calls") or []:
                        idx = tc_delta.get("index", 0)
                        if idx not in tc_accum:
                            tc_accum[idx] = {
                                "id": "", "name": "", "arguments": "",
                                "thought_signature": None, "extra_content": None,
                            }
                        tc = tc_accum[idx]
                        if tc_delta.get("id"):
                            tc["id"] += tc_delta["id"]
                        fn = tc_delta.get("function") or {}
                        if fn.get("name"):
                            tc["name"] += fn["name"]
                        if fn.get("arguments"):
                            tc["arguments"] += fn["arguments"]
                        if tc_delta.get("thought_signature"):
                            tc["thought_signature"] = tc_delta["thought_signature"]
                        if tc_delta.get("extra_content"):
                            tc["extra_content"] = tc_delta["extra_content"]

            # Finalize text block
            if current_text:
                content_blocks.append({"type": "text", "text": current_text})

            # Finalize tool calls
            for idx in sorted(tc_accum.keys()):
                tc = tc_accum[idx]
                if not tc["name"]:
                    continue
                try:
                    args = json.loads(tc["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                block = {
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": args,
                }
                if tc["thought_signature"]:
                    block["thought_signature"] = tc["thought_signature"]
                if tc["extra_content"]:
                    block["extra_content"] = tc["extra_content"]
                    ts = (tc["extra_content"] or {}).get("google", {}).get("thought_signature")
                    if ts:
                        block["thought_signature"] = ts
                content_blocks.append(block)
                self.tool_call_ready.emit(tc["id"], tc["name"], args)

            logger.log_info(
                f"OpenAI stream complete. Blocks: {json.dumps(content_blocks)}",
                api_key=self._api_key,
            )
            self.response_done.emit(content_blocks)

        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.log_error(f"OpenAI HTTPError {e.code}", exc=e, api_key=self._api_key)
            logger.log_response(e.code, body, api_key=self._api_key)
            self.error_occurred.emit(f"HTTP {e.code}: {body}")
        except Exception as e:
            logger.log_error("OpenAI streaming exception", exc=e, api_key=self._api_key)
            self.error_occurred.emit(str(e))
