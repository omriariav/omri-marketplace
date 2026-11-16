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
| `MODEL_PICKER_MODE` | `advise`: note only, launch proceeds. `enforce`: deny launches without a model, with the recommendation in the reason. `off`: disable hooks. | `advise` |
| `MODEL_PICKER_INJECT` | `0` skips the SessionStart injection (per-request use only). | `1` |

The recommendation is a keyword heuristic over the agent's description and prompt, so the hook only advises or blocks. The launching model always picks the final tier. Plugin agents are resolved from `installed_plugins.json` and org-synced plugins, and project agents from `$CLAUDE_PROJECT_DIR/.claude/agents`, matched by frontmatter `name`. On an internal error the hook shows a `systemMessage` and lets the launch through.

Tests: `python3 plugins/model-picker/tests/test_model_picker.py`

Requires `python3`; without it the hooks exit silently.
