---
name: report
description: Show model-picker's usage report — agent launches, which models were used, how often the recommendation was followed, decisions-API stats, subagent token spend by model, and the estimated saving versus running everything on the session model. Use for "/model-picker:report", "how much did model-picker save", "which models did my agents use".
argument-hint: '[today|24h|7d|30d|all]'
---

# model-picker report

Run (default period `7d`; pass the user's period if given):

```bash
python3 <this skill's directory>/../../hooks/model_picker.py report <period>
```

Print its output as-is (it is already a short summary plus a Markdown table). Then add at most
two lines of your own:

- When quoting a saving, quote **Credited to model-picker**, not the total difference: the total
  includes agents whose own definition already chose a cheap model.
- The single most useful observation (for example: "8 of 11 launches without a model inherited
  opus; setting haiku on the search agents would have saved most of the remaining spend").
- If `Followed recommendation` is under 50% or many launches had no explicit model, suggest
  `MODEL_PICKER_MODE=enforce` or reviewing the agents involved.

For machine-readable output add `--json`. The ledger lives in `~/.config/model-picker/log.jsonl`
(override with `MODEL_PICKER_STATE_DIR`; disable with `MODEL_PICKER_LOG=0`). Savings are estimates:
the same tokens priced at the session model's list price.
