# Omri's AI Agent Marketplace

Personal marketplace of plugins, skills, commands, and utilities for Claude Code, Codex, Grok, and Cursor.

## Installation

Each CLI gets its own native manifest rather than relying on another vendor's compatibility layer:

| Runtime | Marketplace manifest | Per-plugin manifest |
|---------|----------------------|---------------------|
| Claude Code | `.claude-plugin/marketplace.json` | `.claude-plugin/plugin.json` |
| Codex | `.agents/plugins/marketplace.json` | `.codex-plugin/plugin.json` |
| Grok | `.grok-plugin/marketplace.json` | `.grok-plugin/plugin.json` |
| Cursor / `cursor-agent` | `.cursor-plugin/marketplace.json` | `.cursor-plugin/plugin.json` |

The examples below install or discover the `x` plugin; replace `x` with another plugin name from the marketplace.

### Claude Code

From the plugin UI:

```
/plugins → Marketplace → Add → omriariav/omri-marketplace
```

Or from a terminal:

```bash
claude plugin marketplace add omriariav/omri-marketplace
claude plugin install x@omri-marketplace
```

### Codex

```bash
codex plugin marketplace add omriariav/omri-marketplace
codex plugin list --marketplace omri-marketplace --available --json
codex plugin add x@omri-marketplace
```

### Grok

```bash
grok plugin marketplace add omriariav/omri-marketplace
grok plugin list --available --json
```

Then open `/marketplace` or `/plugins` in Grok to install the desired plugin.

### Cursor / `cursor-agent`

Cursor manages installation through **Customize** in the IDE, the interactive `/plugin` browser, or the team marketplace dashboard. The CLI can register and list marketplaces, but it does not yet expose a non-interactive `plugin install` command.

```bash
cursor-agent plugin marketplace add https://github.com/omriariav/omri-marketplace
cursor-agent plugin marketplace list --format json
```

For a team marketplace, import `https://github.com/omriariav/omri-marketplace` under **Dashboard → Plugins**. For local development, clone this repository and load the desired plugin directly:

```bash
cursor-agent --plugin-dir "$PWD/plugins/x"
```

## Runtime behavior

All marketplace entries have native manifests on all four runtimes. Component behavior differs only where the hosts expose different primitives:

| Plugin type | Claude Code | Codex | Grok | Cursor |
|-------------|-------------|-------|------|--------|
| Skill-based plugins | Native skills | Native skills | Native skills | Native skills |
| `copy` commands | Native commands + skills | Native skills | Native commands + skills | Native commands + skills |
| `coacher` frame | Session-start hook + skill | Manual skill | Session-start hook + skill | Always-on rule + skill |
| `natbag` snapshot | Hook + invocation fallback | Invocation fallback | Hook + invocation fallback | Invocation fallback |
| `model-picker` rules + launch checks | Session-start + PreToolUse hooks + skill | Manual skill | Manual skill | Manual skill |

`setup-pulse` and `claude-reviewer` can be invoked from every runtime, but their purpose remains configuring or reviewing Claude Code.

### Maintaining manifests

Claude marketplace metadata is the source of truth. Adding a plugin does not require hand-maintaining four catalogs:

1. Create `plugins/<name>/.claude-plugin/plugin.json` with a semantic version and add the plugin's components.
2. Add one entry to `.claude-plugin/marketplace.json`. `name` and local `source` are required; description, author, and keywords fall back to the plugin manifest when omitted.
3. Ensure the plugin has a Codex-compatible component: a skill, `.mcp.json`, or `.app.json`. For command-only plugins, the generator creates Codex skill wrappers automatically. Hook/rule/agent-only plugins need a hand-authored fallback skill because Codex does not ingest those Claude/Grok/Cursor primitives.
4. Regenerate and verify:

```bash
python3 scripts/sync-native-manifests.py
python3 scripts/sync-native-manifests.py --check
```

Display names, short descriptions, categories, and starter prompts are inferred for new plugins. Optional curated overrides live near the top of the generator; they are not required for onboarding.

Marketplace plugins are intentionally vendored in this repository: every source must be the local path `./plugins/<name>`. External or object-form Claude marketplace sources are not supported because Codex, Grok, and Cursor catalogs are generated from the vendored plugin files.

Pull requests run the same `--check` command, so a new or updated plugin cannot merge with stale native catalogs.

For a plugin with Claude/Grok hooks, the generator creates an empty Cursor hook override to prevent incompatible hook discovery. Replace it with a non-empty Cursor-native `.cursor-plugin/hooks.json` when needed; regeneration preserves that hand-authored translation.

### CI and releases

Every pull request to `main` runs the required `CI` check. It validates tracked JSON, Python, and shell files; exercises future-plugin generation and release fixtures; verifies generated marketplace catalogs; confirms that every native manifest has the same plugin name and semantic version; and runs real Codex, Grok, and Cursor CLI plugin smoke tests against pinned versions. A daily scheduled matrix repeats those smoke tests against each vendor's latest stable CLI so upstream compatibility breaks are detected without making pull requests depend on a moving target. Files ending in `.json` must contain strict JSON; use `.jsonc` for comment-bearing configuration where the consuming tool supports it.

Plugin releases are created from the source manifest, so adding a plugin does not require editing the release workflow:

1. Update `plugins/<name>/.claude-plugin/plugin.json` with the next semantic version.
2. Run `python3 scripts/sync-native-manifests.py` and commit all generated native manifests in the same pull request.
3. Merge the pull request after `CI` passes.
4. Open **Actions → Release plugin → Run workflow**, select `main`, and enter `<name>`.

The workflow re-runs validation, rejects duplicate releases, creates the canonical `<name>-v<version>` tag from `main`, and publishes a GitHub release with generated notes. New marketplace entries are discovered automatically by both CI and the release helper.

> **Migrating from `omri-cc-stuff`?** GitHub redirects the old repository URL, but Claude Code and Codex plugin IDs include the marketplace name and do not migrate automatically. Register `omri-marketplace`, reinstall the same plugins under their new `plugin@omri-marketplace` IDs, verify them, and only then remove the old marketplace. Plugin names are unchanged; versions continue independently as features are added.

## Commands

### `/copy:*` - Format & Copy to Clipboard

Copy data/tables/text to clipboard, formatted for your destination.

| Command | Description |
|---------|-------------|
| `/copy:slack` | Format for Slack (ASCII tables, `*bold*`) |
| `/copy:gchat` | Format for Google Chat (same as Slack) |
| `/copy:gmail` | Format for Gmail/email (rich text paste) |
| `/copy:gdocs` | Format for Google Docs (Markdown, paste via Edit → Paste from Markdown) |
| `/copy:jira` | Format for Jira Cloud (Markdown) |
| `/copy:md` | Markdown (standard syntax) |
| `/copy:cleartext` | Plain text (universal) |
| `/copy:richformat` | Rich Text Format (RTF) for Word, Pages |

**Usage:**
```
/copy:slack                    → Auto-detect what to copy, format for Slack
/copy:slack the summary table  → Copy specific content for Slack
/copy:gmail                    → Auto-detect, format as rich text for email
```

**Features:**
- Auto-detects what to copy from conversation context
- Formats text, tables, lists, code, summaries
- Numbers auto-formatted (e.g., `$2.45M` not `2450000`)
- QuickChart URLs for numeric data visualization

**Example:**
```
You: Compare the top programming languages

Claude: | Language   | Stars  | Growth |
        |------------|--------|--------|
        | Python     | 142K   | +18%   |
        | JavaScript | 128K   | +12%   |
        | Rust       | 89K    | +31%   |

You: /copy:slack

Claude: ✓ Copied to clipboard (format: Slack)
```

### `/gdoc-math` - Markdown + LaTeX → Google Doc with editable equations

Convert a Markdown file containing LaTeX math into a **native Google Doc whose equations are real, editable equation objects** — not images, not literal `$$` text. This is the gap `/copy:gdocs` can't fill: Google Docs' "Paste from Markdown" silently drops `$...$` math, and there's no Docs API to insert equations. The pipeline routes through `.docx` instead:

```
markdown+LaTeX  →[pandoc]→  .docx (OMML)  →[gws upload + convert]→  Google Doc
```

```
/gdoc-math ~/notes/laplace.md                 → convert a file
/gdoc-math ~/notes/laplace.md --name "Notes"  → set the Doc title
/gdoc-math                                    → use the last math content in the conversation
```

**Requirements** (the skill verifies all up front):
- [`pandoc`](https://pandoc.org) — `brew install pandoc`
- `python3` — parses `gws` output (default on macOS)
- [`gws`](https://github.com/omriariav/workspace-cli) (Google Workspace CLI), authenticated for Drive — `go install github.com/omriariav/workspace-cli/cmd/gws@latest` then `gws auth login`

**Features:**
- Inline (`$…$`) and display (`$$…$$`) math, plus `\(…\)` / `\[…\]` delimiters
- Equations land as native, clickable, editable Google Docs equation objects (verified via OMML round-trip)
- Auto-trashes the intermediate `.docx` — only the Google Doc remains
- Optional `default_folder_id` in `config.json` to drop Docs in a specific Drive folder
- Ships a runnable sample at `examples/math-demo.md`

> Use `/copy:gdocs` for plain prose/tables with no math (clipboard, instant). Use `/gdoc-math` when the content has formulas that must stay editable.

### `/x` - Post and Read on X (Twitter)

Two skills under one plugin, sharing the same X API credentials.

> **⚠️ Migrating from the old `tweet` plugin?** This release renames the plugin (`tweet` → `x`) and moves its directory (`plugins/tweet/` → `plugins/x/`). Claude Code's plugin cache still points at the old path, so `/plugin update` will fail with `Plugin source not found at .../plugins/tweet`. Uninstall the old `tweet` plugin via `/plugin`, then install `x` fresh. Your Keychain credentials (service `x-api`) are untouched and will be picked up automatically. See `RELEASES.md` for the full migration path.

#### `/x:tweet` - Post

Post tweets directly from Claude Code. Drafts the text, shows a preview with character count, and asks for approval before posting. Learns your voice over time from editing patterns and feedback.

```
/x:tweet Testing my new tweet skill from Claude Code
/x:tweet Announce the launch of our new attribution feature
```

**Workflow:**
1. Draft tweet text (or use your text as-is)
2. Preview with character count (max 280)
3. Approve / Edit / Cancel
4. Posts via X API v2, returns tweet URL

#### `/x:read` - Read

Fetch a tweet by URL or ID via the X API v2 and load it into the conversation as markdown — with author, timestamp, metrics, replied-to / quoted context, and media. Useful for "summarize this thread", "draft a reply to this", or quoting a tweet in another piece of writing.

```
/x:read https://x.com/jack/status/20
/x:read 20
```

Hand the resulting tweet ID to `/x:tweet --reply-to <id>` to chain a reply. Note: each read is metered by X (~$0.005 / post read on the standard tier).

**Setup (one-time):**
```bash
pip3 install requests-oauthlib
```
On first run, `/x:tweet` will pop up native macOS dialogs to store your X API credentials securely in Keychain (`/x:read` reuses the same credentials). Get keys from [developer.x.com](https://developer.x.com) → your app → Keys and Tokens (Consumer Key/Secret + Access Token/Secret with Read+Write permissions).

Alternatively, set env vars in `~/.zshrc`: `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`.

### `/skill-reviewer` - Skill Design Evaluation

Evaluate any skill's design quality against [best practices](https://x.com/trq212/status/2033949937936085378). Scores 10 dimensions (29-point rubric), detects anti-patterns, and optionally applies safe fixes.

```
/skill-reviewer publisher-lookup          → Review by skill name
/skill-reviewer --fix my-skill            → Review + auto-apply safe fixes
/skill-reviewer --compare skill-a skill-b → Side-by-side comparison
/skill-reviewer --with-logs my-skill      → Review + include transcript usage evidence
```

**Dimensions scored:** Progressive Disclosure, Description Quality, Gotchas, Non-Obvious Content, Flexibility, Setup & Config, Memory, Scripts, Frontmatter, Hooks Integration.

**Grades:** A (25-29), B (20-24), C (15-19), D (10-14), F (0-9)

**Skill cleanliness signals (v1.1.0):** Every per-skill review now includes a "Skill Cleanliness Signals" section adapting the [skill-cleaner methodology by @steipete](https://github.com/steipete/agent-scripts/blob/main/skills/skill-cleaner/SKILL.md) — description budget cost, duplicates across roots (with keep-priority), and opt-in usage evidence via `--with-logs`. Read-only; informs the rubric, doesn't replace it.

### `/find-session` - Search Past Conversations

Search past **Claude Code and Codex** session history by keyword and get the right resume command (`claude --resume <id>` or `codex resume <id>`) to pick up where you left off. Results from both sources interleave by recency, each tagged with its source.

```
/find-session deploy                → Search current project (both sources) for "deploy"
/find-session --all taboola         → Search across all projects
/find-session --all --codex taboola → Codex sessions only
/find-session --all --json auth     → Structured JSON output
```

**Features:**
- Two sources in one search: Claude Code (`~/.claude/projects/`) and Codex (`~/.codex/sessions/`)
- `--source claude|codex|both` (default `both`), with shorthand `--claude` / `--codex` / `--both`
- Searches user messages and custom titles (not assistant text); Codex previews skip injected `role: user` boilerplate (`# AGENTS.md`, `<environment_context>`, etc.)
- Word-boundary matching for clean terms, literal match for punctuation (`claude-mem`, `c++`)
- Shows custom title (from `/rename`) when set, falls back to first message preview (Codex has no titles → always preview-based)
- Codex subagent sessions (guardian judges, spawned children) excluded by default; `--include-subagents` to keep them
- Optional Haiku summarization for untitled sessions (asks before running)
- `--json` mode for programmatic use (adds `source` + per-result `resume_cmd`)
- Git-aware project root resolution (works from subdirectories); Codex sessions scoped by recorded `cwd`

**Configuration** (`config.json`):
| Setting | Default | Description |
|---------|---------|-------------|
| `max_results` | 20 | Max sessions to show |
| `use_haiku_summary` | true | Enable Haiku summarization prompt |
| `haiku_threshold` | 50 | Max results for Haiku to trigger |
| `haiku_model` | `claude-haiku-4-5-20251001` | Model for summarization |
| `exclude_slugs` | `[]` | Project slugs to skip in `--all` mode |

### `/claude-reviewer` - Claude Code Configuration Auditor

Review any project's `.claude/` folder setup against [best practices](https://x.com/akshay_pachaar/status/2035341800739877091). Scores 8 dimensions (24-point rubric), detects anti-patterns, and produces an actionable improvement report.

```
/claude-reviewer                       → Review current project
/claude-reviewer ~/Code/myproject      → Review specific project
/claude-reviewer --global              → Review only ~/.claude/ global config
/claude-reviewer --with-logs           → Add transcript-based unused-skill detection
```

**Skill fleet audit (v1.2.0):** Every report now includes a `## Skill Fleet Audit` section adapting the [skill-cleaner methodology by @steipete](https://github.com/steipete/agent-scripts/blob/main/skills/skill-cleaner/SKILL.md) at fleet level — total budget, trim candidates, duplicates across roots, and opt-in unused detection. Companion to `skill-reviewer v1.1.0`'s per-skill signals.

**Dimensions scored:** CLAUDE.md Quality, Permission Hygiene, Modular Instructions, Custom Commands, Skills Setup, Agent Configuration, Git Hygiene, Progressive Disclosure.

**Grades:** A (21-24), B (16-20), C (11-15), D (6-10), F (0-5)

### `/natbag` - Ben Gurion Airport (TLV) Flight Data

Live flight data, destination weather, and historical delay analysis for Ben Gurion Airport (TLV), pulling from [data.gov.il](https://data.gov.il/datasets/airport_authority/flydata) (flights) and [Open-Meteo](https://open-meteo.com/) (weather). A SessionStart hook quietly snapshots flight data so the historical database accumulates over time.

```
/natbag departures            → Departures board (status, gate, terminal)
/natbag arrivals              → Arrivals board
/natbag LY001                 → Status of a specific flight
/natbag delayed               → Only delayed / canceled flights
/natbag weather Paris         → Current weather at a destination city
/natbag history El Al         → On-time / delay history for an airline
```

**Features:**
- Bilingual (Hebrew + English) input and output
- Rolling ~3-day live window; gates, terminals, check-in zones
- Local SQLite history accrues daily snapshots → on-time performance, delay/cancellation rates, per-flight change tracking
- Ships IATA reference data (999 airlines, 9,240 airports); user history lives in `~/.natbag/`
- Pure Python 3 stdlib + `sqlite3` + `curl` — no API keys, no pip installs

### `coacher` - Peer-Collaborator Frame at Session Start

Primes Claude with a peer-collaborator stance at SessionStart (based on Amanda Askell's prompting philosophy — positive framing, permission to push back, no apology spirals). A SessionStart hook injects the frame from `frame.md`; edit that file to tune the stance.

| Command | Description |
|---------|-------------|
| `/coacher:status` | Integrity check — verify the frame is in the current context |
| `/coacher:audit` | Cross-check `frame.md` against your CLAUDE.md and flag stance conflicts |
| `/coacher:reset` | Re-anchor the frame mid-session if Claude drifts into hedging |
| `/coacher:rant <text>` | Vent raw frustration → Claude extracts the intent and acts on it |

Requires `python3` (stdlib only). The hook degrades gracefully — if python3 is missing or the frame is empty, it exits cleanly with no failure.

### `model-picker` - Right Model for Every Agent Launch

Applies Anthropic's [Which Claude model should you build with?](https://claude.dev/blog/which-claude-model-should-you-build-with/) guidance. A SessionStart hook injects the model/effort rules from `guide.md`; a PreToolUse hook checks every `Agent` (subagents and teammates) and `Workflow` launch for an explicit, task-appropriate `model`.

| Entry point | Description |
|-------------|-------------|
| `/model-picker:model-picker <task>` | Per-request recommendation: model, effort, why, optional split |
| `/model-picker:model-picker` | Load the rules manually (Codex, Grok, Cursor) |
| `model-picker:scout` agent | Haiku read-only scout for search, logs, and test output |

Set `MODEL_PICKER_MODE` in `settings.json` `env`: `advise` (default, note only), `enforce` (deny launches without a model), `auto` (fill in the recommended model), or `off`. `MODEL_PICKER_INJECT=0` skips the session-start injection. Requires `python3`; hooks exit silently without it.

## Other Commands

| Command | Description |
|---------|-------------|
| `/setup-pulse` | Install [claude-pulse](https://github.com/omriariav/claude-pulse) statusline |

### `/setup-pulse` - Install Token Usage Statusline

Installs [claude-pulse](https://github.com/omriariav/claude-pulse) - shows real-time context usage in your statusline.

```
/setup-pulse
```

After running, you'll see: `72k/200k (36%)` with color-coded warnings (green/yellow/red).

## Contributing

Contributions welcome! Feel free to open a PR or issue.

## License

Free to use with attribution. Credit: [Omri Ariav](https://github.com/omriariav)
