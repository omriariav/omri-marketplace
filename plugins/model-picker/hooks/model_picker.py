"""model-picker hook logic for Claude Code.

Usage: model_picker.py session|pretool PLUGIN_ROOT   (hook payload on stdin)

Environment:
  MODEL_PICKER_MODE    advise (default) | enforce | off
                       advise:  missing/mismatched model -> context note, launch proceeds
                       enforce: missing model -> launch denied with a recommendation
  MODEL_PICKER_INJECT  1 (default) | 0   inject the guide at SessionStart
"""

import json
import os
import re
import sys
from pathlib import Path

TIERS = ["haiku", "sonnet", "opus", "fable"]

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


def handle_agent(payload, mode):
    inp = payload.get("tool_input")
    if not isinstance(inp, dict):
        raise ValueError(f"unexpected tool_input type {type(inp).__name__}")
    agent_type = str(inp.get("subagent_type") or "")
    if agent_type.lower().split(":")[-1] in SKIP_TYPES:
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
    else:
        emit(context=f"model-picker: `{label}` launched with no explicit model. "
                     f"Recommended: `{rec}` ({why}). Pass `model` explicitly on future launches.")


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


def handle_workflow(payload, mode):
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
    except Exception as exc:  # never block a launch, but never fail silently either
        print(json.dumps({"systemMessage": f"model-picker: hook error ({type(exc).__name__}: {exc}); launch not checked."}))
