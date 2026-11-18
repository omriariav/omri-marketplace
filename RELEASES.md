# Releases

## model-picker v0.5.1 (2026-11-18)

- **A label when the router can't be used.** If the decisions API can't be reached or refuses the call (a gateway behind a VPN that is off, a timeout, an HTTP error) or is paused after failing, a launch shows `model-picker: gpt-6-luna unavailable (timed out); keyword rules used`, and a prompt that would have been routed shows `model-picker: gpt-6-luna unavailable (unreachable, paused)`. Before, a failed call showed nothing until the third failure started a pause, and nothing during the pause, so a missing pick looked like a skipped one. The line that starts a pause still replaces the label. A router that answered, even unsure or unusably, gets no label. With `MODEL_PICKER_PROMPT_ROUTE=1` the prompt label appears only at a natural break, which stays open until the router answers.
- Tests: 341.

## model-picker v0.5.0 (2026-11-17)

- **The prompt line calls the router only at natural breaks.** With `MODEL_PICKER_PROMPT_ROUTE=1` the decisions API is called for a session's first prompt, the first after `/compact` or a resume, and the first after `MODEL_PICKER_PROMPT_ROUTE_IDLE` minutes (default 60) without a prompt, and not for mid-task prompts. Guide rule 7 says to switch the session model only at such breaks, so a mid-task pick was paid for and not acted on. In one working session the old behavior made 106 router calls, and the pick changed on 70 of 105 consecutive prompts. A break stays open until the router replies to an eligible prompt (an unusable reply also closes it), so a short reply, a slash command, a paused router or a connection failure doesn't use it up. `MODEL_PICKER_PROMPT_ROUTE=always` keeps the old every-prompt behavior. Launch routing is unchanged.
- Break state is stored per session under `prompt-breaks/` in the state dir (a timestamp and a flag, no prompt text). It is removed after a day idle.
- Tests: 338.

## model-picker v0.4.10 (2026-11-17)

- **High effort for work that's costly to get wrong.** Guide rule 1 now says `high` for subtle or costly-to-get-wrong work that no automatic check covers (reviews that gate a merge, security, debugging a hard failure), not only after a model fell short, and to take the higher of model-picker's effort and this rule's. In v0.4.9 such work defaulted to `medium` now that Claude passes effort explicitly instead of inheriting the session's.
- **Effort floors.** `MODEL_PICKER_FLOORS` values can name an effort: `{"review": "opus/high"}`. A matching launch is recommended at least that model and effort; a stronger model meets the floor at any effort (guide rule 3), and a floor never lowers either. If the floor's model isn't available here, the nearest available one stands in (a weaker stand-in needs at least high). An explicit launch below the floor, including one with no effort set, is reminded of your rule. A floor that only raised effort doesn't take model-picker's credit for the model, and the stamp compares effort whenever a floor set it.
- Step-downs to a weaker available model keep an effort above `high` (`xhigh`, `max`) instead of capping it at `high`.
- Tests: 325.

## model-picker v0.4.9 (2026-11-17)

- **You asked for it.** When your prompt names a model or effort (`run the search on haiku`, `use opus at high effort`), Claude follows it (the guide says your ask wins) and model-picker labels it, without ever steering a launch from its own reading of the prompt: a launch on the model you asked for gets no advice, its stamp ends `· your call (opus/high effort); gpt-6-luna said sonnet/medium`, the report lists it as your pick (outside the follow rate, savings not credited to model-picker), and the router's prompt line ends `· you asked for opus`. Detection is narrow: a verb must come first, and questions, negations, code, double and curly quotes, quoted lines and pasted model-picker output are skipped; any second model named means no pick; an effort-only ask doesn't mark launches. Stored per session under `asked/` (no prompt text); slash commands clear it, agent notifications keep it; off with `MODEL_PICKER_ASK=0` or `MODEL_PICKER_LOG=0`.
- **Guide: pass `effort` too.** Rule 1 now says to pass an explicit `model` and `effort` on every launch, as an explicit instruction: `low` for haiku-row side work, `medium` otherwise, `high` where the guide's effort rules say so, model-picker's recommendation when it gives one, and the user's ask above all. Claude Code only lets Claude set a subagent's effort when instructions ask for it, so without this rule subagents inherited the session's effort.
- Labels: the prompt line and the verbose launch note name the recommender the way the stamp does (`gpt-6-luna`, `command router`, `keyword rules`).
- Docs: by default a launch that sets a model is judged by the keyword rules; the router judges it only with `MODEL_PICKER_ROUTER_ON_EXPLICIT=1`.
- Tests: 310.

## model-picker v0.4.8 (2026-11-17)

Follow-ups to the v0.4.7 router pause. Each kind of call now keeps its own count, so "3 in a row" is true for what it pauses:

- **Prompt line:** every prompt failure counts, timeouts included; 3 in a row pause the prompt line. A prompt answer clears it. Before, connect failures and timeouts went to separate counts, so alternating them took up to 5 slow prompts.
- **Agent launches:** every launch failure counts; 3 in a row pause launches (they use the keyword rules meanwhile, and the prompt line keeps asking). Only a launch answer clears it. Before, a short prompt answered in time cleared everything, so with long launch input (`MODEL_PICKER_ROUTER_INPUT=full`) launches that kept timing out never paused.
- **Everything:** connection failures and rejections from either kind of call also count toward pausing all router calls, as in v0.4.7; any answer clears that count. Unlike v0.4.7, launch timeouts no longer pause the prompt line. When two pauses start on the same call you get one line, and a pause line says whether the other kind of call is already paused.
- **Stamps show effort and the verdict.** With `MODEL_PICKER_STAMP=1` the stamp now says the effort the subagent ran at (from its transcript, else the launch's `effort`) and whether the launch followed the recommendation, naming who made it: `[model-picker] general-purpose ran on claude-opus-5-5 at high effort · 120K tokens · ~$0.41 · ✓ followed gpt-6-luna (opus/high)`. Other endings: `✓ model as … recommended (opus/high), effort differs`, `✗ … recommended sonnet/medium`, `keyword rules only guessed …` (no confident pick), `no model set; … recommended …` (the launch inherited the session model, so it isn't judged, matching the report's follow rate), or nothing for agents pinned by their definition. Effort is compared only when a router chose it; the keyword rules name a model only. Launch records now keep the requested `effort`.
- v0.4.7 notes corrected: with the default `MODEL_PICKER_ROUTER_TIMEOUT` (3 s) launches get about as long as prompts; raising it (up to about 6 s, the launch hook's cap) lets a slower router keep serving launches.
- Tests: 285.

## model-picker v0.4.7 (2026-11-17)

- **Router pause.** A router that can't be reached no longer slows every prompt and launch. After 3 failures in a row (connection errors, timeouts, HTTP errors, or a router/key command that exits non-zero) router calls pause for 10 minutes (`MODEL_PICKER_ROUTER_PAUSE`, minutes, capped at a day; `0` never pauses), with one line telling you so. A rejected request that a retry can't fix (HTTP 400/401/403/404/405/407) pauses at once and points at the key and header settings. While paused, launches use the keyword heuristic (`source: heuristic (router paused: …)`) and the prompt line isn't shown. Prompts get 3 s in all (2.5 s per request); launches get `MODEL_PICKER_ROUTER_TIMEOUT` (default 3 s) per request. So 3 prompt timeouts in a row pause only the prompt line, which shows nothing on a timeout anyway, and launches keep asking the router; a router slower than 3 s then still serves launches only if you raise `MODEL_PICKER_ROUTER_TIMEOUT` (up to about 6 s, the launch hook's cap). After a pause, calls try again: a success clears the count, and a failure of the same kind (can't reach vs rejected) pauses again without a new message. A malformed answer or a missing API key doesn't count (neither costs any time). State is shared by all sessions and both hooks, under a lock, in `router-health.json` in the state dir (written even with `MODEL_PICKER_LOG=0`; delete it to retry at once), with one entry per router fingerprint: a hash of the router, key and header settings and of the key itself, so sessions with different keys don't share a pause. Failures older than one pause are forgotten, and entries idle for a day are dropped. The `route` CLI behind `/model-picker` honors a pause but never starts one.
- The ledger's state dir now also honors `MODEL_PICKER_STATE_DIR` from the environment when the config passed in lacks it.
- Tests: 264.

## model-picker v0.4.6 (2026-11-16)

- **Followed, by direction.** `/model-picker:report` splits launches that didn't follow the recommendation into "went to a stronger model" and "went to a cheaper one". Going stronger is often deliberate (a stated reason, a user rule) and costs money; going cheaper saves money but is the risky direction. A low follow rate made mostly of deliberate upgrades no longer reads like ignored advice.
- **Your own minimum models: `MODEL_PICKER_FLOORS`.** JSON `{"pattern": "tier"}`, e.g. `{"review": "opus"}`. A launch whose description or agent type matches is recommended at least that tier, so a standing rule like "reviews run on Opus" counts as followed instead of being nudged down; savings on such launches are listed under your rule, not credited to model-picker. A plain word matches at a word start (`review` matches "reviews", not "preview"); anything else is a regex. A floor never lowers a recommendation.
- **Tie-break in the guide.** When a task fits more than one row, pick the row for its hardest part: slides rendered and checked by eye are a vision loop (Opus), not just slides (Sonnet).
- **The router's pick for each prompt: `MODEL_PICKER_PROMPT_ROUTE=1`** (off by default). After each prompt you see one line with the decisions API's raw answer, e.g. `model-picker: gpt-6-luna → sonnet/high (0.96, 521 ms)`: no heuristic fallback, no floors. Shown to you only; Claude doesn't see it. Needs a configured router; only the prompt's first 200 characters are sent to it, whatever `MODEL_PICKER_ROUTER_INPUT` says (3 s budget; nothing is shown if it doesn't answer). Slash commands, agent notifications and replies under three words are skipped. Each pick is logged without the prompt text, and the report counts them.
- Tests: 226.

## model-picker v0.4.5 (2026-11-16)

`/model-picker:report` now also writes an HTML report and opens it in the browser: headline tiles (subagent spend, cost at the session model, saving credited to model-picker, follow rate), recommended vs launched models, spend by model, and who gets credit for each saving. It is one self-contained file (no scripts, fonts or network requests), owner-only (`0600`), at `~/.config/model-picker/report.html` (or under `MODEL_PICKER_STATE_DIR`), and follows the system light/dark mode. CLI: `model_picker.py report <period> --html [--out PATH] [--open]`.

## model-picker v0.4.4 (2026-11-16)

Fix: stamps for background agents never reached anyone. Claude Code runs most agents in the background, and their stamp was only a SubagentStop `systemMessage`, which Claude never receives and the user didn't see.

- Background stamps are now queued when the agent finishes and delivered on the session's next turn through a new UserPromptSubmit hook, which also fires when the agent's result notification arrives. Claude gets the stamp next to the agent's report, with a rule to name that model.
- Queued stamps live in a small `pending-stamps.jsonl` (kept 24 h), read in full under a lock: each is delivered exactly once to its own session (8 racing turns tested), a resumed agent's newer stamp is delivered again, and a failed bookkeeping write delivers nothing rather than repeating on every prompt. With `MODEL_PICKER_LOG=0` the stamp falls back to a plain user-visible line. Off unless `MODEL_PICKER_STAMP=1`. Stamps don't carry over after `/clear` or into a new session.
- Agent launches are never recommended `local`. By the time the launch hook runs, Claude is already launching a tool-using agent, and guessing "call ask_local instead" from a one-line description sent tool work to a model with no tools (found in a real session: Jev picked `local` at 0.60 for what was a code-and-git task; three rounds of text-matching rules couldn't make that guess safe). The local tier is offered where the choice is still open: the session rules ("before launching an agent, consider ask_local for pasted text") and `/model-picker:model-picker`, where a `local` pick below 0.75 confidence falls back to a Claude tier.
- `ledger.read()` skips rotated files older than the requested window by their name; a tail-read edge case no longer drops a complete record.
- Smoke-tested three times in real headless sessions: a background Haiku agent's stamp was quoted verbatim by Claude when its result arrived.
- Tests: 190 (incl. 27 launch prompts that must never get `local` even when the router answers `local`).

## model-picker v0.4.3 (2026-11-16)

Fix: `/model-picker:report` output rendered as one run-together paragraph in Markdown views. The summary is now a bullet list with a bold title, blank lines around the table, and the not-credited breakdown nested under the credited saving. Tests: 162.

## model-picker v0.4.2 (2026-11-16)

Fixes from a post-release review of v0.4.1's `MODEL_PICKER_TIERS`.

- A missing tier now goes to the next *stronger* available tier, keeping effort; stepping down at high effort only happens when nothing stronger exists (e.g. Fable not enabled → Opus high). Previously a missing Sonnet went to Haiku.
- The local tier is only advertised (session rules, workflow advice, recommendations) when `local` is in the allowed tiers.
- Workflow advice lists only available tiers instead of always naming Fable.
- `enforce` mode now also denies an explicit launch on an unavailable model; agents pinned to an unavailable model get a note (deny in `enforce`) with the `model` to pass instead.
- A `MODEL_PICKER_TIERS` value naming no Claude tier is ignored with a session warning instead of silently allowing every tier; non-string list items no longer crash the hook.
- New opt-in `MODEL_PICKER_ROUTER_ON_EXPLICIT=1`: the router also judges launches that already set a model (about 0.7 s each), so the follow rate and credited savings reflect a real verdict instead of mostly "unjudged".
- Pre-merge review fixes: session rules describe the step-up rule; a launch denied in `enforce` and retried counts once; a model name that merely contains "local" is not the local tier.
- Tests: 159.

## model-picker v0.4.1 (2026-11-16)

Respect the models an environment can actually launch.

- New `MODEL_PICKER_TIERS` (e.g. `haiku,sonnet,opus`) for setups where a tier isn't enabled, such as Claude Code accounts without Fable. Missing tiers are never recommended, never offered to the decisions API, and the session rules name them.
- Work meant for a missing higher tier goes to the next tier down at high effort (Fable-level work: Opus at high effort), following the guide's "change effort before changing models"; a missing cheaper tier moves up and keeps its effort.
- An explicit launch on an unavailable tier gets a note with the available alternative instead of a silent failure.
- Default unchanged: all tiers. Tests: 137.

## model-picker v0.4.0 (2026-11-16)

Model stamps and a bundled local-model tool.

- `MODEL_PICKER_STAMP=1`: each subagent result is stamped with the model that actually answered, read from its transcript (not self-reported), plus tokens and cost, e.g. `[model-picker] web-fetch ran on claude-haiku-5-5 · 13K tokens · ~$0.001 (~$0.055 on opus)`. Foreground agents are stamped when the `Agent` call returns (new PostToolUse hook, visible to you and to Claude); background agents when they finish. Never twice.
- `MODEL_PICKER_STAMP_REPLIES=1` (opt-in) asks Claude to end every reply with its model id.
- New bundled MCP server with `ask_local` and `list_local_models`, backed by Ollama (stdlib only). Setting `MODEL_PICKER_OLLAMA_MODEL` turns on the `local` tier, so tool-free text work goes to a $0 local model instead of a subagent. Answers carry a stamp; calls are logged (no prompt or answer text) and reported with their Haiku-price equivalent.
- Clear errors when no model is configured (lists installed models) or Ollama is down; `MODEL_PICKER_OLLAMA=0` hides the tools.
- The local tier is never recommended for work that names a file, path, URL, PR, ticket, doc or page: `ask_local` only sees text pasted into its prompt.
- Robust MCP server: malformed requests get JSON-RPC errors instead of killing it, and Ollama HTTP errors (e.g. a model not pulled) are reported as such, not as "not reachable".
- When the recommendation is only the "opus when unsure" fallback, it no longer judges an explicit model: no upward nudge, no "not followed", and the saving is listed as unjudged rather than credited or blamed. Plain reading verbs (read, first line, look up, check whether) now count as Haiku work.
- Verified in real headless Claude Code sessions: `ask_local` registers as `mcp__plugin_model-picker_local__ask_local`, and a foreground Agent launch produced the decision, usage, and stamp records, with Claude naming the stamped model.
- Tests: 124 (from 85), including a JSON-RPC session against a mock Ollama, malformed requests, HTTP 404s, stamp placement for foreground, background and unknown-shape agents, and session-rule injection.

## model-picker v0.3.0 (2026-11-16)

Visibility: what model-picker decided, which models ran, and what that saved.

- Local ledger at `~/.config/model-picker/log.jsonl` (mode 600, rotated at 5 MB; nothing leaves the machine). Every checked launch logs the agent type, short description (never the prompt), the chosen or inherited model, the recommended tier and effort, what decided it (decisions API or heuristic) with confidence and latency, and whether the recommendation was followed.
- New SubagentStop hook reads each finished subagent's transcript, counts tokens once per API message (input, output, cache reads, 5-minute and 1-hour cache writes), and joins them to the launch via the transcript's `toolUseId`.
- Cost and estimated saving per subagent: priced at its actual model and at the session model (as of launch) it would otherwise have inherited, using Claude API list prices (Haiku's >100K-prompt tier included). Local models count as $0. Override prices with `MODEL_PICKER_PRICES`.
- Honest attribution: only subagents that followed model-picker's recommendation, on a model their own definition didn't already set, are credited. Definition-pinned agents, ignored recommendations, inherited models, and unrecorded launches are shown separately.
- `/model-picker:report [today|24h|7d|30d|all]` (and `model_picker.py report --json`): launches, explicit-model and follow rates, model mix, router hit rate/latency/confidence, spend by model, and estimated saving.
- `MODEL_PICKER_VERBOSE=1` prints a one-line note per launch and per finished subagent. `MODEL_PICKER_LOG=0` turns the ledger off; `MODEL_PICKER_STATE_DIR` moves it.
- Ledger writes are flock-serialized with 0600 files in a 0700 directory; rotated files keep the newest 10. Resumed subagents and enforce-mode retries are counted once.
- Tests: 85 (from 47), including real-format transcripts, streamed-duplicate dedupe, cache-write pricing, rotation, attribution, and review regressions; tests never touch the real ledger.

## model-picker v0.2.0 (2026-11-16)

Decisions-API routing and a local-model tier.

- `MODEL_PICKER_ROUTER`: `openai-decisions` ([OpenAI](https://developers.openai.com/api/docs/guides/decisions), or any LiteLLM gateway serving `/v1/decisions`, which covers [TypeSafe Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev)), `google-decisions` ([Gemini](https://aistudio.google.com/docs/decisions-api)), or `command` (any executable). Each asks for a tier and an effort level; low confidence or any error falls back to the keyword heuristic and never blocks a launch. Recommendations now include effort and the source that decided.
- Privacy default: only the agent's short description is sent (`MODEL_PICKER_ROUTER_INPUT=description`); `brief`/`full` are opt-in. Extra headers and request metadata are configurable for gateway attribution and redaction.
- `MODEL_PICKER_LOCAL_HINT` adds a `local` tier for pure text work that needs no tools or files.
- Settings can live in `~/.config/model-picker/config.json` (environment wins); API keys only from the environment or `MODEL_PICKER_ROUTER_API_KEY_CMD`.
- `hooks/model_picker.py route "<task>"` CLI; the skill uses it for per-request recommendations.
- Agent hook timeout raised to 10 s to fit a router call (router timeout defaults to 3 s).

## model-picker v0.1.0 (2026-11-16)

New plugin: applies Anthropic's [Which Claude model should you build with?](https://claude.dev/blog/which-claude-model-should-you-build-with/) guidance (2026-11-09).

- SessionStart hook injects model/effort rules from `guide.md` (model by task, effort before model, fan-out limits, switch at natural breaks).
- PreToolUse hook on `Agent`/`Task` flags launches without a `model` and models two or more tiers off the task. Skips `fork` and agents whose definition pins `model`.
- PreToolUse hook on `Workflow` flags `agent()` calls without a `model`.
- `MODEL_PICKER_MODE`: `advise` (default), `enforce`, `off`. `MODEL_PICKER_INJECT=0` disables injection.
- `/model-picker:model-picker <task>` gives a per-request recommendation; with no argument it loads the rules (Codex/Grok/Cursor fallback).
- `model-picker:scout`: Haiku read-only agent for search and log reading.
- Agents whose definition pins `model` are found through `installed_plugins.json`, org-synced plugins, and `$CLAUDE_PROJECT_DIR/.claude/agents`, matched by frontmatter `name`.
- Regression tests in `plugins/model-picker/tests/`.

## copy v2.4.1 (2026-11-15)

Fix: `/copy:slack` writes Slack's own composer markup on the mixed/prose path, aligned with Slack's help pages ([Format your messages](https://slack.com/help/articles/202288908-Format-your-messages-in-Slack), [Format your messages with markup](https://slack.com/help/articles/360039953113-Format-your-messages-in-Slack-with-markup)).

- Previously the rules covered only bold, links and tables, so the model improvised the rest (for example `•` bullets).
- Explicit rules for bold `*text*`, italic `_text_`, strikethrough `~text~`, inline code, code blocks and `>` quotes, plus "never" forms (`**bold**`, `~~strike~~`, `#` headings, `<url|text>`).
- Bulleted lists use `- ` (never `•` or `* `), numbered lists use `1. `, and lists stay flat. Headings become a `*bold*` line.
- @mentions don't resolve on paste. The confirmation now lists them and tells the user to retype each one and pick it from autocomplete.
- Added a sample paste, and kept the note that markup renders only with "Format messages with markup" enabled (now with its location in Preferences).
- Native manifests (Codex/Grok/Cursor) and the generated `copy-slack` skill regenerated via `scripts/sync-native-manifests.py`.

## natbag v1.2.3 (2026-08-21)

Bugfix: SSL fallback for Python installs without a CA bundle.

- python.org macOS Python builds ship with an empty certificate store until `Install Certificates.command` is run. When such a build owns `python3` on PATH, every data.gov.il request failed with `CERTIFICATE_VERIFY_FAILED`, and the daily snapshot hook swallowed the error — history silently stopped accumulating.
- New shared `scripts/ssl_fallback.py` used by `snapshot.py` and `query_flights.py`: when the interpreter's default trust store is usable (including capath-based stores that report zero cached CAs), requests keep stdlib default behavior untouched; otherwise the first working CA bundle is used — `certifi` if importable, then OS bundles for macOS/BSD, Debian/Ubuntu, RHEL/Fedora, and openSUSE. Missing or corrupt bundles fall through to the next candidate, the resolved context is cached per process, and if nothing is usable a one-line actionable hint goes to stderr. No new hard dependencies.
- The SessionStart snapshot hook now logs to `~/.natbag/snapshot.log` (size-capped) instead of `/dev/null`, so the next failure class leaves a trace instead of silently stopping history accumulation.
- Native manifests (Codex/Grok/Cursor) regenerated via `scripts/sync-native-manifests.py`.

## Native multi-CLI marketplace support (2026-08-14)

The marketplace now ships first-class manifests for Claude Code, Codex, Grok, and Cursor:

- Claude Code: `.claude-plugin/marketplace.json` and per-plugin `.claude-plugin/plugin.json`.
- Codex: `.agents/plugins/marketplace.json` and per-plugin `.codex-plugin/plugin.json`.
- Grok: `.grok-plugin/marketplace.json` and per-plugin `.grok-plugin/plugin.json`.
- Cursor: `.cursor-plugin/marketplace.json` and per-plugin `.cursor-plugin/plugin.json`.

`scripts/sync-native-manifests.py` discovers new Claude marketplace entries automatically, infers presentation metadata, generates the three additional catalogs, wraps command-only plugins as Codex skills, and offers `--check` for CI-style drift detection. `copy` v2.4.0 adds generated Agent Skill forms of its eight commands, `coacher` v0.3.0 adds a portable skill plus Cursor always-on rule, `natbag` v1.2.2 refreshes its daily snapshot from the invocation workflow when hooks are unavailable, and `find-session` v1.2.1 fixes strict YAML parsing for its argument hint.

## Marketplace rename (2026-08-14)

The repository and root marketplace were renamed from `omri-cc-stuff` to `omri-marketplace` to reflect support for multiple AI coding agents. Use `omriariav/omri-marketplace` for new installs; existing GitHub links redirect to the new repository. Individual plugin names and versions were unchanged by the rename.

For Claude Code and Codex, this is an identity migration: a plugin installed as `x@omri-cc-stuff` is distinct from `x@omri-marketplace`. Existing users should migrate in this order:

1. Register `omriariav/omri-marketplace` with the relevant plugin manager.
2. Reinstall each existing plugin under its new `plugin@omri-marketplace` ID.
3. Verify the new installation and preserve the previous enabled/disabled state.
4. Remove the corresponding `plugin@omri-cc-stuff` registrations, keeping plugin data where the manager supports it.
5. Remove the obsolete `omri-cc-stuff` marketplace source.

See the README for Claude Code, Codex, and Grok registration commands.

## find-session v1.2.0 (2026-06-24)

New feature: `/find-session` now searches **Codex** transcripts alongside Claude Code sessions.

- Codex sessions (`~/.codex/sessions/<Y>/<M>/<D>/rollout-<ts>-<uuid>.jsonl`) are scanned in the same pass as Claude Code sessions. Results from both sources are interleaved by recency, each tagged with its source and the correct resume command (`codex resume <uuid>` vs `claude --resume <uuid>`).
- New flags: `--source claude|codex|both` (default `both`, with shorthand `--claude` / `--codex` / `--both`) to scope by source, and `--include-subagents` to include Codex subagent sessions (guardian auto-review judges and spawned children) that are otherwise excluded as non-resumable noise.
- Codex previews show the first genuine user message — injected `role: user` boilerplate (`# AGENTS.md`, `<environment_context>`, `<skill>`, `<turn_aborted>`, `<<ccr:`) is filtered from both the preview and keyword matching. Codex has no `/rename`, so Codex results are always preview-based.
- Current-project scope resolves Codex sessions by the `cwd` recorded in each transcript's `session_meta`, skipping non-matching files after a single line read (the default path stays ~1s). Cross-project `--all` keyword search uses a `ripgrep`/`grep` pre-filter over Codex transcripts (graceful fallback to a full scan when neither tool is present).
- `--json` output gains a `source` field and a per-result `resume_cmd`.
- Claude Code session parsing is unchanged (verified against the prior release).

## natbag v1.2.1 (2026-06-03)

New plugin — added to the marketplace by migrating `omriariav/natbag-skill` in via `git subtree` (full commit history preserved).

- Ben Gurion Airport (TLV) live flight data, destination weather, and historical delay analysis. `/natbag` shows departures, arrivals, specific flights, status, destination weather, and accumulated on-time/delay history.
- New entry in the root `marketplace.json` (`source: ./plugins/natbag`). The marketplace's first plugin with hooks (SessionStart snapshot + PreToolUse freshness guard); both use `${CLAUDE_PLUGIN_ROOT}` and resolve correctly under the monorepo layout.
- Runtime history accrues in `~/.natbag/flights.db` (install-independent), so existing data carries over untouched.
- Dropped the imported repo's redundant nested `.claude-plugin/marketplace.json` (the monorepo reads only the root manifest).
- The standalone `omriariav/natbag-skill` repo is now deprecated and points here. If you had it installed, uninstall it to avoid double-firing the SessionStart hook.

## coacher v0.2.0 (2026-06-03)

Moved into the marketplace as a local plugin — was previously listed in `marketplace.json` as an external `url` source pointing at `omriariav/claude-coacher`; now vendored locally via `git subtree` (history preserved).

- `marketplace.json` entry flipped from the git URL to `source: ./plugins/coacher`. No behavior change: still injects the SessionStart collaborator frame and provides `/coacher:status`, `/coacher:audit`, `/coacher:reset`, `/coacher:rant`.
- Dropped the imported repo's redundant nested `.claude-plugin/marketplace.json`.
- The standalone `omriariav/claude-coacher` repo is now deprecated and points here. If you had it installed from there, reinstall as `coacher@omri-marketplace` to avoid double-firing the SessionStart hook.

> Marketplace housekeeping: the root `plugin.json` no longer carries a `version` field — versioning is strictly per-plugin (Claude Code keys installs on each plugin's own version, namespaced by marketplace; the root version drove nothing).

## claude-reviewer v1.2.0 (2026-05-28)

New feature: fleet-level skill-cleaner signals on every config audit.

- Companion to `skill-reviewer v1.1.0`. Where `/skill-reviewer` surfaces skill-cleaner signals **per-skill**, `/claude-reviewer` now surfaces them **fleet-wide** across the audited scope. Every `/claude-reviewer [project]` or `/claude-reviewer --global` invocation now includes a `## Skill Fleet Audit` section in its report with:
  - **Fleet budget** — total always-loaded description tokens across all skills in scope vs cap (default `2%` of `200,000`-tok context = `4,000` tok). Pressure: `OK` / `WARN` / `OVER`.
  - **Description trim candidates** — every skill with description ≥ heavy threshold (default 120 tok), sorted by token cost.
  - **Duplicates** — every skill name that appears in multiple roots, with keep-priority `plugin > personal > repo`.
  - **Unused candidates** — opt-in via `--with-logs`. Conservative path-only scan of `~/.claude/projects/*.jsonl` for any skill in scope. Candidate signal only.
- New script: `scripts/fleet_audit.py`. Flags: `--global`, `--root` (repeatable), `--with-logs`, `--months`, `--context-tokens`, `--budget-percent`, `--heavy-threshold`, `--json`.
- Read-only — the script informs; per-skill trims flow through `/skill-reviewer --fix <skill>`.
- `argument-hint` exposes `--with-logs`; `SKILL.md` adds Phase 4 Step 1 documenting the integration; `references/report-template.md` adds the `## Skill Fleet Audit` section between Inventory and Scores (in both project-mode and global-mode templates).
- Same fixes applied as `cleaner_checks.py` from `skill-reviewer v1.1.0`: tolerant YAML block-scalar parsing, `os.walk` directory pruning, realpath-deduped scanning.

## skill-reviewer v1.1.0 (2026-05-28)

New feature: every per-skill review now surfaces skill-cleaner signals.

- Adapts the [skill-cleaner methodology by @steipete](https://github.com/steipete/agent-scripts/blob/main/skills/skill-cleaner/SKILL.md) for Claude Code as **per-skill checks**, not a separate audit mode. Every `/skill-reviewer <skill>` invocation now includes a "Skill Cleanliness Signals" section in its report with:
  - **Description budget** — token cost for the target skill (`ceil(utf8_bytes/4)`) vs cap (default `2%` of a `200,000`-token context). Flags `CANDIDATE for trim` above the heavy threshold (default 120 tok).
  - **Duplicates** — scans all skill roots for other copies of the same skill name; suggests which to keep via `plugin > personal > repo` priority.
  - **Usage evidence** — opt-in via `--with-logs`. Conservative path-only scan of `~/.claude/projects/*.jsonl` for transcript paths like `skills/<name>/SKILL.md`. Candidate signal only.
- New script: `scripts/cleaner_checks.py`. Flags: `--root` (repeatable), `--with-logs`, `--months`, `--context-tokens`, `--budget-percent`, `--heavy-threshold`, `--json`.
- Read-only — the script informs; fixes still flow through `/skill-reviewer --fix <skill>`.
- `argument-hint` exposes `--with-logs`; `SKILL.md` adds the integration step as Phase 5, Step 1; `references/report-template.md` adds the section between Inventory and Scores.
- **Fleet-level audits** (totals across all skills, all-duplicates-at-once, all-unused) will land separately as an addition to `/claude-reviewer`, whose scope already covers the whole `.claude/` setup.

## x v2.1.2 (2026-05-27)

Maintenance — no behavior change.

- `/x:read` and `/x:tweet` command files collapsed into **thin pointers** to their respective `skills/<name>/SKILL.md`. The skills are now the single source of truth for both commands; the previously duplicated workflow prose (which had to be hand-synced on every change, e.g. the v2.1.1 URL-counting fix) is gone. Commands instruct the model to resolve `scripts/...` paths relative to the skill directory.

## gdoc-math v1.0.2 (2026-05-27)

Maintenance — no behavior change.

- Trimmed the `SKILL.md` frontmatter `description` (tightened the opening line and compressed the NOT-for clauses) to cut always-loaded prompt-budget cost. All trigger phrases preserved.

## skill-reviewer v1.0.1 (2026-05-27)

Maintenance.

- Trimmed the `SKILL.md` frontmatter `description` (dropped the inline dimension list — documentation, not a trigger) to reduce always-loaded prompt-budget cost.
- Fixed broken YAML in the `SKILL.md` frontmatter: `argument-hint` was unquoted and began with `[`, which a strict YAML parser reads as a flow sequence and rejects. Now single-quoted (matching the sibling reviewer skills). Surfaced by Codex review of this PR.

## claude-reviewer v1.1.1 (2026-05-27)

Maintenance — no behavior change.

- Trimmed the `SKILL.md` frontmatter `description` (dropped the rubric-stats clause) to reduce always-loaded prompt-budget cost. Trigger phrases unchanged.

## copy v2.3.1 (2026-05-27)

Maintenance — no behavior change.

- Removed an orphaned legacy unified-router command (`.claude/commands/copy.md`) that sat in a non-loading location and duplicated the eight split `/copy:*` commands. No effect on the active command surface.

## gdoc-math v1.0.1 (2026-05-25)

Docs/example only — no pipeline changes.

- **Bundled example swapped** to a general math showcase: `examples/math-demo.md` (quadratic formula, Euler's identity, finite + geometric series, the derivative limit, the Gaussian integral, Pythagoras). Replaces the ML-specific `laplace-smoothing.md` sample so the demo reads as broadly recognizable math.
- `SKILL.md` and `README.md` updated to point at the new sample.

## gdoc-math v1.0.0 (2026-05-25)

New plugin. `/gdoc-math` converts a Markdown file containing LaTeX math into a **native Google Doc whose equations are real, editable equation objects** — solving the gap that `/copy:gdocs` (Paste from Markdown) and Google's own "Export to Docs" can't: both leave `$...$` / `$$...$$` as literal text.

### How it works
```
markdown+LaTeX  →[pandoc]→  .docx (OMML)  →[gws drive upload + convert --to docs]→  Google Doc
                                                                └─ intermediate .docx auto-trashed
```
pandoc renders LaTeX into Office MathML; Google Drive imports OMML as native, editable equations. Verified by round-tripping the converted Doc back to `.docx` — equations return as `m:oMath` objects with zero embedded images.

### Features
- Inline (`$…$`), display (`$$…$$`), and `\(…\)` / `\[…\]` math.
- Input modes: a `.md` file path, inline content, or the last math-bearing content in the conversation.
- `--name` (Doc title), `--folder` (target Drive folder), `--keep-docx` (debug).
- Optional `default_folder_id` in `config.json`; empty by default (My Drive root), never required.
- Ships a runnable sample at `examples/laplace-smoothing.md`.

### Requirements (verified by `verify-setup.sh` at Step 0)
- `pandoc` — `brew install pandoc`
- `python3` — parses `gws` output (default on macOS)
- `gws` ([Google Workspace CLI](https://github.com/omriariav/workspace-cli)), authenticated for Drive — `go install github.com/omriariav/workspace-cli/cmd/gws@latest`, then `gws auth login`

### Notes
- Simple fractions/subscripts/Greek round-trip cleanly; complex LaTeX (stacked fractions, matrices, large operators) can lose fidelity on Google's import and may need a touch-up.
- Kept standalone (not folded into `copy`) on purpose: it writes to Google Drive over the network with external dependencies — a different contract from `copy`'s instant, offline, clipboard-only commands.

## copy v2.3.0 (2026-05-04)

`/copy:slack` now produces a **native Slack table** on paste when the resolved content is just a table.

### New
- **Native Slack table paste (table-only path)** — When the content is a bare table, `/copy:slack` puts a Google-Sheets-flavored payload on the macOS pasteboard: `public.utf8-plain-text` carries TSV, and `public.html` carries a `<table>` wrapped with `<google-sheets-html-origin>` and `data-sheets-root="1"`. Slack's composer recognizes that exact shape and converts the paste into a real native table block — same render as a bot posting via the Block Kit `table` API.
- New `## Native Table Paste` section in `commands/slack.md` with the full TSV + HTML + JXA `NSPasteboard` recipe (plain `pbcopy` is not enough for this path).

### Changed
- **Router** (`/copy slack`): step 4 of the workflow now branches Slack into table-only (JXA path) vs mixed/prose (`pbcopy`); the destination rules and the example explicitly call out the same branch so a model following either prompt can't fall back to plain text and silently break native rendering.
- Mixed/prose Slack guidance (bold, links, QuickChart) is now scoped to the mixed path. Adding any prose or chart URL to the table-only path would convert the paste into mixed content and Slack would drop the native render — the docs spell this out.

### Why "table-only"?
Slack's composer recognizes the Sheets-flavored clipboard only when the paste is a table. Add prose around the table and Slack falls back to plain text. For prose + native table together, the realistic options are:
- Two pastes (prose first, table-only second), or
- Use a Slack MCP / webhook that posts via `chat.postMessage` with a Block Kit `table` block.

End users still cannot construct a native table block in the composer manually — that's a Slack platform constraint, not a plugin gap.

## x v2.0.0 (2026-04-29)

**Breaking change:** plugin renamed `tweet` → `x`. Command `/tweet` is now `/x:tweet`. Update any scripts or muscle memory.

### New
- **`/x:read`** — Fetch a tweet by URL or ID via X API v2 (`GET /2/tweets/:id`) and load it into the conversation as markdown (author, timestamp, public metrics, replied-to / quoted tweets, media). Reuses `/x:tweet` Keychain credentials. Metered by X at ~$0.005 / post read.
- Plugin description updated to cover both posting and reading.

### Changed
- Plugin directory `plugins/tweet/` → `plugins/x/`.
- `plugins/x/.claude-plugin/plugin.json` `name` field is now `x`.
- `marketplace.json` plugin entry renamed and now lists both `./skills/tweet` and `./skills/read`.

### Migration

**If you had the old `tweet` plugin installed**, Claude Code's plugin cache still points at `plugins/tweet/` and `/plugin update` will fail with `Plugin source not found at .../plugins/tweet`. The directory and entry name both moved, so this is a fresh install, not an upgrade.

To migrate:
1. `/plugin` → select `tweet` → **Uninstall**
2. `/plugin` → install `x` from this marketplace
3. Restart Claude Code so the new commands register

Keychain credentials live under service `x-api` and are untouched — `/x:tweet` will pick them up immediately. Just update muscle memory: `/tweet` → `/x:tweet`, and you get `/x:read` for free.

## tweet v1.3.0 (2026-03-30)

Voice learning and skill structure improvements.

### New
- **Voice learning**: Reads `LEARNINGS.json` before drafting to match your established voice, appends observations after each posted tweet
- `config.json` for operational defaults (char target, thread style)
- `references/` directory with extracted char-counting rules and drafting guidelines

### Changed
- Scoped `allowed-tools` to `Bash(python3*), Bash(bash scripts/*)` for security
- Expanded description with negative trigger cases
- Trimmed SKILL.md from 134 to 92 lines via progressive disclosure
- Removed duplicate credential setup and verbose script output sections

## copy v2.2.2 (2026-04-29)

Bugfix: `/copy:slack` now emits Markdown `[text](url)` links instead of Slack's `<url|text>` Block Kit syntax.

### Fixed
- **Slack**: Links use Markdown `[text](url)` — renders as a hyperlink in the composer when "Format messages with markup" is enabled in Slack preferences. The previous `<url|text>` form is API/`mrkdwn` syntax and renders literally in the composer. Raw URLs remain a safe fallback (Slack auto-unfurls them). (#6)
- Router (`/copy slack`) updated to match.

## copy v2.2.1 (2026-04-05)

Bugfix: resolve format inconsistencies between router and subcommands.

### Fixed
- **Jira**: Router had stale wiki markup (`||header||`, `*bold*`); now matches `jira.md` Markdown syntax
- **Gmail**: `gmail.md` incorrectly used `pbcopy`; now uses `textutil` → RTF → `osascript` for rich text paste
- **Router**: Added `richformat` and `cleartext` to argument list, examples, and clipboard rules
- **Router**: Renamed `/copy email` → `/copy gmail` for consistency with actual command name

## copy v2.2.0 (2026-03-30)

### New
- `/copy:md` - Copy as standard Markdown (tables, headers, bold, links, code blocks)

---

## v2.0.0 (2025-12-29)

**Breaking Change:** Restructured `/copy` command into separate format-specific commands.

### New Commands
- `/copy:slack` - Format for Slack
- `/copy:gchat` - Format for Google Chat
- `/copy:gmail` - Format for Gmail/email (HTML)
- `/copy:gdocs` - Format for Google Docs (Markdown)
- `/copy:jira` - Format for Jira Cloud (Markdown)
- `/copy:cleartext` - Plain text
- `/copy:richformat` - Rich Text Format (RTF)

### Changes
- Plugin renamed from `copy-command` to `copy` for cleaner namespacing
- Each format is now a separate command (`/copy:slack` instead of `/copy slack`)
- Jira format updated to use Markdown (Jira Cloud) instead of wiki markup
- All commands accept optional `[what to copy]` argument

### Migration
Old: `/copy slack the table`
New: `/copy:slack the table`

---

## v1.x (2025-12-29)

Initial releases with single `/copy` command and various structural experiments.

- v1.3.0 - Skills-based structure attempt
- v1.2.0 - GitHub sync fixes
- v1.0.0 - Initial plugin release
