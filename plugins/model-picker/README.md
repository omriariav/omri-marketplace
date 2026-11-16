# model-picker

Applies Anthropic's [Which Claude model should you build with?](https://claude.dev/blog/which-claude-model-should-you-build-with/) guidance (Nov 2026) to every session and every agent launch. Optionally hands the choice to a decisions API and adds a local-model tier.

## What it does

| Component | Behavior |
|-----------|----------|
| SessionStart hook | Injects the `<model-picker-guide>` rules from `guide.md` (model by task, effort before model, fan-out limits). |
| PreToolUse hook on `Agent`/`Task` | Checks subagent and teammate launches. A missing `model` gets a recommendation (tier + effort); a model two or more tiers off the task gets a note. Skips `fork`, built-ins that pick their own model, and agents whose definition pins `model`. |
| PreToolUse hook on `Workflow` | Flags `agent()` calls in workflow scripts that set no `model`. |
| `/model-picker:model-picker [task]` | Per-request recommendation (model, effort, why, source, optional split). No argument loads the rules (Codex/Cursor/Grok fallback). |
| `hooks/model_picker.py route "<task>"` | CLI: prints the recommendation as JSON. |
| `model-picker:scout` agent | Haiku read-only scout for search, logs, and test output. |

## Settings

Set as environment variables (for example in `settings.json` → `env`), or as the same keys in a JSON file at `~/.config/model-picker/config.json` (override the path with `MODEL_PICKER_CONFIG`). Environment wins over the file. Keep company-specific gateway settings in that file, outside any repo.

| Variable | Values | Default |
|----------|--------|---------|
| `MODEL_PICKER_MODE` | `advise`: note only, launch proceeds. `enforce`: deny launches without a model, with the recommendation in the reason. `off`. | `advise` |
| `MODEL_PICKER_INJECT` | `0` skips the SessionStart injection. | `1` |
| `MODEL_PICKER_LOCAL_HINT` | Turns on the `local` tier. Text telling Claude how to reach the local model. | unset |
| `MODEL_PICKER_ROUTER` | `heuristic`, `openai-decisions`, `google-decisions`, `command` | `heuristic` |
| `MODEL_PICKER_ROUTER_URL` | Base URL. Point `openai-decisions` at any LiteLLM gateway that serves `/v1/decisions`. | vendor URL |
| `MODEL_PICKER_ROUTER_MODEL` | Decision model | `gpt-6-luna` / `gemini-decision-1` |
| `MODEL_PICKER_ROUTER_API_KEY_ENV` | Name of the env var that holds the key; `none` sends no auth header (gateways on a private network) | `OPENAI_API_KEY` / `GEMINI_API_KEY` |
| `MODEL_PICKER_ROUTER_API_KEY_CMD` | Command that prints the key (e.g. `security find-generic-password -w -s my-gateway`) | unset |
| `MODEL_PICKER_ROUTER_HEADERS` | JSON object of extra headers (attribution, redaction) | `{}` |
| `MODEL_PICKER_ROUTER_METADATA` | JSON object sent as request `metadata` (`openai-decisions`) | `{}` |
| `MODEL_PICKER_ROUTER_CMD` | Executable for `command`: JSON `{task, tiers, efforts}` on stdin, JSON `{tier, effort, confidence}` on stdout | unset |
| `MODEL_PICKER_ROUTER_INPUT` | What task text leaves the machine: `description` (agent's short description), `brief` (+ first 500 prompt chars), `full` (+ up to 8000) | `description` |
| `MODEL_PICKER_ROUTER_TIMEOUT` | Seconds | `3` |
| `MODEL_PICKER_ROUTER_MIN_CONFIDENCE` | Below this, the heuristic decides | `0.5` |

API keys are read from the environment or `API_KEY_CMD`, never from the config file.

## Decisions APIs

A decisions API answers typed questions (here: which tier, which effort) with calibrated probabilities and no generated text, in roughly 100 ms. The router asks two choice questions, takes the answer when confidence clears the threshold, and otherwise falls back to the keyword heuristic. Router errors never block a launch; the recommendation's `source` says what decided.

| Provider | Router | Notes |
|----------|--------|-------|
| [OpenAI Decisions](https://developers.openai.com/api/docs/guides/decisions) | `openai-decisions` | `POST /v1/decisions`, model `gpt-6-luna` |
| [Google Decisions](https://aistudio.google.com/docs/decisions-api) | `google-decisions` | `POST /v1beta/decisions`, model `gemini-decision-1` |
| [TypeSafe Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) | `openai-decisions` via a LiteLLM gateway that serves Jev on `/v1/decisions`, or `command` | Early access |
| Anything else | `command` | Wrap any provider or private gateway in a script |

**Privacy:** with a router configured, task text goes to that service. The default `description` input sends only the agent's short description. Use `brief` or `full` only with a provider you trust with your code, and add your gateway's redaction header through `MODEL_PICKER_ROUTER_HEADERS`.

Example (LiteLLM gateway, in `~/.config/model-picker/config.json`):

```json
{
  "MODEL_PICKER_ROUTER": "openai-decisions",
  "MODEL_PICKER_ROUTER_URL": "https://llm-gateway.example.com",
  "MODEL_PICKER_ROUTER_MODEL": "typesafe/jev-latest",
  "MODEL_PICKER_ROUTER_API_KEY_ENV": "LITELLM_API_KEY",
  "MODEL_PICKER_ROUTER_HEADERS": {"x-litellm-enable-message-redaction": "true"},
  "MODEL_PICKER_ROUTER_METADATA": {"tags": ["service:model-picker"]}
}
```

## Local models

Claude Code sends every request to one Anthropic-format endpoint, so a local model is reached either through a gateway that routes by model name (set `ANTHROPIC_BASE_URL` to the gateway and `ANTHROPIC_DEFAULT_HAIKU_MODEL` to the local model's name, so `model: haiku` runs locally) or through an MCP tool that wraps the local server. Set `MODEL_PICKER_LOCAL_HINT` to describe whichever you use. The plugin then recommends `local` only for pure text work that needs no tools or files (summarize, classify, extract, translate given text); search and file work stay on Haiku, since local models are weak at tool use.

Tests: `python3 plugins/model-picker/tests/test_model_picker.py`. Requires `python3` (stdlib only); hooks exit silently without it.
