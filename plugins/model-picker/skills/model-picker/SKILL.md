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

Using the guide, answer in this exact shape and nothing else:

```
Model:  <haiku|sonnet|opus|fable>  (Haiku 5.5 / Sonnet 5.5 / Opus 5.5 / Fable 5.2)
Effort: <low|medium|high>
Why:    <one line tied to the task>
Split:  <only if delegation helps: which parts go to which model; otherwise omit>
```

Rules for the recommendation:
- Start from the task table in the guide; when unsure, opus at medium.
- A clear spec alone does not make a task sonnet work: migrations and performance work stay on opus (fable if major).
- Recommend effort changes before model changes. Past high, recommend the next model up at lower effort.
- Recommend a split only for work larger than one context window or with clearly separable read-only side work (search, logs, summaries → haiku).

## Launching agents

When this skill is active and you launch an Agent, teammate, or workflow `agent()`, always pass
`model` explicitly, chosen with the same rules. Keep code edits and judgment on the main model.
