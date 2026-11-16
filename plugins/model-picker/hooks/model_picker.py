"""model-picker hook logic for Claude Code.

Usage: model_picker.py session|pretool PLUGIN_ROOT   (hook payload on stdin)

Environment:
  MODEL_PICKER_MODE    advise (default) | enforce | auto | off
                       advise:  missing/mismatched model -> context note, launch proceeds
                       enforce: missing model -> launch denied with a recommendation
                       auto:    missing model -> recommended model is filled in
  MODEL_PICKER_INJECT  1 (default) | 0   inject the guide at SessionStart
"""

import json
import os
import re
import sys
from pathlib import Path

TIERS = ["haiku", "sonnet", "opus", "fable"]

# Agent types that never take a model (fork inherits the parent's by design).
SKIP_TYPES = {"fork"}
# Built-in read-only helpers: cheap-to-verify work.
READ_ONLY_TYPES = {"explore", "web-fetch", "claude-code-guide", "statusline-setup"}
PLANNING_TYPES = {"plan"}

RULES = [
    ("fable", r"\b(major refactor|large[- ]scale refactor|re-?architect|architecture plan|migrat(e|ion) (the|our|all|entire)|"
              r"deep research|scientific|quantitative analysis|long[- ]running|unattended|orchestrat|lead (the )?(subagents|agents|team))"),
    ("opus", r"\b(migrat|performance|optimi[sz]|debug|root cause|multi-?pr|financial model|computer use|screenshot loop|"
             r"vision|security review|design (the|a) system)"),
    ("haiku", r"\b(find|search|grep|locate|list (all|the)?|where is|which files?|read (the )?(logs?|output)|"
              r"test output|summari[sz]e|classif|tag|label|extract|triage|fetch|scan|count|inventory)\b"),
    ("sonnet", r"\b(fix|bug|implement|add (a|an|the)|write (tests?|code|docs?)|feature|review|draft|document|"
               r"slides?|spreadsheet|investigat|refactor)\b"),
]
EDIT_RE = re.compile(r"\b(edit|modify|implement|refactor|fix|write|change|patch|apply)\b")

REASONS = {
    "haiku": "read/search/summarize side work that is cheap to verify",
    "sonnet": "well-scoped coding, review, or drafting",
    "opus": "complex engineering or judgment (default when unsure)",
    "fable": "major refactor/migration, architecture, deep research, or leading many agents",
}


def recommend(text, agent_type=""):
    t = (agent_type or "").lower().split(":")[-1]
    if t in READ_ONLY_TYPES:
        return "haiku", f"`{agent_type}` is a read-only helper"
    if t in PLANNING_TYPES:
        return "opus", "planning needs judgment"
    low = text.lower()
    hits = {tier: bool(re.search(rx, low)) for tier, rx in RULES}
    if hits["fable"]:
        tier = "fable"
    elif hits["opus"]:
        tier = "opus"
    elif hits["haiku"] and not EDIT_RE.search(low):
        tier = "haiku"
    elif hits["sonnet"]:
        tier = "sonnet"
    else:
        tier = "opus"
    return tier, REASONS[tier]


def pinned_model(agent_type, cwd):
    """Return the model pinned in the agent's definition file, if one is found."""
    if not agent_type:
        return None
    name = agent_type.split(":")[-1]
    home = Path.home()
    candidates = [Path(cwd or ".") / ".claude/agents", home / ".claude/agents"]
    files = [d / f"{name}.md" for d in candidates]
    plugins = home / ".claude/plugins"
    if ":" in agent_type and plugins.is_dir():
        files += list(plugins.glob(f"**/agents/{name}.md"))[:20]
    for f in files:
        try:
            head = f.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        m = re.match(r"---\s*\n(.*?)\n---", head, re.DOTALL)
        if not m:
            continue
        mm = re.search(r"^model:\s*['\"]?([\w.-]+)", m.group(1), re.MULTILINE)
        if mm and mm.group(1).lower() != "inherit":
            return mm.group(1).lower()
    return None


def tier_of(model):
    model = (model or "").lower()
    for tier in TIERS:
        if tier in model:
            return tier
    return None


def emit(decision=None, reason=None, context=None, updated=None):
    out = {"hookEventName": "PreToolUse"}
    if decision:
        out["permissionDecision"] = decision
    if reason:
        out["permissionDecisionReason"] = reason
    if context:
        out["additionalContext"] = context
    if updated is not None:
        out["updatedInput"] = updated
    print(json.dumps({"hookSpecificOutput": out}))


def handle_agent(payload, mode):
    inp = payload.get("tool_input") or {}
    agent_type = inp.get("subagent_type") or ""
    if agent_type.lower() in SKIP_TYPES:
        return
    text = " ".join(str(inp.get(k) or "") for k in ("description", "prompt", "name"))
    rec, why = recommend(text, agent_type)
    model = inp.get("model")
    label = agent_type or "general-purpose"

    if model:
        chosen = tier_of(model)
        if chosen and abs(TIERS.index(chosen) - TIERS.index(rec)) >= 2:
            emit(context=f"model-picker: `{label}` launched on {chosen}, but the task reads as {rec} work ({why}). "
                         f"Prefer {rec} for similar launches unless you have a specific reason.")
        return

    if pinned_model(agent_type, payload.get("cwd")):
        return  # the agent definition already pins a model

    if mode == "enforce":
        emit("deny", f"model-picker: launch `{label}` with an explicit `model`. Recommended: `{rec}` ({why}). "
                     f"Re-issue the same call with model: \"{rec}\", or pick another tier if you have a reason.")
    elif mode == "auto":
        emit("allow", f"model-picker set model={rec}", updated={**inp, "model": rec})
    else:
        emit(context=f"model-picker: `{label}` launched without a model, so it inherits the session model. "
                     f"Recommended: `{rec}` ({why}). Pass `model` explicitly on future launches.")


def handle_workflow(payload, mode):
    script = (payload.get("tool_input") or {}).get("script") or ""
    if not script:
        return
    starts = [m.start() for m in re.finditer(r"\bagent\(", script)]
    missing = 0
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(script)
        if "model" not in script[s:min(end, s + 1500)]:
            missing += 1
    if not missing:
        return
    msg = (f"model-picker: {missing} of {len(starts)} agent() calls in this workflow set no `model`. "
           "Pass {model: 'haiku'} for search/read/summarize steps, 'sonnet' for scoped edits or review, "
           "'opus' for judgment, 'fable' only for the lead on major work.")
    if mode == "enforce":
        emit("deny", msg + " Add the model options and re-run.")
    else:
        emit(context=msg)


def handle_session(root):
    if os.environ.get("MODEL_PICKER_INJECT", "1") == "0":
        return
    try:
        raw = (Path(root) / "guide.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    m = re.search(r"<model-picker-guide>.*?</model-picker-guide>", raw, re.DOTALL)
    if not m:
        return
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": m.group(0)}}))


def main():
    event, root = sys.argv[1], sys.argv[2]
    raw = sys.stdin.read()
    mode = os.environ.get("MODEL_PICKER_MODE", "advise").strip().lower()
    if mode == "off":
        return
    if event == "session":
        handle_session(root)
        return
    payload = json.loads(raw or "{}")
    tool = payload.get("tool_name")
    if tool in ("Agent", "Task"):
        handle_agent(payload, mode)
    elif tool == "Workflow":
        handle_workflow(payload, mode)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
