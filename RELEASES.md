# Releases

## model-picker v0.1.0 (2026-11-16)

New plugin: applies Anthropic's [Which Claude model should you build with?](https://claude.dev/blog/which-claude-model-should-you-build-with/) guidance (2026-11-09).

- SessionStart hook injects model/effort rules from `guide.md` (model by task, effort before model, fan-out limits, switch at natural breaks).
- PreToolUse hook on `Agent`/`Task` flags launches without a `model` and models two or more tiers off the task. Skips `fork` and agents whose definition pins `model`.
- PreToolUse hook on `Workflow` flags `agent()` calls without a `model`.
- `MODEL_PICKER_MODE`: `advise` (default), `enforce`, `auto`, `off`. `MODEL_PICKER_INJECT=0` disables injection.
- `/model-picker:model-picker <task>` gives a per-request recommendation; with no argument it loads the rules (Codex/Grok/Cursor fallback).
- `model-picker:scout`: Haiku read-only agent for search and log reading.

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
