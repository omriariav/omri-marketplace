---
description: Post a tweet, thread, or reply to X (Twitter) — drafts, previews, and posts after approval.
argument-hint: '<text or topic to tweet>'
allowed-tools: Bash(python3:*), Bash(bash:*), Read, AskUserQuestion
---

Execute the **tweet** skill: read `${CLAUDE_PLUGIN_ROOT}/skills/tweet/SKILL.md` and follow its workflow. That file — together with `references/drafting-guidelines.md`, `references/char-counting.md`, `config.json`, and `LEARNINGS.local.json` (seeded from the `LEARNINGS.json` template on first run) in the same directory — is the single source of truth: setup check, drafting + voice, preview/approval gates, thread splitting, posting, and post-publish voice learning all live there.

Treat `$ARGUMENTS` as the tweet text or topic. Resolve every script and reference path in the SKILL.md relative to `${CLAUDE_PLUGIN_ROOT}/skills/tweet/` (e.g. `scripts/post.py` → `${CLAUDE_PLUGIN_ROOT}/skills/tweet/scripts/post.py`).
