---
name: model-picker
description: >
  Pick the right Claude model (Haiku, Sonnet, Opus, Fable, or a local model) and effort level for
  a specific task, subagent, teammate, or workflow step, using Anthropic's model-selection guidance
  and the configured decisions API. Use whenever the user asks which model or effort to use, whether
  a model is overkill or too weak for something, how to split work across cheaper and stronger
  models, or asks to load the model-selection rules (e.g. in Codex or Cursor). Not for choosing model
  IDs, pricing or parameters inside an app built on the Claude API (use claude-api), and not needed
  before every agent launch in Claude Code, where the plugin's hooks already apply the rules.
argument-hint: '[task description | load]'
---

# Model picker

Paths below are relative to the directory that contains this SKILL.md.

## Load (no argument, or `load`)

Read [`../../guide.md`](../../guide.md) and follow every rule in its `<model-picker-guide>` block
for the rest of the session, then reply with one line: `model-picker: rules loaded.`

Claude Code already loads these rules at session start through the plugin's hook; this path exists
for agents without that hook (Codex, Cursor, Grok) and for re-anchoring a long session.

## Recommend (argument is a task)

Get a recommendation from the router, which uses the configured decisions API and falls back to a
keyword heuristic:

```bash
python3 ../../hooks/model_picker.py route <<'TASK'
<the task in one or two plain sentences>
TASK
```

Two things about that call matter:

- **What you send may leave the machine.** With a decisions API configured, the text goes to that
  service. Describe the kind of work generically ("find where the login handler is defined",
  "refactor the payment module's retry logic") and leave out code, file contents, customer data,
  names, and secrets. The router only needs the shape of the task, not its content.
- **Pass it on stdin with the quoted `'TASK'` delimiter**, never as a shell argument, so quotes, `$`,
  and backticks in the description are not expanded by the shell.

It prints JSON with `tier`, `effort`, `reason`, and `source`. Treat it as a strong prior, not a
verdict: override it when the guide clearly disagrees, and say why. If the command fails, decide
from the guide alone and say so in `Source`.

Start your answer with this block:

```
Model:  <local|haiku|sonnet|opus|fable>  (local / Haiku 5.5 / Sonnet 5.5 / Opus 5.5 / Fable 5.2)
Effort: <low|medium|high>
Source: <router source from the JSON, or "guide">
Why:    <one line tied to the task>
Split:  <only if delegation helps: which parts go to which model; otherwise omit>
```

If the user only asked for advice, stop there. If they asked you to do the work too ("use
model-picker for this and build it"), continue and apply the recommendation; the block is the plan,
not the end of the turn.

How to decide:
- Start from the task table in the guide; when unsure, opus at medium.
- A clear spec alone does not make a task sonnet work: migrations and performance work stay on opus
  (fable if major).
- Prefer an effort change to a model change. Past high, recommend the next model up at lower effort.
- Recommend a split only for work larger than one context window or with clearly separable
  read-only side work (search, logs, summaries go to haiku). Keep code edits and judgment on the
  main model, which already holds the context.

What the tiers mean in practice:
- **local** means calling the `ask_local` tool (or whatever the session-start note describes) with
  the text pasted into the prompt itself. It cannot open files, URLs, PRs, or tickets, and small
  local models are weak at reasoning, so use it only for transforming text you already have
  (summarize, classify, extract, translate) and check the result.
- **Effort** applies to the session (`/effort`) or an API call. Claude Code's Agent tool sets effort
  only when the user explicitly asks for it, so for a subagent, treat the effort line as advice
  unless the user has asked you to set it.

## Launching agents

When you launch an Agent, teammate, or workflow `agent()`, pass `model` explicitly, chosen with the
same rules. Without it the agent inherits the session model, which is usually the most expensive one.

## Usage and savings

For what was launched, which models actually ran, and what that saved, use `/model-picker:report`.
