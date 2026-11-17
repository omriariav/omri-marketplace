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
  MODEL_PICKER_FLOORS            JSON {"pattern": "tier"}: your own minimum model per kind of task, e.g.
                                 {"review": "opus"}; matched against the launch description and agent type
  MODEL_PICKER_PROMPT_ROUTE      0 (default) | 1 | always   show the decisions API's raw pick for a prompt
                                 (needs a router; the prompt's first 200 characters are sent). 1: only at
                                 natural breaks (a session's first prompt, after /compact or resume, after an
                                 idle gap), when switching the session model is cheap; always: every prompt
  MODEL_PICKER_PROMPT_ROUTE_IDLE minutes (default 60; 0 = off): with PROMPT_ROUTE=1, a prompt after this long
                                 without one counts as a natural break
  MODEL_PICKER_ASK               1 (default) | 0   label launches that use the model the user named in the prompt
  MODEL_PICKER_ROUTER_PAUSE      minutes (default 10; 0 = never pause): after 3 router failures in a row
                                 (or one rejected request, e.g. HTTP 401), skip router calls for this long

Also: model_picker.py report [today|24h|7d|30d|all] [--json] [--html [--out PATH] [--open]]
"""

import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
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


def config_path():
    return Path(os.environ.get("MODEL_PICKER_CONFIG") or Path.home() / ".config/model-picker/config.json")


def load_config():
    path = config_path()
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
            target = lower[-1]
            effort = rec["effort"] if _effort_rank(rec.get("effort")) > _effort_rank("high") else "high"
            note = f"{want} is not available here; {target} at high effort instead"
    return {**rec, "tier": target, "effort": effort, "reason": f"{rec['reason']} ({note})", "capped_from": want}


LOCAL_MIN_CONFIDENCE = 0.75  # a wrong "local" call costs the most: the local model can't use tools at all
def guard_local(rec, inp, cfg):
    """Don't trust a shaky `local` call: below LOCAL_MIN_CONFIDENCE fall back to the best non-local tier.
    (Agent launches never get `local` at all; see decide(for_launch=True).)"""
    if rec.get("tier") != "local":
        return rec
    if not (isinstance(rec.get("confidence"), (int, float)) and rec["confidence"] < LOCAL_MIN_CONFIDENCE):
        return rec
    text = " ".join(str(inp.get(k) or "") for k in ("description", "prompt")).lower()[:2000]
    tier, why = recommend(text, str(inp.get("subagent_type") or ""), local=False)
    note = f"local confidence {rec['confidence']:.2f} too low"
    return {**rec, "tier": tier, "effort": "medium", "reason": f"{why} (not local: {note})",
            "source": f"heuristic (local vetoed: {note})", "confidence": None,
            "unsure": why == UNSURE_REASON, "local_vetoed": True}


FLOOR_EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def _floors(cfg):
    """[(compiled pattern, label, tier, effort or None)] from MODEL_PICKER_FLOORS; bad entries are skipped.
    A value is a tier ("opus") or a tier and effort ("opus/high")."""
    try:
        raw = _json_obj(cfg.get("MODEL_PICKER_FLOORS"))
    except (ValueError, TypeError):
        return []
    out = []
    for pat, value in (raw.items() if isinstance(raw, dict) else []):
        tier, _, effort = str(value).strip().lower().partition("/")
        tier, effort = tier.strip(), effort.strip() or None
        if tier not in CLAUDE_TIERS or (effort and effort not in FLOOR_EFFORTS) or not str(pat).strip():
            continue
        pat = str(pat).strip()
        # A plain word ("review") matches at a word start: review, reviews, reviewer; not "preview".
        rx = r"\b" + re.escape(pat) if re.fullmatch(r"[\w -]+", pat) else pat
        try:
            out.append((re.compile(rx, re.I), pat, tier, effort))
        except re.error:
            continue
    return out


def _effort_rank(effort):
    return FLOOR_EFFORTS.index(effort) if effort in FLOOR_EFFORTS else -1


def _fitted_floor(tier, effort, cfg):
    """The floor as it applies here: an unavailable tier moves to the nearest available one. A stronger stand-in
    meets the floor at any effort (guide rule 3: past high, the next model up at lower effort); a weaker one
    needs at least high."""
    tiers = available_tiers(cfg)
    if tier in tiers:
        return tier, effort
    idx = CLAUDE_TIERS.index(tier)
    higher = [t for t in CLAUDE_TIERS[idx + 1:] if t in tiers]
    if higher:
        return higher[0], None
    lower = [t for t in CLAUDE_TIERS[:idx] if t in tiers]
    if not lower:
        return tier, effort
    return lower[-1], effort if _effort_rank(effort) > _effort_rank("high") else "high"


def apply_floors(rec, inp, cfg):
    """Raise the recommendation to the user's own minimum model (and effort) for matching tasks
    (MODEL_PICKER_FLOORS). A stronger model meets a floor at any effort; a floor never lowers anything.
    floor (the rule's name, used for credit) is set only when the model was raised; an effort-only raise is
    recorded as floor_effort."""
    # Bounded input: patterns are the user's own regexes and run as written.
    text = f"{str(inp.get('description') or inp.get('name') or '')[:300]} {str(inp.get('subagent_type') or '')[:100]}"
    hits = [(label, tier, effort) for rx, label, tier, effort in _floors(cfg) if rx.search(text)]
    if not hits:
        return rec
    top = max((h[1] for h in hits), key=CLAUDE_TIERS.index)
    at_top = [h for h in hits if h[1] == top]  # weaker rules are met by the stronger tier at any effort
    effort = max((h[2] for h in at_top if h[2]), key=_effort_rank, default=None)
    tier, effort = _fitted_floor(top, effort, cfg)
    floor_min = f"{tier}/{effort}" if effort else tier
    # Name only the rules that set the minimum: the top-tier ones, and of those the one giving the effort.
    labels = ", ".join(dict.fromkeys(h[0] for h in at_top if not h[2] or h[2] == effort or tier != top))
    minimum = f"{tier}/{effort} effort" if effort else tier
    cur = rec["tier"] if rec["tier"] in CLAUDE_TIERS else None
    if cur and CLAUDE_TIERS.index(cur) > CLAUDE_TIERS.index(tier):
        return {**rec, "floor_min": floor_min}  # a stronger model meets the floor
    raise_tier = not cur or CLAUDE_TIERS.index(cur) < CLAUDE_TIERS.index(tier)
    new_effort = effort if effort and _effort_rank(effort) > _effort_rank(rec.get("effort")) else rec.get("effort")
    if not raise_tier and new_effort == rec.get("effort"):
        return {**rec, "floor_min": floor_min}  # already meets the floor
    out = {**rec, "tier": tier, "effort": new_effort, "unsure": False, "floor_min": floor_min,
           "source": f"{rec['source']} + floor"}
    if raise_tier:
        out.update(reason=f"your rule: \"{labels}\" tasks get at least {minimum} (MODEL_PICKER_FLOORS)",
                   floor=labels, floored_from=rec["tier"])
        if effort:
            out["floor_effort"] = new_effort
    else:
        out.update(reason=f"{rec['reason']}; effort raised to {new_effort} by your rule \"{labels}\"",
                   floor_effort=new_effort)
    return out


def decide(inp, cfg, use_router=True, for_launch=False, track=True):
    """Recommendation dict: tier, effort, reason, source, limited to the available tiers.

    for_launch: the call is an Agent/teammate launch that is already happening. `local` (call ask_local
    instead of launching an agent) can't apply there, and guessing it from a one-line description sends
    tool work to a model that has no tools; so launches only ever get Claude tiers. The local tier is
    offered where the choice is still open: the session rules and the /model-picker skill.

    track=False: honor a router pause but don't count this call toward one (its notice would have no reader).
    """
    rec = _decide(inp, cfg, use_router, allow_local=not for_launch, track=track)
    # Fit first, so a floor compares against a model that can actually run here.
    return fit_to_available(apply_floors(fit_to_available(guard_local(rec, inp, cfg), cfg), inp, cfg), cfg)


def _decide(inp, cfg, use_router=True, allow_local=True, track=True):
    """Router first (if configured), heuristic fallback; may name a tier that is not available."""
    local = allow_local and local_enabled(cfg)
    agent_type = str(inp.get("subagent_type") or "")
    text = " ".join(str(inp.get(k) or "") for k in ("description", "prompt", "name"))
    tier, why = recommend(text, agent_type, local)
    fallback = {"tier": tier, "effort": "medium", "reason": why, "source": "heuristic",
                "confidence": None, "router_ms": None, "unsure": why == UNSURE_REASON}
    name = str(cfg.get("MODEL_PICKER_ROUTER") or "heuristic").lower()
    router = routers.ROUTERS.get(name)
    if not use_router or not router or agent_type.lower().split(":")[-1] in (READ_ONLY_TYPES | PLANNING_TYPES):
        return fallback
    paused = router_paused(cfg)
    if paused:
        fallback["source"] = f"heuristic (router paused: {paused})"
        return fallback
    box = {}
    worker = threading.Thread(target=lambda: box.update(res=_route(router, name, inp, cfg, local)), daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(ROUTER_DEADLINE)
    fallback["router_ms"] = int((time.monotonic() - started) * 1000)
    res = box.get("res")
    notice = record_router(cfg, res) if track else None
    if notice:
        fallback["notice"] = notice
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
        rcfg["_timeout"] = min(_num(cfg, "MODEL_PICKER_ROUTER_TIMEOUT", 3), ROUTER_DEADLINE * 0.9)  # finish before the join gives up
        rcfg["_headers"] = _json_obj(cfg.get("MODEL_PICKER_ROUTER_HEADERS"))
        rcfg["_metadata"] = _json_obj(cfg.get("MODEL_PICKER_ROUTER_METADATA"))
        key_env = str(cfg.get("MODEL_PICKER_ROUTER_API_KEY_ENV") or routers.DEFAULT_KEY_ENV.get(name, ""))
        if name != "command" and key_env.lower() == "none":
            rcfg["_api_key"] = ""  # gateway on a private network that needs no key
        elif name != "command":
            key = os.environ.get(key_env, "")
            if not key and cfg.get("MODEL_PICKER_ROUTER_API_KEY_CMD"):
                key = routers.run_shell(cfg["MODEL_PICKER_ROUTER_API_KEY_CMD"], min(rcfg["_timeout"], 2)).strip()
            if not key:
                raise ValueError("no API key")
            rcfg["_api_key"] = key
        tiers = [t for t in available_tiers(cfg) if local or t != "local"]
        return router(task_text(inp, cfg), tiers, rcfg)
    except Exception as exc:  # router trouble never blocks a launch
        return exc


# --- router pause: stop waiting on a router that is down (e.g. a gateway behind a VPN that is off) ----------
ROUTER_FAILS_TO_PAUSE = 3                         # timeouts / connection errors in a row
REJECTED_CODES = {400, 401, 403, 404, 405, 407}   # a retry can't fix these: pause on the first one
MAX_PAUSE_MINUTES = 1440
HEALTH_LOCK_WAIT = 1.0                            # seconds; past this the call just isn't counted
# Settings that decide whether calls can succeed; changing any of them starts a fresh count.
ROUTER_ID_KEYS = ("MODEL_PICKER_ROUTER", "MODEL_PICKER_ROUTER_URL", "MODEL_PICKER_ROUTER_MODEL",
                  "MODEL_PICKER_ROUTER_CMD", "MODEL_PICKER_ROUTER_API_KEY_ENV", "MODEL_PICKER_ROUTER_API_KEY_CMD",
                  "MODEL_PICKER_ROUTER_HEADERS")


def _router_id(cfg):
    """Fingerprint of the router settings and the key in use (hashed: commands, headers and keys are secrets).
    A session with a different key (fixed, or still stale) keeps its own count."""
    name = str(cfg.get("MODEL_PICKER_ROUTER") or "").lower()
    key_env = str(cfg.get("MODEL_PICKER_ROUTER_API_KEY_ENV") or routers.DEFAULT_KEY_ENV.get(name, ""))
    key = os.environ.get(key_env, "") if key_env and key_env.lower() != "none" else ""
    raw = "|".join([*(str(cfg.get(k) or "") for k in ROUTER_ID_KEYS), key])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _router_label(cfg):
    name = str(cfg.get("MODEL_PICKER_ROUTER") or "heuristic").lower()
    model = cfg.get("MODEL_PICKER_ROUTER_MODEL")
    return str(model).split("/")[-1] if model and name != "command" else f"{name} router"


def _pause_seconds(cfg):
    try:
        minutes = _num(cfg, "MODEL_PICKER_ROUTER_PAUSE", 10)
    except (TypeError, ValueError):
        minutes = 10.0
    if minutes != minutes:  # NaN
        minutes = 10.0
    return min(max(0.0, minutes), MAX_PAUSE_MINUTES) * 60


def _health_path(cfg):
    return ledger.state_dir(cfg) / "router-health.json"


def _health_all(cfg):
    try:
        data = json.loads(_health_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = data.get("routers") if isinstance(data, dict) else None
    return {k: v for k, v in entries.items() if isinstance(v, dict)} if isinstance(entries, dict) else {}


def _health_read(cfg):
    """This router's state. Shared across sessions, so one session's failures spare the others the wait;
    kept per fingerprint, so sessions with different keys or settings don't overwrite each other."""
    return dict(_health_all(cfg).get(_router_id(cfg)) or {})


def _entry_last(e):
    vals = []
    for k in ("last", "paused_until", "prompt_last", "prompt_paused_until", "launch_last", "launch_paused_until"):
        try:
            vals.append(float(e.get(k) or 0))
        except (TypeError, ValueError):
            pass
    return max(vals or [0.0])


def _health_write(cfg, entry, now=None):
    """Atomic (readers never see a half-written file). Entries idle for a day are dropped."""
    tmp = None
    try:
        now = now or time.time()
        entries = {k: v for k, v in _health_all(cfg).items() if now - _entry_last(v) < MAX_PAUSE_MINUTES * 60}
        if entry:
            entries[_router_id(cfg)] = entry
        else:
            entries.pop(_router_id(cfg), None)
        path = _health_path(cfg)
        fd, tmp = tempfile.mkstemp(prefix=".router-health.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"routers": entries}, fh)
        os.replace(tmp, path)
    except OSError:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _health_lock(cfg):
    """flock serializing read-modify-write across parallel hooks (parallel launches run their hooks at once).
    Returns (proceed, fd): fd to unlock; (True, None) where the filesystem has no flock (a lost update there
    only delays a pause); (False, None) when another hook holds the lock too long or the dir is unusable."""
    try:
        d = ledger.state_dir(cfg)
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(d / ".router-health.lock", os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return False, None
    deadline = time.monotonic() + HEALTH_LOCK_WAIT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True, fd
        except BlockingIOError:  # held by another hook
            if time.monotonic() > deadline:
                os.close(fd)
                return False, None
            time.sleep(0.01)
        except OSError:  # no flock here (ENOLCK, EOPNOTSUPP): go on without it
            os.close(fd)
            return True, None


def _num_field(h, key):
    try:
        return float(h.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _paused_until(h, prefix=""):
    return _num_field(h, prefix + "paused_until")


def router_paused(cfg, now=None, scope="launch"):
    """Why router calls are paused right now, or None: the main pause, plus the prompt line's own pause
    (scope="prompt") or the launches' own pause (scope="launch").
    A pause ending further ahead than one pause from now (clock moved back, hand-edited file) is ignored."""
    pause = _pause_seconds(cfg)
    if pause <= 0:
        return None
    h = _health_read(cfg)
    now = now or time.time()
    for prefix in ("", "prompt_" if scope == "prompt" else "launch_"):
        if now < _paused_until(h, prefix) <= now + pause:
            return str(h.get(prefix + "reason") or "failing")
    return None


def _failure(res):
    """(reason, rejected, slow) when the router could not be reached or refused the call; None when it answered;
    "other" for any other error (a malformed or unusable answer means the router is up and fast).
    slow: the call ran out of time (after connecting, or we can't tell), so a longer budget might have worked."""
    if isinstance(res, dict):
        return None
    if res is None:
        return "timed out", False, True
    if isinstance(res, urllib.error.HTTPError):  # before URLError: it is a subclass
        return f"HTTP {res.code}", res.code in REJECTED_CODES, False
    if isinstance(res, urllib.error.URLError):  # never connected: DNS, refused, no route, connect timeout
        timed_out = isinstance(res.reason, (TimeoutError, socket.timeout))
        return ("connect timed out" if timed_out else "unreachable"), False, False
    if isinstance(res, (TimeoutError, socket.timeout, subprocess.TimeoutExpired)):  # socket.timeout: Python < 3.10
        return "timed out", False, True
    if isinstance(res, OSError):
        return "unreachable", False, False
    if isinstance(res, subprocess.CalledProcessError):
        # The router or key command failed; its exit code says nothing about why (a wrapped curl that
        # can't reach the gateway looks the same), so it counts like a connection error.
        return f"command exit {res.returncode}", False, False
    return "other"


def _clear(h, prefix):
    changed = False
    for k in ("fails", "reason", "rejected", "last", "paused_until"):
        changed = h.pop(prefix + k, None) is not None or changed
    return changed


def record_router(cfg, res, now=None, scope="launch"):
    """Count this router call in the shared state. Returns a one-line notice when it starts a pause.

    Three counts per router, each "in a row" for what it measures:
    - main: connection failures and rejections from either hook; pauses every router call; any answer clears it.
    - prompt_: every prompt failure, timeouts included (prompts get only 3 s); pauses only the prompt line,
      which shows nothing on a failure anyway; a prompt answer clears it.
    - launch_: every launch failure; pauses only launches; a launch answer clears it (a short prompt answering
      in time says nothing about a long launch input).
    A retry that fails right after a pause, for the same kind of reason, pauses again quietly (the user was told)."""
    pause = _pause_seconds(cfg)
    if pause <= 0:
        return None
    fail = _failure(res)
    if fail == "other":
        return None  # no sign either way: leave the count alone
    proceed, fd = _health_lock(cfg)
    if not proceed:
        return None
    notices = {}
    own = "prompt_" if scope == "prompt" else "launch_"
    try:
        now = now or time.time()
        h = _health_read(cfg)
        before = dict(h)
        for pre in ("", "prompt_", "launch_"):  # old failures, or a pause that ended long ago (overnight): start over
            last = max(_paused_until(h, pre), _num_field(h, pre + "last"))
            if last and now > last + pause:
                _clear(h, pre)
        if fail is None:
            _clear(h, "")
            _clear(h, own)
            if h != before:
                _health_write(cfg, h, now)
            return None
        reason, rejected, slow = fail
        counts = [own] + ([] if slow else [""])
        for pre in counts:
            until = _paused_until(h, pre)
            fails = h[pre + "fails"] + 1 if isinstance(h.get(pre + "fails"), int) else 1
            h.update({pre + "fails": fails, pre + "reason": reason, pre + "rejected": rejected, pre + "last": now})
            if not rejected and fails < ROUTER_FAILS_TO_PAUSE:
                h.pop(pre + "paused_until", None)
                continue
            h[pre + "paused_until"] = now + pause
            # Same kind (can't reach vs rejected): "timed out" and "unreachable" alternating isn't news.
            if not (until and bool(before.get(pre + "rejected")) == rejected):
                notices[pre] = fails
        _health_write(cfg, h, now)
    finally:
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    if not notices:
        return None
    pre = "" if "" in notices else own  # a main pause covers the prompt line too: one line is enough
    fails = notices[pre]
    mins = f"{pause / 60:g} min"
    label = _router_label(cfg)
    reset = f"delete {_health_path(cfg)} to retry sooner"
    other_paused = pre and now < max(_paused_until(h, "launch_" if pre == "prompt_" else "prompt_"), _paused_until(h, ""))
    if pre == "launch_":
        rest = "the prompt line is paused too" if other_paused else "the prompt line still asks it"
        return (f"model-picker: {label} failed {fails}× in a row ({reason}) on agent launches; launches use the "
                f"keyword rules for {mins}, {rest} ({reset}).")
    if pre:
        rest = "agent launches are paused too" if other_paused else "agent launches still ask it"
        return (f"model-picker: {label} failed on {fails} prompts in a row ({reason}); the prompt line is paused "
                f"for {mins}, {rest} ({reset}).")
    if rejected:
        return (f"model-picker: {label} rejected the call ({reason}), which a retry won't fix; router calls paused "
                f"for {mins}. Check the router URL, key (MODEL_PICKER_ROUTER_API_KEY_ENV / _API_KEY_CMD) and "
                f"headers, then {reset}.")
    return (f"model-picker: {label} failed {fails}× in a row ({reason}); router calls paused for {mins}, "
            f"agent launches use the keyword rules meanwhile ({reset}).")


# --- "you asked for": a model or effort the user named in their prompt -----------------------------------
# Labels only. Claude reads the prompt itself and the guide tells it the user's ask wins; the hooks never steer
# a launch from this guess, they only record and label a launch that used what the user asked for.
ASK_TIERS = "haiku|sonnet|opus|fable"
ASK_EFFORTS = "low|medium|high|xhigh|max"
_VERB = r"\b(?:use|run|launch|spawn|start|switch\s+to|go\s+with|try|pick|do)\b"
_ASK_TIER_RX = re.compile(
    _VERB + r"[^.;:!?\n]{0,30}?"
    r"(?:\b(?:the|a|an)\s+(?:claude[- ])?(?P<t1>" + ASK_TIERS + r")(?:[- ]?\d+(?:[.-]\d+)*)?\s+(?:agents?|subagents?|model)\b(?!['’]s\b)(?!\s+(?:definition|file|config))"
    r"|(?<![\w-])(?:claude[- ])?(?P<t2>" + ASK_TIERS + r")(?:[- ]?\d+(?:[.-]\d+)*)?(?![\w-])"
    r"(?!\s+(?:poem|poems|about))(?!\s+(?:agent\s+)?definition))", re.I)
_ANY_TIER = re.compile(r"(?<![\w-])(?:claude[- ])?(" + ASK_TIERS + r")(?:[- ]?\d+(?:[.-]\d+)*)?(?![\w-])", re.I)
_ASK_EFFORT_RX = [
    re.compile(r"\b(?:use|run|at|with|on)\s+(" + ASK_EFFORTS + r")[- ]effort\b", re.I),
    re.compile(r"\beffort\s*[:=]\s*(" + ASK_EFFORTS + r")\b", re.I),
    re.compile(r"\bset\s+(?:the\s+)?effort\s+to\s+(" + ASK_EFFORTS + r")\b", re.I),
]
_NEGATION = re.compile(r"\b(?:don['’]?t|do\s+not|(?:can|won|shouldn|mustn|doesn|didn|isn|wouldn)['’]?t|cannot|not|never|no"
                       r"|avoid|stop|instead\s+of|rather\s+than|without|except)\b"
                       r"[^.!?;,\n]{0,25}$", re.I)
# Code (an unclosed fence runs to the end), inline code, double and curly quotes (straight single quotes are
# left alone: they are usually apostrophes).
_QUOTED = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`|\"[^\"\n]*\"|“[^”\n]*”|‘[^’\n]*’", re.S)
_ASK_ARTICLE_POEM = re.compile(r"\b(?:a|an|the)\s+$", re.I)
ASK_TTL = 86400


def _ask_text_body(prompt):
    """The parts of a prompt a person wrote as an instruction: no code, quotes, quoted lines or pasted
    model-picker output, and no questions."""
    text = _QUOTED.sub(" ", str(prompt or "")[:20000])
    lines = [ln for ln in text.splitlines()  # quoted lines, shell lines, tracebacks, pasted hook output
             if not re.match(r"[\s⎿│●⏺]*(?:>|\$ |File \")", ln) and not re.match(r"\s{4,}\S", ln)
             and "model-picker" not in ln.lower()]
    sentences = re.split(r"(?<!\be\.g\.)(?<!\bi\.e\.)(?<=[.!?])\s+|(?<=[。！？])|\n+", "\n".join(lines))
    return "\n".join(x for x in sentences if not x.rstrip().endswith(("?", "？")))[:2000]


def detect_ask(prompt):
    """{"tier", "effort"} the user asked for in this prompt, or None. Narrow on purpose (an imperative verb
    before the model; "<level> effort" after use/run/at/with/on, "effort: <level>", "set effort to <level>");
    negations, questions, code and quotes are skipped; two different models (or efforts) named means no pick."""
    text = _ask_text_body(prompt)
    tiers, efforts = set(), set()
    for m in _ASK_TIER_RX.finditer(text):
        tier = (m.group("t1") or m.group("t2")).lower()
        start = m.start("t1") if m.group("t1") else m.start("t2")
        if m.group("t2") and _ASK_ARTICLE_POEM.search(text[max(0, start - 6):start]):
            continue  # "try a haiku", "pick a sonnet": a poem, not a model (models with an article need "agent"/"model")
        if not _NEGATION.search(text[max(0, start - 45):start]):
            tiers.add(tier)
    for rx in _ASK_EFFORT_RX:
        for m in rx.finditer(text):
            if not _NEGATION.search(text[max(0, m.start() - 45):m.start()]):
                efforts.add(m.group(1).lower())
    if len(tiers) == 1:  # any other model named (not negated) anywhere makes it a comparison, not a pick
        for m in _ANY_TIER.finditer(text):
            if m.group(1).lower() not in tiers and not _NEGATION.search(text[max(0, m.start() - 45):m.start()]):
                tiers.add(m.group(1).lower())
    ask = {"tier": tiers.pop() if len(tiers) == 1 else None, "effort": efforts.pop() if len(efforts) == 1 else None}
    return ask if ask["tier"] or ask["effort"] else None


def _asks_enabled(cfg):
    return _enabled(cfg, "MODEL_PICKER_ASK", default=True) and ledger.logging_enabled(cfg)


def _ask_file(cfg, session):
    return _session_file(cfg, "asked", session)


def remember_ask(cfg, session, ask, now=None):
    """Keep this turn's ask for the session's launches until the next prompt. One small file per session (no
    cross-session lost updates); files older than a day are removed."""
    if not session or not _asks_enabled(cfg):
        return
    now = now or time.time()
    path = _ask_file(cfg, session)
    try:
        if not ask:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        _write_session_file(path, {**ask, "ts": now}, now)
    except OSError:
        pass


def _session_file(cfg, kind, session):
    return ledger.state_dir(cfg) / kind / (hashlib.sha256(str(session).encode()).hexdigest()[:24] + ".json")


def _write_session_file(path, obj, now):
    """Atomically write one small per-session JSON file; files in the same folder idle over a day are removed."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for old in path.parent.glob("*.json"):
        try:
            if now - old.stat().st_mtime > ASK_TTL:
                old.unlink()
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def current_ask(cfg, session, now=None):
    if not session or not _asks_enabled(cfg):
        return None
    try:
        ask = json.loads(_ask_file(cfg, session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(ask, dict) or (now or time.time()) - _num_field(ask, "ts") >= ASK_TTL:
        return None
    tier = ask.get("tier") if ask.get("tier") in CLAUDE_TIERS else None
    effort = ask.get("effort") if isinstance(ask.get("effort"), str) else None
    return {"tier": tier, "effort": effort} if tier or effort else None


def ask_text(ask):
    return "/".join(x for x in (ask.get("tier"), ask.get("effort") and f"{ask['effort']} effort") if x)


def describe(rec, cfg):
    target = f"`{rec['tier']}`"
    if rec["tier"] == "local":
        target += f" ({local_hint(cfg)})"
    return f"{target}, effort {rec['effort']}: {rec['reason']} [via {rec['source']}]"


def _short(text, n=120):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def log_decision(cfg, payload, inp, outcome, chosen=None, rec=None, session_model=None, pinned=None, user_pick=None):
    """One ledger line per checked launch. Description only (local file); never the prompt."""
    chosen_tier = ledger.tier_of(chosen) if chosen else None
    entry = {
        "kind": "decision", "session": payload.get("session_id"), "tool_use_id": payload.get("tool_use_id"),
        "tool": payload.get("tool_name"), "agent_type": str(inp.get("subagent_type") or "general-purpose"),
        "description": _short(inp.get("description") or inp.get("name")), "outcome": outcome,
        "chosen": chosen, "chosen_tier": chosen_tier, "session_model": session_model, "pinned": pinned,
        "chosen_effort": inp.get("effort") if isinstance(inp.get("effort"), str) else None,
    }
    if user_pick:
        entry["user_pick"] = user_pick
    if rec:
        entry.update({"rec_tier": rec["tier"], "rec_effort": rec["effort"], "source": rec["source"],
                      "confidence": rec.get("confidence"), "router_ms": rec.get("router_ms"),
                      "unsure": bool(rec.get("unsure")), "floor": rec.get("floor"),
                      "floor_effort": rec.get("floor_effort"),
                      # A fallback guess can't judge an explicit choice: leave "followed" unknown.
                      "followed": None if (not chosen_tier or rec.get("unsure")) else
                      (chosen_tier == rec["tier"] or (rec["tier"] == "local" and chosen_tier == "haiku"))})
    ledger.append(cfg, entry)


def _below_floor(rec, chosen_tier, effort):
    """"opus/high effort" when an explicit launch is under the user's floor for it, else None. An omitted
    effort is below an effort floor (the launch inherits whatever the session runs at)."""
    floor = rec.get("floor_min")
    if not floor or chosen_tier not in CLAUDE_TIERS:
        return None
    tier, _, f_effort = floor.partition("/")
    have, need = CLAUDE_TIERS.index(chosen_tier), CLAUDE_TIERS.index(tier)
    below = have < need or (have == need and bool(f_effort) and _effort_rank(effort) < _effort_rank(f_effort))
    return (f"{tier}/{f_effort} effort" if f_effort else tier) if below else None


def verbose_note(cfg, label, chosen, rec):
    if not _enabled(cfg, "MODEL_PICKER_VERBOSE", default=False):
        return None
    conf = f" {rec['confidence']:.2f}" if isinstance(rec.get("confidence"), (int, float)) else ""
    ms = f", {rec['router_ms']} ms" if rec.get("router_ms") is not None else ""
    src = ledger._rec_label(rec["source"])
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
        not model or _enabled(cfg, "MODEL_PICKER_ROUTER_ON_EXPLICIT", default=False)), for_launch=True)
    session_model = ledger.last_model(payload.get("transcript_path"))
    outcome = ("denied" if mode == "enforce" and (not model or chosen_unavailable)
               else "explicit" if model else "missing")
    # The user named a model in their prompt and this launch used it: their call, not a deviation from the
    # recommendation (an omitted effort doesn't count against it). Labels only: nothing here steers a launch.
    ask = current_ask(cfg, payload.get("session_id"))
    launch_effort = inp.get("effort") if isinstance(inp.get("effort"), str) else None
    honored = bool(ask and ask["tier"]) and bool(model) and tier_of(model) == ask["tier"] and (
        not ask["effort"] or not launch_effort or launch_effort == ask["effort"])
    # Record only what the launch actually honored (an omitted effort isn't the asked effort).
    user_pick = {"tier": ask["tier"], "effort": ask["effort"] if launch_effort == ask["effort"] else None} if honored else None
    log_decision(cfg, payload, inp, outcome, chosen=model or session_model, rec=rec, session_model=session_model,
                 pinned=pinned, user_pick=user_pick)
    note = "\n".join(n for n in (rec.get("notice"), verbose_note(cfg, label, model, rec)) if n) or None
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
        below = _below_floor(rec, chosen, launch_effort)
        if honored:
            emit(system=note)
        elif below:
            emit(context=f"model-picker: `{label}` matches your MODEL_PICKER_FLOORS rule (at least {below}); it was "
                         f"launched on {chosen}{f' at {launch_effort} effort' if launch_effort else ' with no effort set'}. "
                         "Use at least that for similar launches.", system=note)
        elif chosen and not rec.get("unsure") and abs(TIERS.index(chosen) - TIERS.index(rec["tier"])) >= 2:
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
        line = ledger.stamp_text(meta.get("agentType"), main_model, totals, baseline,
                                 ledger.run_effort(messages) or launch.get("chosen_effort"), launch)
        if not ledger.logging_enabled(cfg):
            # Delivery needs the ledger; without it, the user-facing line is the best we can do.
            print(json.dumps({"systemMessage": line}))
            return
        ledger.append(cfg, {"kind": "stamp", "agent_id": payload.get("agent_id"), "tool_use_id": meta.get("toolUseId")})
        # A SubagentStop systemMessage reaches neither Claude nor (reliably) the user, so queue the stamp:
        # the session's next turn (UserPromptSubmit, which also fires when the agent's result notification
        # arrives) delivers it next to the agent's report. `pid` makes each pending stamp uniquely deliverable.
        if not ledger.queue_stamp(cfg, payload.get("session_id"), payload.get("agent_id"), line):
            print(json.dumps({"systemMessage": line}))  # couldn't queue: at least show it
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
    line = ledger.stamp_text(meta.get("agentType"), model, totals, baseline,
                             ledger.run_effort(messages) or launch.get("chosen_effort"), launch)
    out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": line}}
    already = any(r.get("kind") == "stamp" and r.get("tool_use_id") == payload.get("tool_use_id")
                  for r in ledger.read(cfg, time.time() - 86400))
    if not already:  # the user already saw it if SubagentStop stamped first; Claude still gets it
        out["systemMessage"] = line
        ledger.append(cfg, {"kind": "stamp", "tool_use_id": payload.get("tool_use_id")})
    print(json.dumps(out))


PROMPT_ROUTE_DEADLINE = 3.0  # seconds: the user is waiting for their turn to start


def _prompt_route_mode(cfg):
    """None (off), "breaks" (1: natural breaks only) or "always" (every prompt)."""
    if str(cfg.get("MODEL_PICKER_PROMPT_ROUTE") or "").strip().lower() == "always":
        return "always"
    return "breaks" if _enabled(cfg, "MODEL_PICKER_PROMPT_ROUTE", default=False) else None


def mark_break(cfg, session, now=None):
    """SessionStart (startup, resume, /clear, /compact): the session's next prompt is a natural break."""
    if not session or _prompt_route_mode(cfg) != "breaks":
        return
    now = now or time.time()
    path = _session_file(cfg, "prompt-breaks", session)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = None
    last = _num_field(state, "last") if isinstance(state, dict) else 0.0
    try:
        _write_session_file(path, {"last": last or now, "pending": True}, now)
    except OSError:
        pass


def _break_pending(cfg, state, now):
    """True when this prompt is a natural break: the session's first, one marked by SessionStart, one whose
    break hasn't been answered yet, or the first after MODEL_PICKER_PROMPT_ROUTE_IDLE minutes without a prompt."""
    if not isinstance(state, dict) or state.get("pending") is not False:
        return True
    try:
        idle = _num(cfg, "MODEL_PICKER_PROMPT_ROUTE_IDLE", 60) * 60
    except (TypeError, ValueError):
        idle = 3600.0
    return idle > 0 and now - _num_field(state, "last") >= idle


def prompt_route(payload, cfg, now=None):
    """MODEL_PICKER_PROMPT_ROUTE: the router's raw answer for this prompt, as one user-visible line.

    With 1, only at natural breaks, where switching the session model is cheap (guide rule 7): mid-task
    prompts are never sent, so the router isn't paid for picks nobody would act on. A break stays open
    until the router answers an eligible prompt. With always, every eligible prompt."""
    mode = _prompt_route_mode(cfg)
    if not mode:
        return None
    session = payload.get("session_id")
    if mode == "always" or not session:
        return _prompt_route_call(payload, cfg)[0]
    now = now or time.time()
    path = _session_file(cfg, "prompt-breaks", session)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = None
    pending = _break_pending(cfg, state, now)
    # Any prompt (agent notifications and slash commands too: each runs a model turn) restarts the idle clock.
    # If the state can't be written, every prompt looks like a session's first (the always behavior).
    line, answered = _prompt_route_call(payload, cfg) if pending else (None, False)
    try:
        _write_session_file(path, {"last": now, "pending": pending and not answered}, now)
    except OSError:
        pass
    return line


def _prompt_route_call(payload, cfg):
    """(line or None, whether the router answered). Only the configured decisions API's own answer: no
    heuristic fallback, no floors, no confidence cut. Nothing is shown when no router is configured or it
    doesn't answer in time. Skips slash commands, harness messages (agent notifications), and very short
    replies ("ok", "merge")."""
    name = str(cfg.get("MODEL_PICKER_ROUTER") or "heuristic").lower()
    router = routers.ROUTERS.get(name)
    prompt = str(payload.get("prompt") or "").strip()
    if not router or not prompt or prompt[0] in "/<!" or len(prompt.split()) < 3:
        return None, False
    if router_paused(cfg, scope="prompt"):
        return None, False  # don't make every prompt wait on a router that is down
    # Only the first 200 characters, whatever MODEL_PICKER_ROUTER_INPUT says (that setting is for launches).
    inp = {"description": _short(prompt, 200), "subagent_type": "main-session"}
    # Router subprocesses (command router, key command) must finish inside the deadline, not be orphaned.
    rcfg = {**cfg, "MODEL_PICKER_ROUTER_TIMEOUT": min(_num(cfg, "MODEL_PICKER_ROUTER_TIMEOUT", 3), PROMPT_ROUTE_DEADLINE - 0.5)}
    box = {}
    worker = threading.Thread(target=lambda: box.update(res=_route(router, name, inp, rcfg, False)), daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(PROMPT_ROUTE_DEADLINE)
    res = box.get("res")
    notice = record_router(cfg, res, scope="prompt")
    if notice:
        return notice, False
    if not isinstance(res, dict) or res.get("tier") not in TIERS:
        # The router was reached and replied, just not usably (malformed, a tier it wasn't offered): that
        # still closes the break, or a billed-but-unusable gateway would be called on every prompt.
        # Connection failures, timeouts and rejections keep it open.
        return None, isinstance(res, dict) or _failure(res) == "other"
    ms = int((time.monotonic() - started) * 1000)
    ledger.append(cfg, {"kind": "prompt", "session": payload.get("session_id"), "rec_tier": res["tier"],
                        "rec_effort": res.get("effort"), "source": res.get("source"), "confidence": res.get("confidence"),
                        "router_ms": ms, "session_tier": ledger.tier_of(ledger.last_model(payload.get("transcript_path")))})
    label = ledger._rec_label(res.get("source") or name)
    conf = f"{res['confidence']:.2f}, " if isinstance(res.get("confidence"), (int, float)) else ""
    return f"model-picker: {label} → {res['tier']}/{res.get('effort') or 'medium'} ({conf}{ms} ms)", True


def _human_prompt(payload):
    """The prompt if a person typed it: not a slash command, harness message (agent notification) or shell line."""
    prompt = str(payload.get("prompt") or "").strip()
    return prompt if prompt and prompt[0] not in "/<!" else None


def handle_prompt(payload, cfg):
    """UserPromptSubmit: show the router's pick for this prompt (opt-in), and deliver stamps of background
    agents that finished since the last turn (reads only the small stamp queue, never the whole ledger)."""
    shown, context = [], None
    ask = None
    try:
        raw = str(payload.get("prompt") or "").lstrip()
        if not raw.startswith("<task-notification"):  # an agent finishing isn't a new request: keep the ask
            prompt = _human_prompt(payload)
            ask = detect_ask(prompt) if prompt is not None and _asks_enabled(cfg) else None  # slash commands: clear
            remember_ask(cfg, payload.get("session_id"), ask)
    except Exception:
        pass
    try:
        line = prompt_route(payload, cfg)
        if ask and line and " → " in line:  # on the router's pick line only, never on a pause notice
            line += f" · you asked for {ask_text(ask)}"
        if line:
            shown.append(line)
    except Exception:
        pass  # advisory only: never stand between the user and their turn
    session = payload.get("session_id")
    if session and _enabled(cfg, "MODEL_PICKER_STAMP", default=False) and ledger.logging_enabled(cfg):
        lines = ledger.take_stamps(cfg, session)
        if lines:
            shown += lines
            context = ("Background agents finished since your last turn. Their actual models, from the transcripts:\n"
                       + "\n".join(lines) + "\nWhen you report what one of them produced, name its model.")
    if not shown:
        return
    body = {"systemMessage": "\n".join(shown)}
    if context:
        body["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": context}
    print(json.dumps(body))


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
        # A workflow agent() step is a tool-using agent: never route it to the local model. Plain text
        # transformation belongs outside the workflow (ask_local from the main session), if anywhere.
        msg += " Text-only transformation of text you already have can be done with ask_local before or after the workflow, not as an agent() step."
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
        context += ("\nLocal tier enabled: before launching an agent, consider whether the task is pure text work on text "
                    "you already have (summarize, classify, "
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


def _open_in_browser(uri):
    """Open uri in a GUI browser and say whether it worked. On Linux/BSD without a display, Python's
    webbrowser falls back to console browsers (lynx, w3m) that would take over the terminal, so skip it."""
    headless = sys.platform not in ("darwin", "win32") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if not headless:
        try:
            import webbrowser
            if webbrowser.open(uri):
                return "Opened in the browser."
        except Exception:
            pass
    return "Could not open a browser here; open the file link above."


def main():
    event = sys.argv[1]
    cfg = load_config()
    if event == "route":
        task = (" ".join(sys.argv[2:3]) or sys.stdin.read()).strip()
        print(json.dumps(decide({"description": task[:200], "prompt": task}, cfg, track=False), indent=2))
        return
    if event == "report":
        args = sys.argv[2:]
        out_at = args.index("--out") + 1 if "--out" in args else None
        if out_at is not None and (out_at >= len(args) or args[out_at].startswith("-")):
            print("model-picker report: --out needs a file path", file=sys.stderr)
            sys.exit(2)
        out_arg = args[out_at] if out_at is not None else None
        period = next((a for i, a in enumerate(args) if not a.startswith("-") and i != out_at), "7d")
        try:
            data = ledger.report(cfg, period)
        except ValueError as exc:
            print(f"model-picker report: {exc}", file=sys.stderr)
            sys.exit(2)
        as_json = "--json" in args
        print(json.dumps(data, indent=2) if as_json else ledger.format_report(data))
        if "--html" in args:
            note = sys.stderr if as_json else sys.stdout  # keep --json output parseable
            try:
                path = ledger.write_html(cfg, data, out_arg).resolve()
            except OSError as exc:
                print(f"model-picker report: could not write the HTML report: {exc}", file=sys.stderr)
                sys.exit(2)
            print(f"\nHTML report: {path.as_uri()}", file=note)
            if "--open" in args:
                print(_open_in_browser(path.as_uri()), file=note)
        return
    root = sys.argv[2]
    raw = sys.stdin.read()
    mode = str(cfg.get("MODEL_PICKER_MODE") or "advise").strip().lower()
    if mode in ("false", "0", "no"):
        mode = "off"
    if mode == "off":
        return
    if event == "session":
        handle_session(root, cfg)  # the rules first: break bookkeeping must never cost a session its guide
        try:
            start = json.loads(raw or "{}")
            mark_break(cfg, start.get("session_id") if isinstance(start, dict) else None)
        except Exception:
            pass
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
        if len(sys.argv) > 1 and sys.argv[1] == "prompt":
            pass  # a stamp-delivery hiccup must not put a "launch not checked" line on every prompt
        else:
            print(json.dumps({"systemMessage": f"model-picker: hook error ({type(exc).__name__}: {exc}); "
                                               "launch not checked."}))
