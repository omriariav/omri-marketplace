---
name: model-picker
description: >
  Pick the right Claude model and effort level for a task, subagent, teammate, or workflow
  step, following Anthropic's "Which Claude model should you build with?" guidance. Use when
  the user asks which model to use, wants a per-request recommendation, asks to load the
  model-selection rules, or is about to launch agents, a team, or a workflow.
argument-hint: '[task description | load]'
---

# Model picker

## Load (no argument, or `load`)

Read [`../../guide.md`](../../guide.md) and follow every rule in the `<model-picker-guide>`
block for the rest of the session. Reply with one line: `model-picker: rules loaded.`
Claude Code loads these rules at session start through the plugin hook; other agents use this.

## Recommend (argument is a task)

First run the router (it uses the configured decision API, or the keyword heuristic):

```bash
python3 <this skill's directory>/../../hooks/model_picker.py route <<'TASK'
<the task, one or two sentences>
TASK
```

Always pass the task on stdin with the quoted `'TASK'` delimiter, never as a shell argument, so quotes,
`$`, and backticks in the task are not expanded.

It prints JSON with `tier`, `effort`, `reason`, and `source`. Treat it as a strong prior, not a
verdict: override it only when the guide clearly disagrees, and say so in `Why`. If the command
fails, decide from the guide alone. A `local` tier means use the local model the way the
session-start note or `MODEL_PICKER_LOCAL_HINT` describes.

Answer in this exact shape and nothing else:

```
Model:  <local|haiku|sonnet|opus|fable>  (local / Haiku 5.5 / Sonnet 5.5 / Opus 5.5 / Fable 5.2)
Effort: <low|medium|high>
Source: <router source from the JSON, or "guide">
Why:    <one line tied to the task>
Split:  <only if delegation helps: which parts go to which model; otherwise omit>
```

Rules for the recommendation:
- Start from the task table in the guide; when unsure, opus at medium.
- A clear spec alone does not make a task sonnet work: migrations and performance work stay on opus (fable if major).
- Recommend effort changes before model changes. Past high, recommend the next model up at lower effort.
- Recommend a split only for work larger than one context window or with clearly separable read-only side work (search, logs, summaries → haiku).

## Report

For usage, spend, and savings, use `/model-picker:report` instead.

## Launching agents

When this skill is active and you launch an Agent, teammate, or workflow `agent()`, always pass
`model` explicitly, chosen with the same rules. Keep code edits and judgment on the main model.
