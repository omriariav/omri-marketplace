"""model-picker local tier: a minimal MCP server (stdio, newline-delimited JSON-RPC) for Ollama.

Tools:
  ask_local(prompt, system?, max_tokens?, model?)  text in, text out on a local model, $0
  list_local_models()                              installed Ollama models

Settings (environment, or ~/.config/model-picker/config.json like the hooks):
  MODEL_PICKER_OLLAMA_MODEL    model to use (e.g. qwen2.5:7b-instruct); also turns on the local tier
  MODEL_PICKER_OLLAMA_URL      default http://localhost:11434
  MODEL_PICKER_OLLAMA_TIMEOUT  seconds per call, default 120
  MODEL_PICKER_OLLAMA          0 hides the tools entirely

Every answer ends with a stamp naming the model that produced it, and each call is logged to the
model-picker ledger (kind "local": model, token counts, seconds; never the prompt or answer text).
Stdlib only.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import ledger  # noqa: E402
from model_picker import load_config  # noqa: E402

try:
    VERSION = json.loads((Path(__file__).resolve().parent.parent / ".claude-plugin/plugin.json").read_text())["version"]
except (OSError, ValueError, KeyError):
    VERSION = "0"
DEFAULT_URL = "http://localhost:11434"

TOOLS = [
    {
        "name": "ask_local",
        "description": (
            "Run a pure text task on a local model via Ollama, at $0 and without sending data off this "
            "machine. Use only for tool-free text work on text you pass in: summarize, classify, extract, "
            "translate, rephrase. The model cannot read files, browse, or call tools, and small local models "
            "are weak at reasoning, so check the answer. The reply ends with a [model-picker] stamp naming "
            "the model that answered."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The full task, including all text to work on."},
                "system": {"type": "string", "description": "Optional system instruction."},
                "max_tokens": {"type": "integer", "minimum": 1, "maximum": 8192,
                               "description": "Maximum tokens to generate (default 1024)."},
                "model": {"type": "string", "description": "Override the configured Ollama model."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "list_local_models",
        "description": "List the models installed in the local Ollama server.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _url(cfg):
    return str(cfg.get("MODEL_PICKER_OLLAMA_URL") or DEFAULT_URL).rstrip("/")


def _http(method, url, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def installed_models(cfg):
    return [m.get("name") for m in _http("GET", f"{_url(cfg)}/api/tags").get("models", []) if m.get("name")]


def ask_local(cfg, args):
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("`prompt` is required")
    model = args.get("model") or cfg.get("MODEL_PICKER_OLLAMA_MODEL")
    if not model:
        try:
            names = ", ".join(installed_models(cfg)) or "none installed"
        except (OSError, ValueError):
            names = "Ollama not reachable"
        raise ValueError(f"no local model configured: set MODEL_PICKER_OLLAMA_MODEL (installed: {names})")
    messages = ([{"role": "system", "content": str(args["system"])}] if args.get("system") else [])
    messages.append({"role": "user", "content": prompt})
    max_tokens = min(max(int(args.get("max_tokens") or 1024), 1), 8192)
    timeout = float(cfg.get("MODEL_PICKER_OLLAMA_TIMEOUT") or 120)
    started = time.monotonic()
    record = {"kind": "local", "model": f"ollama:{model}"}
    try:
        data = _http("POST", f"{_url(cfg)}/api/chat", {
            "model": model, "messages": messages, "stream": False,
            "options": {"num_predict": max_tokens, "temperature": 0.2}}, timeout=timeout)
    except Exception as exc:
        ledger.append(cfg, {**record, "error": type(exc).__name__, "seconds": round(time.monotonic() - started, 2)})
        raise
    seconds = round(time.monotonic() - started, 2)
    text = ((data.get("message") or {}).get("content") or "").strip()
    tin, tout = int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0)
    haiku = ledger.message_cost({"input": tin, "output": tout, "cache_read": 0, "cache_write": 0,
                                 "cache_write_1h": 0}, "haiku", ledger.price_table(cfg)) or 0.0
    ledger.append(cfg, {**record, "input": tin, "output": tout, "seconds": seconds, "haiku_equiv": round(haiku, 6),
                        "truncated": data.get("done_reason") == "length"})
    note = " · hit max_tokens" if data.get("done_reason") == "length" else ""
    return f"{text}\n\n[model-picker] answered by ollama:{model} · {tin} in / {tout} out tokens · $0 · {seconds}s{note}"


def call_tool(cfg, name, args):
    if args is not None and not isinstance(args, dict):
        raise ValueError("`arguments` must be an object")
    if name == "ask_local":
        return ask_local(cfg, args or {})
    if name == "list_local_models":
        names = installed_models(cfg)
        current = cfg.get("MODEL_PICKER_OLLAMA_MODEL")
        return "\n".join(f"{n}{'  (configured)' if n == current else ''}" for n in names) or "No models installed."
    raise ValueError(f"unknown tool {name!r}")


def handle(msg, cfg):
    """Return the JSON-RPC response for one request, or None for notifications."""
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return None
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "params must be an object"}}
    enabled = str(cfg.get("MODEL_PICKER_OLLAMA", "1")).strip().lower() not in ("0", "false", "off", "no")
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "model-picker-local", "version": VERSION}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS if enabled else []}
    elif method == "tools/call":
        if not enabled:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "local tools disabled"}}
        try:
            text = call_tool(cfg, params.get("name"), params.get("arguments"))
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        except urllib.error.HTTPError as exc:  # Ollama is up but refused: wrong model, OOM, ...
            try:
                detail = json.loads(exc.read().decode() or "{}").get("error") or exc.reason
            except (OSError, ValueError, AttributeError):
                detail = exc.reason
            hint = " Pull it with `ollama pull <model>` or check MODEL_PICKER_OLLAMA_MODEL." if exc.code == 404 else ""
            result = {"content": [{"type": "text", "text": f"Ollama returned HTTP {exc.code}: {detail}.{hint}"}],
                      "isError": True}
        except urllib.error.URLError as exc:
            result = {"content": [{"type": "text", "text": f"Ollama not reachable at {_url(cfg)} ({exc.reason}). "
                                                          "Start it with `ollama serve`."}], "isError": True}
        except Exception as exc:
            result = {"content": [{"type": "text", "text": f"ask_local failed: {exc}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    cfg = load_config()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            try:
                reply = handle(msg, cfg) if isinstance(msg, dict) else None
            except Exception as exc:  # never let one bad request kill the server
                mid = msg.get("id") if isinstance(msg, dict) else None
                reply = None if mid is None else {"jsonrpc": "2.0", "id": mid,
                                                  "error": {"code": -32603, "message": f"internal error: {exc}"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
