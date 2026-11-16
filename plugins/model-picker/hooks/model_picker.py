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
  MODEL_PICKER_ROUTER_API_KEY_ENV  env var holding the key (OPENAI_API_KEY / GEMINI_API_KEY);
                                 "none" sends no auth header (private-network gateways)
  MODEL_PICKER_ROUTER_API_KEY_CMD  command printing the key (e.g. a Keychain lookup)
  MODEL_PICKER_ROUTER_HEADERS    JSON object of extra request headers
  MODEL_PICKER_ROUTER_METADATA   JSON object sent as the request `metadata` (OpenAI format)
  MODEL_PICKER_ROUTER_CMD        executable for the `command` router
  MODEL_PICKER_ROUTER_INPUT      description (default) | brief | full: what task text leaves the machine
  MODEL_PICKER_ROUTER_TIMEOUT    seconds (default 3)
  MODEL_PICKER_ROUTER_MIN_CONFIDENCE  below this the heuristic decides (default 0.5)
  MODEL_PICKER_ROUTER_ON_EXPLICIT  0 (default) | 1   also ask the router about launches that set a model
  MODEL_PICKER_LOG               1 (default) | 0   local decision/usage ledger (see ledger.py)
  MODEL_PICKER_STATE_DIR         where the ledger lives (default ~/.config/model-picker)
  MODEL_PICKER_VERBOSE           0 (default) | 1   one-line note per launch and per finished subagent
  MODEL_PICKER_PRICES            JSON price overrides per tier ($/M tokens)
  MODEL_PICKER_STAMP             0 (default) | 1   stamp each subagent result with the model that actually ran
  MODEL_PICKER_STAMP_REPLIES     0 (default) | 1   ask Claude to end every reply with its model id
  MODEL_PICKER_OLLAMA_MODEL      enables the local tier through the bundled `ask_local` MCP tool
  MODEL_PICKER_OLLAMA_URL        Ollama base URL (default http://localhost:11434)
  MODEL_PICKER_TIERS             tiers this environment can launch, e.g. "haiku,sonnet,opus" when
                                 Fable isn't enabled; others are never recommended (default: all)

Also: model_picker.py report [today|24h|7d|30d|all] [--json]
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger  # noqa: E402
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
    r"\b(find|search|grep|locate|list|where is|which files?|read|look up|lookup|first line|show me|print|"
    r"check (if|whether)|logs?|test output|summari[sz]e|classify|categori[sz]e|tag|label|extract|triage|"
    r"fetch|scan|count|inventory|explore)\b")
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
                     r"release|branch|commit|tag|api|tests?|schema|database|rewrite|refactor|update|create|delete|"
                     r"readme|pr|pull request|issue|diff|ticket|docs?|document|page|site|url|link|"
                     r"spreadsheet|sheet|slack|thread|channel|email|inbox)\b"
                     r"|https?://|\w/\w|\b[\w-]+\.[a-z0-9]{1,5}\b")  # URLs, paths, file names

UNSURE_REASON = "no clear signal in the task text; opus is the guide's default when unsure"
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
        return tier, UNSURE_REASON
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
    """Claude tier named in a model id/alias. "local" is never a launchable `model` value, so a
    name that merely contains "local" (e.g. my-local-llama) is not treated as the local tier."""
    model = (model or "").lower()
    for tier in CLAUDE_TIERS:
        if tier in model:
            return tier
    return None


def emit(decision=None, reason=None, context=None, system=None):
    out = {"hookEventName": "PreToolUse"}
    if decision:
        out["permissionDecision"] = decision
    if reason:
        out["permissionDecisionReason"] = reason
    if context:
        out["additionalContext"] = context
    body = {"hookSpecificOutput": out} if len(out) > 1 else {}
    if system:
        body["systemMessage"] = system
    if body:
        print(json.dumps(body))


ASK_LOCAL_TOOL = "mcp__plugin_model-picker_local__ask_local"


def local_hint(cfg):
    """How Claude reaches the local model, or None when the local tier is off."""
    if cfg.get("MODEL_PICKER_LOCAL_HINT"):
        return str(cfg["MODEL_PICKER_LOCAL_HINT"])
    if cfg.get("MODEL_PICKER_OLLAMA_MODEL"):
        return (f"call the `{ASK_LOCAL_TOOL}` tool directly instead of launching an agent "
                f"(Ollama {cfg['MODEL_PICKER_OLLAMA_MODEL']}, $0; text in, text out, no tools or files)")
    return None


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


CLAUDE_TIERS = ["haiku", "sonnet", "opus", "fable"]
EFFORT_ORDER = ["low", "medium", "high"]


def _tiers_and_warning(cfg):
    local = bool(local_hint(cfg))
    everything = [t for t in TIERS if local or t != "local"]
    raw = cfg.get("MODEL_PICKER_TIERS")
    if not raw:
        return everything, None
    items = raw if isinstance(raw, list) else str(raw).split(",")
    wanted = {str(t).strip().lower() for t in items if str(t).strip()}
    tiers = [t for t in TIERS if t in wanted and (t != "local" or local)]
    if any(t in CLAUDE_TIERS for t in tiers):
        return tiers, None
    # Fail open (a launch must always have a model), but say so: a typo must not silently re-enable Fable.
    return everything, (f"MODEL_PICKER_TIERS={raw!r} names no Claude tier (haiku, sonnet, opus, fable), so it is "
                        "ignored and all tiers are allowed. Fix the setting if some models are not available.")


def available_tiers(cfg):
    """Tiers this environment can actually launch, in TIERS order."""
    return _tiers_and_warning(cfg)[0]


def local_enabled(cfg):
    return "local" in available_tiers(cfg)


def fit_to_available(rec, cfg):
    """Move a recommendation to the nearest available tier.

    Prefer the next stronger available tier, keeping effort: a weaker model at high effort is a poor
    stand-in for missing capability in the middle of the range. Only when nothing stronger exists
    (e.g. Fable not enabled) step down, raising effort to high: the guide's "change effort before
    changing models".
    """
    tiers = available_tiers(cfg)
    if rec["tier"] in tiers:
        return rec
    want = rec["tier"]
    if want == "local":
        target = next((t for t in CLAUDE_TIERS if t in tiers), tiers[-1])
        note = "local tier not available here; cheapest available tier instead"
        effort = rec["effort"]
    else:
        idx = CLAUDE_TIERS.index(want)
        higher = [t for t in CLAUDE_TIERS[idx + 1:] if t in tiers]
        lower = [t for t in CLAUDE_TIERS[:idx] if t in tiers]
        if higher:
            target, effort = higher[0], rec["effort"]
            note = f"{want} is not available here; {target} instead"
        else:
            target, effort = lower[-1], "high"
            note = f"{want} is not available here; {target} at high effort instead"
    return {**rec, "tier": target, "effort": effort, "reason": f"{rec['reason']} ({note})", "capped_from": want}


def decide(inp, cfg, use_router=True):
    """Recommendation dict: tier, effort, reason, source, limited to the available tiers."""
    return fit_to_available(_decide(inp, cfg, use_router), cfg)


def _decide(inp, cfg, use_router=True):
    """Router first (if configured), heuristic fallback; may name a tier that is not available."""
    local = local_enabled(cfg)
    agent_type = str(inp.get("subagent_type") or "")
    text = " ".join(str(inp.get(k) or "") for k in ("description", "prompt", "name"))
    tier, why = recommend(text, agent_type, local)
    fallback = {"tier": tier, "effort": "medium", "reason": why, "source": "heuristic",
                "confidence": None, "router_ms": None, "unsure": why == UNSURE_REASON}
    name = str(cfg.get("MODEL_PICKER_ROUTER") or "heuristic").lower()
    router = routers.ROUTERS.get(name)
    if not use_router or not router or agent_type.lower().split(":")[-1] in (READ_ONLY_TYPES | PLANNING_TYPES):
        return fallback
    box = {}
    worker = threading.Thread(target=lambda: box.update(res=_route(router, name, inp, cfg, local)), daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(ROUTER_DEADLINE)
    fallback["router_ms"] = int((time.monotonic() - started) * 1000)
    res = box.get("res")
    if res is None:
        fallback["source"] = "heuristic (router timed out)"
    elif isinstance(res, Exception):
        fallback["source"] = f"heuristic (router error: {type(res).__name__})"
    elif res["confidence"] < _num(cfg, "MODEL_PICKER_ROUTER_MIN_CONFIDENCE", 0.5):
        fallback["source"] = f"heuristic (router {res['source']} unsure: {res['confidence']:.2f})"
    else:
        return {"tier": res["tier"], "effort": res["effort"], "reason": routers.TIER_CRITERIA[res["tier"]],
                "source": f"{res['source']} ({res['confidence']:.2f})", "confidence": res["confidence"],
                "router_ms": fallback["router_ms"], "unsure": False}
    return fallback


def _route(router, name, inp, cfg, local):
    """Call the router; returns its result dict or the exception (never raises)."""
    try:
        rcfg = dict(cfg)
        rcfg["_timeout"] = min(_num(cfg, "MODEL_PICKER_ROUTER_TIMEOUT", 3), ROUTER_DEADLINE)
        rcfg["_headers"] = _json_obj(cfg.get("MODEL_PICKER_ROUTER_HEADERS"))
        rcfg["_metadata"] = _json_obj(cfg.get("MODEL_PICKER_ROUTER_METADATA"))
        key_env = str(cfg.get("MODEL_PICKER_ROUTER_API_KEY_ENV") or routers.DEFAULT_KEY_ENV.get(name, ""))
        if name != "command" and key_env.lower() == "none":
            rcfg["_api_key"] = ""  # gateway on a private network that needs no key
        elif name != "command":
            key = os.environ.get(key_env, "")
            if not key and cfg.get("MODEL_PICKER_ROUTER_API_KEY_CMD"):
                key = subprocess.run(cfg["MODEL_PICKER_ROUTER_API_KEY_CMD"], shell=True, capture_output=True,
                                     text=True, timeout=min(rcfg["_timeout"], 2), check=True).stdout.strip()
            if not key:
                raise ValueError("no API key")
            rcfg["_api_key"] = key
        return router(task_text(inp, cfg), available_tiers(cfg), rcfg)
    except Exception as exc:  # router trouble never blocks a launch
        return exc


def describe(rec, cfg):
    target = f"`{rec['tier']}`"
    if rec["tier"] == "local":
        target += f" ({local_hint(cfg)})"
    return f"{target}, effort {rec['effort']}: {rec['reason']} [via {rec['source']}]"


def _short(text, n=120):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def log_decision(cfg, payload, inp, outcome, chosen=None, rec=None, session_model=None, pinned=None):
    """One ledger line per checked launch. Description only (local file); never the prompt."""
    chosen_tier = ledger.tier_of(chosen) if chosen else None
    entry = {
        "kind": "decision", "session": payload.get("session_id"), "tool_use_id": payload.get("tool_use_id"),
        "tool": payload.get("tool_name"), "agent_type": str(inp.get("subagent_type") or "general-purpose"),
        "description": _short(inp.get("description") or inp.get("name")), "outcome": outcome,
        "chosen": chosen, "chosen_tier": chosen_tier, "session_model": session_model, "pinned": pinned,
    }
    if rec:
        entry.update({"rec_tier": rec["tier"], "rec_effort": rec["effort"], "source": rec["source"],
                      "confidence": rec.get("confidence"), "router_ms": rec.get("router_ms"),
                      "unsure": bool(rec.get("unsure")),
                      # A fallback guess can't judge an explicit choice: leave "followed" unknown.
                      "followed": None if (not chosen_tier or rec.get("unsure")) else
                      (chosen_tier == rec["tier"] or (rec["tier"] == "local" and chosen_tier == "haiku"))})
    ledger.append(cfg, entry)


def verbose_note(cfg, label, chosen, rec):
    if not _enabled(cfg, "MODEL_PICKER_VERBOSE", default=False):
        return None
    conf = f" {rec['confidence']:.2f}" if isinstance(rec.get("confidence"), (int, float)) else ""
    ms = f", {rec['router_ms']} ms" if rec.get("router_ms") is not None else ""
    src = str(rec["source"]).split(" (")[0].split(":")[-1]
    picked = f"on {chosen}" if chosen else "no model set"
    return f"model-picker: {label} {picked} → recommends {rec['tier']}/{rec['effort']} ({src}{conf}{ms})"


def handle_agent(payload, mode, cfg):
    inp = payload.get("tool_input")
    if not isinstance(inp, dict):
        raise ValueError(f"unexpected tool_input type {type(inp).__name__}")
    agent_type = str(inp.get("subagent_type") or "")
    label = agent_type or "general-purpose"
    if agent_type.lower().split(":")[-1] in SKIP_TYPES:
        log_decision(cfg, payload, inp, "skipped")
        return
    model = inp.get("model")
    pinned = pinned_model(agent_type, payload.get("cwd"))
    if pinned and not model:
        pinned_tier = tier_of(pinned)
        unavailable = bool(pinned_tier) and pinned_tier not in available_tiers(cfg)
        # A denied launch never ran: log it as "denied" so the retry isn't counted twice.
        log_decision(cfg, payload, inp, "denied" if unavailable and mode == "enforce" else "pinned",
                     chosen=pinned, pinned=pinned)
        if unavailable:
            alt = fit_to_available({"tier": pinned_tier, "effort": "medium", "reason": "pinned by the agent definition"},
                                   cfg)
            msg = (f"model-picker: `{label}` is pinned to {pinned} in its definition, which is not available in "
                   f"this environment. Pass model: \"{alt['tier']}\" explicitly to override.")
            emit("deny", msg) if mode == "enforce" else emit(context=msg)
        return  # the agent definition already pins a model

    # An explicit model is judged by the keyword heuristic (instant) unless MODEL_PICKER_ROUTER_ON_EXPLICIT
    # asks the router to judge it too: ~0.7s per launch, but every launch gets a real verdict.
    chosen_unavailable = bool(model) and bool(tier_of(model)) and tier_of(model) not in available_tiers(cfg)
    # No router round trip for a launch that is about to be redirected anyway.
    rec = decide(inp, cfg, use_router=not chosen_unavailable and (
        not model or _enabled(cfg, "MODEL_PICKER_ROUTER_ON_EXPLICIT", default=False)))
    session_model = ledger.last_model(payload.get("transcript_path"))
    outcome = ("denied" if mode == "enforce" and (not model or chosen_unavailable)
               else "explicit" if model else "missing")
    log_decision(cfg, payload, inp, outcome, chosen=model or session_model, rec=rec, session_model=session_model,
                 pinned=pinned)
    note = verbose_note(cfg, label, model, rec)
    if model:
        chosen = tier_of(model)
        if chosen and chosen not in available_tiers(cfg):
            msg = (f"model-picker: `{label}` was launched on {chosen}, which is not available in this "
                   f"environment (MODEL_PICKER_TIERS). Use {describe(rec, cfg)} instead.")
            if mode == "enforce":
                emit("deny", msg + " Re-issue the call with an available model.", system=note)
            else:
                emit(context=msg, system=note)
            return
        if chosen and not rec.get("unsure") and abs(TIERS.index(chosen) - TIERS.index(rec["tier"])) >= 2:
            emit(context=f"model-picker: `{label}` launched on {chosen}, but the task reads as "
                         f"{describe(rec, cfg)}. Prefer that for similar launches unless you have a specific reason.",
                 system=note)
        else:
            emit(system=note)
        return

    if mode == "enforce":
        emit("deny", f"model-picker: launch `{label}` with an explicit `model`. Recommended: {describe(rec, cfg)}. "
                     "Re-issue the same call with that model, or pick another tier if you have a reason.", system=note)
    else:
        emit(context=f"model-picker: `{label}` launched with no explicit model. "
                     f"Recommended: {describe(rec, cfg)}. Pass `model` explicitly on future launches.", system=note)


def handle_subagent_stop(payload, cfg):
    """Record the finished subagent's token usage, cost, and saving vs the session model."""
    if payload.get("stop_hook_active"):
        return
    path = ledger.subagent_transcript(payload)
    if not path:
        return
    messages = ledger.transcript_usage(path)
    if not messages:
        return
    meta = ledger.subagent_meta(path)
    # Price the baseline at the session model as of launch (the user may switch /model later).
    launch = ledger.find_decision(cfg, meta.get("toolUseId")) or {}
    session_model = launch.get("session_model") or ledger.last_model(payload.get("transcript_path"))
    baseline = ledger.tier_of(session_model)
    totals = ledger.summarize_usage(messages, baseline, ledger.price_table(cfg))
    attribution = ledger.attribute(launch)
    main_model = max(totals["models"], key=totals["models"].get)
    ledger.append(cfg, {
        "kind": "usage", "session": payload.get("session_id"), "agent_id": payload.get("agent_id"),
        "tool_use_id": meta.get("toolUseId"), "agent_type": meta.get("agentType") or payload.get("agent_type"),
        "description": _short(meta.get("description")), "model": main_model, "tier": ledger.tier_of(main_model)
        or ("local" if totals["local"] else None), "session_model": session_model,
        "attribution": attribution,
        **{k: totals[k] for k in ("messages", "input", "output", "cache_read", "cache_write", "cost",
                                  "baseline_cost", "saving", "local", "unpriced")},
    })
    # Foreground agents are stamped at PostToolUse (where Claude also sees it). Everything else
    # (background, or a meta with no requestShape) is stamped here, once, to the user.
    if _enabled(cfg, "MODEL_PICKER_STAMP", default=False) and meta.get("requestShape") != "foreground":
        line = ledger.stamp_text(meta.get("agentType"), main_model, totals, baseline)
        ledger.append(cfg, {"kind": "stamp", "agent_id": payload.get("agent_id"), "tool_use_id": meta.get("toolUseId")})
        # A SubagentStop systemMessage reaches neither Claude nor (reliably) the user, so queue the stamp:
        # the session's next turn (UserPromptSubmit, which also fires when the agent's result notification
        # arrives) delivers it next to the agent's report.
        ledger.append(cfg, {"kind": "pending_stamp", "session": payload.get("session_id"),
                            "agent_id": payload.get("agent_id"), "text": line})
    elif _enabled(cfg, "MODEL_PICKER_VERBOSE", default=False):
        tokens = totals["input"] + totals["output"] + totals["cache_read"] + totals["cache_write"]
        vs = f" vs ~${totals['baseline_cost']:.3f} on {baseline}" if baseline and totals["saving"] else ""
        print(json.dumps({"systemMessage": f"model-picker: {meta.get('agentType') or 'subagent'} on {main_model} "
                                            f"used {tokens:,} tokens, ~${totals['cost']:.3f}{vs}"}))


def handle_posttool(payload, cfg):
    """Stamp a finished foreground subagent's result with the model that actually ran."""
    if not _enabled(cfg, "MODEL_PICKER_STAMP", default=False):
        return
    path, meta = ledger.find_subagent(payload.get("transcript_path"), payload.get("tool_use_id"))
    if not path or meta.get("requestShape") == "background":
        return  # background agents are stamped at SubagentStop, once they have actually run
    if meta.get("requestShape") != "foreground" and meta.get("finished") is not True:
        return  # unknown shape and not finished: likely a launch-time return, let SubagentStop stamp it
    messages = ledger.transcript_usage(path)
    if not messages:
        return
    launch = ledger.find_decision(cfg, payload.get("tool_use_id")) or {}
    session_model = launch.get("session_model") or ledger.last_model(payload.get("transcript_path"))
    baseline = ledger.tier_of(session_model)
    totals = ledger.summarize_usage(messages, baseline, ledger.price_table(cfg))
    model = max(totals["models"], key=totals["models"].get)
    line = ledger.stamp_text(meta.get("agentType"), model, totals, baseline)
    out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": line}}
    already = any(r.get("kind") == "stamp" and r.get("tool_use_id") == payload.get("tool_use_id")
                  for r in ledger.read(cfg, time.time() - 86400))
    if not already:  # the user already saw it if SubagentStop stamped first; Claude still gets it
        out["systemMessage"] = line
        ledger.append(cfg, {"kind": "stamp", "tool_use_id": payload.get("tool_use_id")})
    print(json.dumps(out))


def handle_prompt(payload, cfg):
    """UserPromptSubmit: deliver stamps of background agents that finished since the last turn."""
    if not _enabled(cfg, "MODEL_PICKER_STAMP", default=False):
        return
    session = payload.get("session_id")
    if not session:
        return
    recent = ledger.read(cfg, time.time() - 86400)
    pending = {}
    for r in recent:  # a resumed agent stops more than once: keep only its latest stamp
        if r.get("kind") == "pending_stamp" and r.get("session") == session and r.get("text"):
            pending[r.get("agent_id")] = r
    # Re-deliver a resumed agent's newer stamp; skip ones already delivered at or after their time.
    last_delivery = {}
    for r in recent:
        if r.get("kind") == "stamp_delivered" and r.get("session") == session:
            last_delivery[r.get("agent_id")] = r.get("ts", 0)
    fresh = [r for a, r in pending.items() if r.get("ts", 0) > last_delivery.get(a, -1)]
    if not fresh:
        return
    for r in fresh:
        ledger.append(cfg, {"kind": "stamp_delivered", "session": session, "agent_id": r.get("agent_id")})
    lines = [r["text"] for r in fresh]
    context = ("Background agents finished since your last turn. Their actual models, from the transcripts:\n"
               + "\n".join(lines) + "\nWhen you report what one of them produced, name its model.")
    print(json.dumps({"systemMessage": "\n".join(lines),
                      "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}))


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
    roles = {"haiku": "search/read/summarize steps", "sonnet": "scoped edits or review",
             "opus": "judgment", "fable": "only the lead on major work"}
    tiers = available_tiers(cfg)
    advice = ", ".join(f"'{t}' for {r}" for t, r in roles.items() if t in tiers)
    msg = (f"model-picker: {missing} of {len(calls)} agent() calls in this workflow set no `model`. "
           f"Pass {{model: ...}} using the available tiers: {advice}.")
    if local_enabled(cfg):
        msg += f" For tool-free text steps, use the local model: {local_hint(cfg)}."
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
    _, tiers_warning = _tiers_and_warning(cfg)
    if tiers_warning:
        context += f"\nmodel-picker warning: {tiers_warning}"
    if local_enabled(cfg):
        context += ("\nLocal tier enabled: for pure text work that needs no tools or files (summarize, classify, "
                    f"extract, translate text you paste into the prompt itself), use the local model: {local_hint(cfg)}. "
                    "It cannot open files, URLs, PRs or tickets. Check its output: "
                    "small local models are fine for simple transformation, weak at anything else.")
    missing = [t for t in CLAUDE_TIERS if t not in available_tiers(cfg)]
    if missing:
        context += (f"\nNot available in this environment: {', '.join(missing)}. Never launch an agent on those; "
                    "for work the rules assign to them, use the next stronger available tier at the same effort; step down "
                    "only when nothing stronger exists, raising effort to high (Fable-level work: opus at high effort).")
    if _enabled(cfg, "MODEL_PICKER_STAMP", default=False):
        context += ("\nModel stamps are on: foreground subagent results and ask_local answers carry a "
                    "`[model-picker] ... ran on <model>` line taken from the transcript; name that model when you "
                    "report what they produced. Background agents are stamped when they finish: the stamp arrives with your "
                    "next turn (usually alongside the agent's result); name that model when you report it, and never guess "
                    "a model that has no stamp (point to /model-picker:report instead).")
    if _enabled(cfg, "MODEL_PICKER_STAMP_REPLIES", default=False):
        context += ("\nEnd every reply with one line naming the model you are running on, e.g. `— claude-opus-5-5`, "
                    "using the model id stated in your system prompt.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))


def main():
    event = sys.argv[1]
    cfg = load_config()
    if event == "route":
        task = (" ".join(sys.argv[2:3]) or sys.stdin.read()).strip()
        print(json.dumps(decide({"description": task[:200], "prompt": task}, cfg), indent=2))
        return
    if event == "report":
        args = sys.argv[2:]
        period = next((a for a in args if not a.startswith("-")), "7d")
        try:
            data = ledger.report(cfg, period)
        except ValueError as exc:
            print(f"model-picker report: {exc}", file=sys.stderr)
            sys.exit(2)
        print(json.dumps(data, indent=2) if "--json" in args else ledger.format_report(data))
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
    if event == "subagentstop":
        handle_subagent_stop(payload, cfg)
        return
    if event == "posttool":
        if payload.get("tool_name") in ("Agent", "Task"):
            handle_posttool(payload, cfg)
        return
    if event == "prompt":
        handle_prompt(payload, cfg)
        return
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
