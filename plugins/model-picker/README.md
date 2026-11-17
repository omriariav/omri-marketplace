# model-picker

Applies Anthropic's [Which Claude model should you build with?](https://claude.dev/blog/which-claude-model-should-you-build-with/) guidance (Nov 2026) to every session and every agent launch. Optionally hands the choice to a decisions API and adds a local-model tier.

## Install

```bash
claude plugin marketplace add omriariav/omri-marketplace
claude plugin install model-picker@omri-marketplace
```

## What it does

| Component | Behavior |
|-----------|----------|
| SessionStart hook | Injects the `<model-picker-guide>` rules from `guide.md` (model by task, effort before model, fan-out limits). |
| PreToolUse hook on `Agent`/`Task` | Checks subagent and teammate launches. A missing `model` gets a recommendation (tier + effort); a model two or more tiers off the task gets a note. Skips `fork`, built-ins that pick their own model, and agents whose definition pins `model`. |
| PreToolUse hook on `Workflow` | Flags `agent()` calls in workflow scripts that set no `model`. |
| `/model-picker:model-picker [task]` | Per-request recommendation (model, effort, why, source, optional split). No argument loads the rules (Codex/Cursor/Grok fallback). |
| `hooks/model_picker.py route "<task>"` | CLI: prints the recommendation as JSON. |
| `model-picker:scout` agent | Haiku read-only scout for search, logs, and test output. |
| SubagentStop hook | Records each finished subagent's tokens, model, cost, and the cost at the session model. |
| PostToolUse hook on `Agent` | With `MODEL_PICKER_STAMP=1`, stamps each subagent result with the model that actually ran, its effort, tokens, cost, and whether it followed the recommendation. |
| `ask_local` MCP tool | Runs tool-free text work on a local Ollama model at $0, stamped and logged. |
| `/model-picker:report [period]` | Launches, model mix, follow rate, router stats, spend by model, estimated saving; also opens an HTML version. |

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
| `MODEL_PICKER_ROUTER_PAUSE` | Minutes to skip router calls after 3 failures in a row (timeouts, connection or HTTP errors, a router/key command exiting non-zero) or one rejected request (HTTP 400/401/403/404/405/407). Connection failures and rejections pause every call (launches use the keyword heuristic, the prompt line stays off); repeated prompt or launch failures pause only that kind of call (see the decisions API section). One line tells you when a pause starts. Shared by all sessions (`router-health.json` in the state dir; delete it to retry at once). `0` never pauses | `10` |
| `MODEL_PICKER_ROUTER_MIN_CONFIDENCE` | Below this, the heuristic decides | `0.5` |
| `MODEL_PICKER_ROUTER_ON_EXPLICIT` | `1` also asks the router about launches that already set a model, so every launch gets a real verdict (follow rate and credited savings depend on it). Costs one router call (~0.7 s) per launch | `0` |
| `MODEL_PICKER_LOG` | `0` turns off the local ledger | `1` |
| `MODEL_PICKER_STATE_DIR` | Where the ledger lives | `~/.config/model-picker` |
| `MODEL_PICKER_VERBOSE` | `1` shows a one-line note per launch and per finished subagent | `0` |
| `MODEL_PICKER_PRICES` | JSON price overrides per tier, $ per million tokens | built-in list prices |
| `MODEL_PICKER_STAMP` | `1` stamps subagent results with the model and effort that actually ran, and whether that followed the recommendation | `0` |
| `MODEL_PICKER_STAMP_REPLIES` | `1` asks Claude to end every reply with its model id | `0` |
| `MODEL_PICKER_OLLAMA_MODEL` | Ollama model for `ask_local`; also turns on the `local` tier | unset |
| `MODEL_PICKER_OLLAMA_URL` | Ollama base URL | `http://localhost:11434` |
| `MODEL_PICKER_OLLAMA_TIMEOUT` | Seconds per `ask_local` call | `120` |
| `MODEL_PICKER_OLLAMA` | `0` hides the `ask_local` tools | `1` |
| `MODEL_PICKER_TIERS` | Tiers this environment can launch, e.g. `haiku,sonnet,opus` where Fable isn't enabled. Others are never recommended or offered to the router. A missing tier is replaced by the next stronger available one; only when nothing stronger exists (e.g. no Fable) does work step down, at high effort. A value naming no Claude tier is ignored with a warning | all |
| `MODEL_PICKER_FLOORS` | JSON `{"pattern": "tier"}`: your own minimum model per kind of task, e.g. `{"review": "opus"}`. Matched against the launch description and agent type (first 300 characters); a plain word matches at a word start, anything else is a regex and runs as written. Never lowers a recommendation. A launch on the floored tier counts as followed, and its saving is listed under your rule, not credited to model-picker | unset |
| `MODEL_PICKER_PROMPT_ROUTE` | `1` shows, after each prompt, the decisions API's raw pick for it (`model-picker: gpt-6-luna → sonnet/high (0.96, 521 ms)`). You see it, Claude doesn't. Needs a router; only the prompt's first 200 characters are sent, whatever `MODEL_PICKER_ROUTER_INPUT` says. Skips slash commands, agent notifications and replies under three words | `0` |

API keys are read from the environment or `API_KEY_CMD`, never from the config file.

## Decisions APIs

A decisions API answers typed questions (here: which tier, which effort) with calibrated probabilities and no generated text, in roughly 100 ms. The router asks two choice questions, takes the answer when confidence clears the threshold, and otherwise falls back to the keyword heuristic. Router errors never block a launch; the recommendation's `source` says what decided. A router that keeps failing (say, a gateway behind a VPN that is off) is paused for `MODEL_PICKER_ROUTER_PAUSE` minutes, so prompts and launches stop waiting on it; 3 failed prompts in a row pause just the prompt line (prompts get only 3 s), and 3 failed launches in a row pause just launches; connection failures and rejections from either also count toward pausing everything. After a pause, calls try again; an answer clears the count for its own kind of call and the shared one.

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

## Visibility and savings

Every checked launch and every finished subagent is appended to a local ledger, `~/.config/model-picker/log.jsonl` (mode 600, rotated at 5 MB). Nothing leaves the machine.

- **Decisions** (PreToolUse): time, session, agent type, the short description (never the prompt), the model chosen (or the session model it inherited), the recommended tier and effort, what decided it (decisions API or heuristic) with confidence and latency, and whether the launch followed the recommendation. Skipped and pinned launches are logged too.
- **Usage** (SubagentStop): the subagent's tokens (input, output, cache reads, 5-minute and 1-hour cache writes), deduplicated per API message, read from its transcript. It is joined to the launch through the transcript's `toolUseId`.
- **Cost and saving**: each subagent is priced at its actual model and again at the session model, which it would have inherited without an explicit `model`. A local model (no Claude tier in its id) counts as $0; a Claude model with no known price claims no saving.
- **Attribution**: a recommendation that is only the "opus when unsure" fallback never judges an explicit model (listed as unjudged). Otherwise, only subagents whose explicit model matched model-picker's recommendation, and whose agent definition did not already set that model, are credited to model-picker. Agents pinned by their own definition (like `model-picker:scout`), explicit models that ignored the recommendation, inherited models, and launches with no record are listed separately and not credited.

Rotated files are kept as `log.<epoch>.jsonl` (newest 10); writes are serialized with a file lock, so parallel subagents never interleave or lose lines. A subagent resumed and stopped again is counted once (its latest, cumulative usage); a launch denied in `enforce` mode and retried counts once.

`/model-picker:report 7d` (or `today`, `24h`, `30d`, `all`; `--json` for raw numbers) prints the summary below and opens the same numbers as an HTML page (`~/.config/model-picker/report.html`: one self-contained, owner-only file with no network requests, in light or dark mode):

```markdown
**model-picker report (7d)**

- Launches: 23 (19 checked, 4 skipped as fork/pinned/built-in)
- Explicit model: 17/19 (89%)
- Followed recommendation: 14/17 (82%)
- Launch models: haiku 11, sonnet 5, opus 3
- Router: 16/17 answered by the decisions API (p50 720 ms, avg confidence 0.9); rest by heuristic

| Model | Agents | Input | Output | Cache read | Cost | At session model | Saved |
|---|---:|---:|---:|---:|---:|---:|---:|
| sonnet | 5 | 410.2K | 38.1K | 2.10M | $1.43 | $2.86 | $1.43 |
| haiku | 11 | 320.5K | 61.0K | 3.40M | $0.10 | $3.87 | $3.77 |

- Subagent spend: $1.53; at the session model it would have been $6.73. Total difference: $5.20 (77%).
- **Credited to model-picker: $3.94** (13 agent(s) that followed its recommendation).
  - Not credited, model set by the agent's own definition (would have happened anyway): 3 agent(s), $1.26
```

(Illustrative numbers.) Prices are Claude API list prices per million tokens: Haiku 5.5 $0.10 input / $0.50 output for prompts up to 100K tokens ($0.50 / $2.50 above), Sonnet 5.5 $2 / $10, Opus 5.5 $4 / $20, Fable 5.2 $8 / $40, with cache reads at $0.01 (Haiku), $0.10, $0.20, $0.20. Cache writes use the standard 1.25x (5-minute) and 2x (1-hour) input multipliers. The saving is an estimate: same tokens, different price. A larger model may finish in fewer tokens, so treat it as an upper bound.

With `MODEL_PICKER_VERBOSE=1` you also see each decision as it happens:

```
model-picker: model-picker:scout on haiku → recommends haiku/medium (jev-latest 0.97, 640 ms)
model-picker: model-picker:scout on claude-haiku-5-5 used 247,607 tokens, ~$0.009 vs ~$0.337 on opus
```

## Model stamps

Asking a model which model it is gives unreliable answers, and behind a gateway it is simply wrong. With `MODEL_PICKER_STAMP=1`, the model name comes from the transcript instead: the API response records exactly which model answered.

- **Foreground subagents:** when the `Agent` call returns, a line like this is shown to you and added to Claude's context, and Claude is told to name that model when it reports the result:
  `[model-picker] general-purpose ran on claude-haiku-5-5 at low effort · 13K tokens · ~$0.001 (~$0.055 on opus) · ✓ followed gpt-6-luna (haiku/low)`
- **Background subagents:** their `Agent` call returns at launch, so the stamp is queued when they finish (SubagentStop) and delivered on the session's next turn (UserPromptSubmit), which also fires when the agent's result notification arrives. Claude sees it next to the agent's report and names the model; you see it too. Queued stamps sit in `pending-stamps.jsonl` (24 h) and are delivered exactly once to their own session; a resumed agent's newer stamp is delivered again. Stamps don't carry over after `/clear`.
- **`ask_local` answers** end with `[model-picker] answered by ollama:<model> · N in / M out tokens · $0 · Xs`.
- **Replies (opt-in):** `MODEL_PICKER_STAMP_REPLIES=1` asks Claude to end every reply with its model id, taken from its system prompt. Off by default because it adds a line to every reply.

## Local models

### Bundled Ollama tool (`ask_local`)

The plugin ships a small stdlib MCP server (`mcp/local_server.py`) with two tools: `ask_local(prompt, system?, max_tokens?, model?)` and `list_local_models()`. Set `MODEL_PICKER_OLLAMA_MODEL` (for example `qwen2.5:7b-instruct`) and:

- the `local` tier turns on: the session rules tell Claude to consider calling `ask_local` directly, instead of launching an agent, for tool-free text work on text it already has (summarize, classify, extract, translate, rephrase), and `/model-picker:model-picker` can recommend it. Agent launches themselves are never recommended `local` (the agent is already being launched with tools), so the launch hook can't misroute tool work to a model that has none;
- each call is logged to the ledger (model, token counts, seconds; never the prompt or answer) and shows up in `/model-picker:report` with what it would have cost at Haiku prices.

Without a configured model the tools still list but return a clear error naming the installed models; with Ollama stopped they return "not reachable" instead of failing. `MODEL_PICKER_OLLAMA=0` hides them. Small local models are fine for simple transformation and weak at reasoning or long structured writing, so check what they return.

### Other routes

Claude Code sends every request to one Anthropic-format endpoint, so beyond `ask_local` a local model is reached either through a gateway that routes by model name (set `ANTHROPIC_BASE_URL` to the gateway and `ANTHROPIC_DEFAULT_HAIKU_MODEL` to the local model's name, so `model: haiku` runs locally) or through an MCP tool that wraps the local server. Set `MODEL_PICKER_LOCAL_HINT` to describe whichever you use. The session rules and `/model-picker:model-picker` then offer `local` for pure text work on text already in hand (summarize, classify, extract, translate); agent launches and workflow steps are never routed to it, since a local model has no tools.

Tests: `python3 plugins/model-picker/tests/test_model_picker.py`. Requires `python3` (stdlib only); hooks exit silently without it.
