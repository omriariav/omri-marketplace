---
name: report
description: >
  Show model-picker's own usage report: agent launches it checked, which models subagents actually
  ran on, how often its recommendation was followed, decisions-API hit rate and latency, subagent
  token spend by model, local-model calls, and the saving credited to model-picker versus the
  session model. Use when the user asks what model-picker saved, whether subagents really use Haiku,
  which models their agents used, or for /model-picker:report, even without the word "report". For
  total session or account spend, use /usage, /cost, or an agent cost report instead.
argument-hint: '[today|24h|7d|30d|all]'
---

# model-picker report

Pick the period from the user's words, defaulting to `7d`: "today" → `today`, "yesterday" or
"last day" → `24h`, "this week" or "last week" → `7d`, "this month" or "last month" → `30d`,
"ever" or "since install" → `all`. Only those five values are accepted.

Run (the path is relative to the directory that contains this SKILL.md):

```bash
python3 ../../hooks/model_picker.py report <period> --html --open
```

Show the output as-is; it is already a short summary plus a Markdown table. It ends with the
`HTML report:` file link and one line saying whether the page opened in the browser; keep both and
don't claim it opened if that line says it could not. Leave out `--open` if the user asked for text only. Then add at most two
lines of your own:

1. The saving to quote is **Saved by model-picker** (the report's first line), not the total difference. The total also
   counts agents whose own definition already picked a cheap model, which would have saved money
   with or without model-picker, so quoting it overstates the plugin.
2. The single most useful observation, for example "8 of 11 launches without a model inherited opus;
   setting haiku on the search agents would have saved most of the remaining spend". If
   `Followed recommendation` is under 50%, or many launches had no explicit model, make that the
   observation and suggest `MODEL_PICKER_MODE=enforce` or reviewing those agents.

If it says no launches are logged yet, the usual reasons are that the session started before
model-picker was installed or updated (restart Claude Code), or that `MODEL_PICKER_LOG=0` is set.
Say which applies rather than reporting zero savings.

For raw numbers add `--json`. The ledger is `~/.config/model-picker/log.jsonl` (moved with
`MODEL_PICKER_STATE_DIR`). Savings are estimates: the same tokens priced at the session model's list
price, and a larger model might have used fewer tokens.
