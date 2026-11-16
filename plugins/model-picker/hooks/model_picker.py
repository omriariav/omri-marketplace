"""model-picker hook logic for Claude Code.

Usage: model_picker.py session|pretool PLUGIN_ROOT   (hook payload on stdin)

       model_picker.py route "<task>" [PLUGIN_ROOT]        (prints a JSON recommendation)

Settings come from environment variables, falling back to a JSON file of the same
keys at $MODEL_PICKER_CONFIG (default ~/.config/model-picker/config.json):

  MODEL_PICKER_MODE              advise (default) | enforce | off
  MODEL_PICKER_INJECT            1 (default) | 0   inject the guide at SessionStart
  MODEL_PICKER_LOCAL_HINT        enables the `local` tier; text telling Claude how to reach the
                                 local model (e.g. "model: haiku; the gateway maps haiku to qwen3")
  MODEL_PICKER_ROUTER            heuristic (default) | openai-decisions | google-decisions | command
  MODEL_PICKER_ROUTER_URL        base URL (OpenAI/Google default; any LiteLLM gateway works)
  MODEL_PICKER_ROUTER_MODEL      decision model (gpt-6-luna / gemini-decision-1 by default)
  MODEL_PICKER_ROUTER_API_KEY_ENV  env var holding the key (OPENAI_API_KEY / GEMINI_API_KEY)
  MODEL_PICKER_ROUTER_API_KEY_CMD  command printing the key (e.g. a Keychain lookup)
  MODEL_PICKER_ROUTER_HEADERS    JSON object of extra request headers
  MODEL_PICKER_ROUTER_METADATA   JSON object sent as the request `metadata` (OpenAI format)
  MODEL_PICKER_ROUTER_CMD        executable for the `command` router
  MODEL_PICKER_ROUTER_INPUT      description (default) | brief | full: what task text leaves the machine
  MODEL_PICKER_ROUTER_TIMEOUT    seconds (default 3)
  MODEL_PICKER_ROUTER_MIN_CONFIDENCE  below this the heuristic decides (default 0.5)
"""

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import routers  # noqa: E402

TIERS = ["local", "haiku", "sonnet", "opus", "fable"]

# Agent types that never take a model: fork inherits by design; the others are
# built-in helpers that choose their own model.
SKIP_TYPES = {"fork", "claude-code-guide", "statusline-setup", "web-fetch"}
# Read-only helpers: cheap-to-verify work.
READ_ONLY_TYPES = {"explore", "scout"}
PLANNING_TYPES = {"plan"}

FABLE_RE = re.compile(
    r"\b(major refactor(ing)?|large[- ]scale refactor(ing)?|re-?architect(ing|ure)?|architecture plan(ning)?|"
    r"migrate (the )?(entire|whole|all of the|full) \w+|deep research|scientific analysis|quantitative analysis|"
    r"lead (the |many |several )?(subagents|agents|team)|orchestrate (the |many |several )?(subagents|agents|workers))\b")
OPUS_RE = re.compile(
    r"\b(migrate|debug|debugging|root[- ]cause|optimi[sz]e|performance (issue|regression|problem|work)|"
    r"financial model|computer use|security review|design the (system|architecture))\b")
HAIKU_RE = re.compile(
    r"\b(find|search|grep|locate|list|where is|which files?|read the (logs?|output)|logs?|test output|"
    r"summari[sz]e|classify|categori[sz]e|tag|label|extract|triage|fetch|scan|count|inventory|explore)\b")
SONNET_RE = re.compile(
    r"\b(fix|bug|implement|add (a|an|the)|write (tests?|code|docs?|the docs)|feature|review|draft|document|"
    r"slides?|spreadsheet|investigate|refactor)\b")
# Intent to change files (vetoes haiku). Reporting verbs ("write a report") are not edits.
EDIT_RE = re.compile(
    r"\b(edit|modify|implement|refactor|fix|patch|apply (the |a )?(fix|patch|change)|"
    r"write (the )?(code|tests?|implementation|migration)|change the code|update the code)\b")

# Pure text work that needs no tools: candidates for a local model.
LOCAL_RE = re.compile(r"\b(summari[sz]e|classify|categori[sz]e|extract|translate|rephrase|sentiment)\b")
TOOL_RE = re.compile(r"\b(find|search|grep|locate|list|where is|which files?|logs?|test output|fetch|scan|explore|"
                     r"run|open|read the|in the (repo|codebase)|files?|code|module|function|class|parser|repo|"
                     r"release|branch|commit|tag|api|tests?|schema|database|rewrite|refactor|update|create|delete)\b")

REASONS = {
    "local": "pure text work that needs no tools or files",
    "haiku": "read/search/summarize side work that is cheap to verify",
    "sonnet": "well-scoped coding, review, or drafting",
    "opus": "complex engineering or judgment (default when unsure)",
    "fable": "major refactor/migration, architecture, deep research, or leading many agents",
}


def recommend(text, agent_type="", local=False):
    """Keyword heuristic: returns (tier, reason)."""
    t = (agent_type or "").lower().split(":")[-1]
    low = text.lower()
    if t in READ_ONLY_TYPES:
        return "haiku", f"`{agent_type}` is a read-only helper"
    if t in PLANNING_TYPES:
        return "opus", "planning needs judgment"
    if local and LOCAL_RE.search(low) and not TOOL_RE.search(low) and not EDIT_RE.search(low):
        return "local", REASONS["local"]
    edits = bool(EDIT_RE.search(low))
    if FABLE_RE.search(low):
        tier = "fable"
    elif HAIKU_RE.search(low) and not edits and not re.search(r"\b(debug|root[- ]cause)\b", low):
        tier = "haiku"
    elif OPUS_RE.search(low):
        tier = "opus"
    elif SONNET_RE.search(low):
        tier = "sonnet"
    else:
        tier = "opus"
    return tier, REASONS[tier]


def frontmatter(path):
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return None
    m = re.match(r"---\s*\n(.*?)\n---", head, re.DOTALL)
    return m.group(1) if m else None


def fm_field(fm, key):
    m = re.search(rf"^{key}:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", fm, re.MULTILINE)
    return m.group(1).strip() if m else None


def agent_files(agent_type, cwd):
    """Candidate definition files for agent_type, most authoritative first."""
    home = Path.home()
    if ":" in agent_type:
        plugin, name = agent_type.split(":", 1)
        root = home / ".claude/plugins"
        dirs = []
        # Authoritative: the install paths Claude Code recorded for this plugin.
        try:
            installed = json.loads((root / "installed_plugins.json").read_text(encoding="utf-8"))
            for key, entries in (installed.get("plugins") or {}).items():
                if key.split("@")[0] == plugin:
                    for e in entries if isinstance(entries, list) else [entries]:
                        if isinstance(e, dict) and e.get("installPath"):
                            dirs.append(Path(e["installPath"]) / "agents")
        except (OSError, ValueError, AttributeError):
            pass
        # Org-synced plugins: synced/<org>/<plugin>[~gN]/; newest generation only.
        synced = sorted((p for p in root.glob("synced/*/*") if p.is_dir()
                         and p.name.split("~")[0] == plugin),
                        key=lambda p: p.stat().st_mtime, reverse=True)
        dirs += [p / "agents" for p in synced[:1]]
    else:
        name = agent_type
        project = os.environ.get("CLAUDE_PROJECT_DIR") or cwd or "."
        dirs = [Path(project) / ".claude/agents", home / ".claude/agents"]
    return name, dirs


def pinned_model(agent_type, cwd):
    """Return the model pinned in the agent's definition, if one is found."""
    if not agent_type:
        return None
    name, dirs = agent_files(agent_type, cwd)
    for d in dirs:
        if not d.is_dir():
            continue
        exact = d / f"{name}.md"
        files = [exact] if exact.is_file() else []
        files += [f for f in sorted(d.glob("*.md")) if f != exact]
        for f in files:
            fm = frontmatter(f)
            if not fm:
                continue
            if (fm_field(fm, "name") or f.stem) != name:
                continue
            model = (fm_field(fm, "model") or "").lower()
            return model if model and model != "inherit" else None
    return None


def tier_of(model):
    model = (model or "").lower()
    for tier in TIERS:
        if tier in model:
            return tier
    return None


def emit(decision=None, reason=None, context=None):
    out = {"hookEventName": "PreToolUse"}
    if decision:
        out["permissionDecision"] = decision
    if reason:
        out["permissionDecisionReason"] = reason
    if context:
        out["additionalContext"] = context
    print(json.dumps({"hookSpecificOutput": out}))


def load_config():
    path = Path(os.environ.get("MODEL_PICKER_CONFIG") or Path.home() / ".config/model-picker/config.json")
    cfg = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cfg = {k: v for k, v in data.items() if k.startswith("MODEL_PICKER_")}
    except (OSError, ValueError, AttributeError):
        pass
    cfg.update({k: v for k, v in os.environ.items() if k.startswith("MODEL_PICKER_")})
    return cfg


def _num(cfg, key, default):
    value = cfg.get(key)
    if value is None or value == "":
        return float(default)
    return float(value)


def _enabled(cfg, key, default=True):
    value = cfg.get(key)
    if value is None or value == "":
        return default
    return str(value).strip().lower() not in ("0", "false", "off", "no")


def _json_obj(value):
    if isinstance(value, dict):
        return value
    return json.loads(value) if value else {}


def task_text(inp, cfg):
    """What leaves the machine when an external router is used."""
    level = str(cfg.get("MODEL_PICKER_ROUTER_INPUT") or "description").lower()
    desc = str(inp.get("description") or inp.get("name") or "")
    prompt = str(inp.get("prompt") or "")
    agent = f"Agent type: {inp.get('subagent_type') or 'general-purpose'}. "
    if level == "full":
        return f"{agent}Task: {desc}\n\n{prompt[:8000]}"
    if level == "brief":
        return f"{agent}Task: {desc}\n\n{prompt[:500]}"
    return f"{agent}Task: {desc}" if desc else agent.strip()


ROUTER_DEADLINE = 7.0  # seconds; stays under the 10 s hook timeout


def decide(inp, cfg, use_router=True):
    """Recommendation dict: tier, effort, reason, source. Router first (if configured), heuristic fallback."""
    local = bool(cfg.get("MODEL_PICKER_LOCAL_HINT"))
    agent_type = str(inp.get("subagent_type") or "")
    text = " ".join(str(inp.get(k) or "") for k in ("description", "prompt", "name"))
    tier, why = recommend(text, agent_type, local)
    fallback = {"tier": tier, "effort": "medium", "reason": why, "source": "heuristic"}
    name = str(cfg.get("MODEL_PICKER_ROUTER") or "heuristic").lower()
    router = routers.ROUTERS.get(name)
    if not use_router or not router or agent_type.lower().split(":")[-1] in (READ_ONLY_TYPES | PLANNING_TYPES):
        return fallback
    box = {}
    worker = threading.Thread(target=lambda: box.update(res=_route(router, name, inp, cfg, local)), daemon=True)
    worker.start()
    worker.join(ROUTER_DEADLINE)
    res = box.get("res")
    if res is None:
        fallback["source"] = "heuristic (router timed out)"
    elif isinstance(res, Exception):
        fallback["source"] = f"heuristic (router error: {type(res).__name__})"
    elif res["confidence"] < _num(cfg, "MODEL_PICKER_ROUTER_MIN_CONFIDENCE", 0.5):
        fallback["source"] = f"heuristic (router {res['source']} unsure: {res['confidence']:.2f})"
    else:
        return {"tier": res["tier"], "effort": res["effort"], "reason": routers.TIER_CRITERIA[res["tier"]],
                "source": f"{res['source']} ({res['confidence']:.2f})"}
    return fallback


def _route(router, name, inp, cfg, local):
    """Call the router; returns its result dict or the exception (never raises)."""
    try:
        rcfg = dict(cfg)
        rcfg["_timeout"] = min(_num(cfg, "MODEL_PICKER_ROUTER_TIMEOUT", 3), ROUTER_DEADLINE)
        rcfg["_headers"] = _json_obj(cfg.get("MODEL_PICKER_ROUTER_HEADERS"))
        rcfg["_metadata"] = _json_obj(cfg.get("MODEL_PICKER_ROUTER_METADATA"))
        if name != "command":
            key = os.environ.get(cfg.get("MODEL_PICKER_ROUTER_API_KEY_ENV") or routers.DEFAULT_KEY_ENV[name], "")
            if not key and cfg.get("MODEL_PICKER_ROUTER_API_KEY_CMD"):
                key = subprocess.run(cfg["MODEL_PICKER_ROUTER_API_KEY_CMD"], shell=True, capture_output=True,
                                     text=True, timeout=min(rcfg["_timeout"], 2), check=True).stdout.strip()
            if not key:
                raise ValueError("no API key")
            rcfg["_api_key"] = key
        tiers = [t for t in TIERS if local or t != "local"]
        return router(task_text(inp, cfg), tiers, rcfg)
    except Exception as exc:  # router trouble never blocks a launch
        return exc


def describe(rec, cfg):
    target = f"`{rec['tier']}`"
    if rec["tier"] == "local":
        target += f" ({cfg.get('MODEL_PICKER_LOCAL_HINT')})"
    return f"{target}, effort {rec['effort']}: {rec['reason']} [via {rec['source']}]"


def handle_agent(payload, mode, cfg):
    inp = payload.get("tool_input")
    if not isinstance(inp, dict):
        raise ValueError(f"unexpected tool_input type {type(inp).__name__}")
    agent_type = str(inp.get("subagent_type") or "")
    if agent_type.lower().split(":")[-1] in SKIP_TYPES:
        return
    model = inp.get("model")
    label = agent_type or "general-purpose"
    if not model and pinned_model(agent_type, payload.get("cwd")):
        return  # the agent definition already pins a model

    # An explicit model only needs a mismatch check: keep it local and instant (no router call).
    rec = decide(inp, cfg, use_router=not model)
    if model:
        chosen = tier_of(model)
        if chosen and abs(TIERS.index(chosen) - TIERS.index(rec["tier"])) >= 2:
            emit(context=f"model-picker: `{label}` launched on {chosen}, but the task reads as "
                         f"{describe(rec, cfg)}. Prefer that for similar launches unless you have a specific reason.")
        return

    if mode == "enforce":
        emit("deny", f"model-picker: launch `{label}` with an explicit `model`. Recommended: {describe(rec, cfg)}. "
                     "Re-issue the same call with that model, or pick another tier if you have a reason.")
    else:
        emit(context=f"model-picker: `{label}` launched with no explicit model. "
                     f"Recommended: {describe(rec, cfg)}. Pass `model` explicitly on future launches.")


def agent_calls(script):
    """Yield the argument text of each agent(...) call, honoring strings and nesting."""
    for m in re.finditer(r"\bagent\s*\(", script):
        i, depth, quote = m.end(), 1, None
        while i < len(script) and depth:
            c = script[i]
            if quote:
                if c == "\\":
                    i += 1
                elif c == quote:
                    quote = None
            elif c in "'\"`":
                quote = c
            elif c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            i += 1
        yield script[m.end():i - 1]


def strip_strings(code):
    return re.sub(r"`(?:\\.|[^`\\])*`|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"", "''", code, flags=re.DOTALL)


def handle_workflow(payload, mode, cfg):
    inp = payload.get("tool_input")
    script = inp.get("script") if isinstance(inp, dict) else None
    if not script:
        return
    calls = list(agent_calls(script))
    missing = sum(1 for args in calls if not re.search(r"\bmodel\s*:", strip_strings(args)))
    if not missing:
        return
    msg = (f"model-picker: {missing} of {len(calls)} agent() calls in this workflow set no `model`. "
           "Pass {model: 'haiku'} for search/read/summarize steps, 'sonnet' for scoped edits or review, "
           "'opus' for judgment, 'fable' only for the lead on major work.")
    if cfg.get("MODEL_PICKER_LOCAL_HINT"):
        msg += f" For tool-free text steps, use the local model: {cfg['MODEL_PICKER_LOCAL_HINT']}."
    if mode == "enforce":
        emit("deny", msg + " Add the model options and re-run.")
    else:
        emit(context=msg)


def handle_session(root, cfg):
    if not _enabled(cfg, "MODEL_PICKER_INJECT"):
        return
    try:
        raw = (Path(root) / "guide.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    m = re.search(r"<model-picker-guide>.*?</model-picker-guide>", raw, re.DOTALL)
    if not m:
        return
    context = m.group(0)
    if cfg.get("MODEL_PICKER_LOCAL_HINT"):
        context += ("\nLocal tier enabled: for pure text work that needs no tools or files (summarize, classify, "
                    f"extract, translate given text), use the local model: {cfg['MODEL_PICKER_LOCAL_HINT']}.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))


def main():
    event = sys.argv[1]
    cfg = load_config()
    if event == "route":
        task = (" ".join(sys.argv[2:3]) or sys.stdin.read()).strip()
        print(json.dumps(decide({"description": task[:200], "prompt": task}, cfg), indent=2))
        return
    root = sys.argv[2]
    raw = sys.stdin.read()
    mode = str(cfg.get("MODEL_PICKER_MODE") or "advise").strip().lower()
    if mode in ("false", "0", "no"):
        mode = "off"
    if mode == "off":
        return
    if event == "session":
        handle_session(root, cfg)
        return
    payload = json.loads(raw or "{}")
    tool = payload.get("tool_name")
    if tool in ("Agent", "Task"):
        handle_agent(payload, mode, cfg)
    elif tool == "Workflow":
        handle_workflow(payload, mode, cfg)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # never block a launch, but never fail silently either
        print(json.dumps({"systemMessage": f"model-picker: hook error ({type(exc).__name__}: {exc}); launch not checked."}))
