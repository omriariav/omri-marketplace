# model-picker

Applies Anthropic's [Which Claude model should you build with?](https://claude.dev/blog/which-claude-model-should-you-build-with/) guidance (Nov 2026) to every session and every agent launch.

## What it does

| Component | Behavior |
|-----------|----------|
| SessionStart hook | Injects the `<model-picker-guide>` rules from `guide.md` (model by task, effort before model, fan-out limits). |
| PreToolUse hook on `Agent`/`Task` | Checks subagent and teammate launches. A missing `model` gets a recommendation; a model two or more tiers off the task gets a note. Skips `fork` and agents whose definition file already pins `model`. |
| PreToolUse hook on `Workflow` | Flags `agent()` calls in workflow scripts that set no `model`. |
| `/model-picker:model-picker [task]` | Per-request recommendation (model, effort, why, optional split). No argument loads the rules (Codex/Cursor fallback). |
| `model-picker:scout` agent | Haiku read-only scout for search, logs, and test output. |

## Modes

Set in `settings.json` → `env`:

| Variable | Values | Default |
|----------|--------|---------|
| `MODEL_PICKER_MODE` | `advise`: note only, launch proceeds. `enforce`: deny launches without a model, with the recommendation in the reason. `auto`: fill in the recommended model. `off`: disable hooks. | `advise` |
| `MODEL_PICKER_INJECT` | `0` skips the SessionStart injection (per-request use only). | `1` |

The recommendation is a keyword heuristic over the agent's description and prompt, so `enforce` (the model chooses, with a hint) is safer than `auto` (the heuristic chooses).

Requires `python3`; without it the hooks exit silently.
