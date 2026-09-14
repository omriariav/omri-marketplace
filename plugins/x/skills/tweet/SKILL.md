---
name: tweet
description: |
  Post tweets to X (Twitter) from Claude Code. Supports single tweets, threads (auto-split or manual), and replies.
  Use when user says "/x:tweet", "post a tweet", "tweet this", "post a thread", "reply to this tweet".
  NOT for reading tweets, analytics, scheduling, or DMs.
argument-hint: '<text or topic to tweet>'
allowed-tools: Bash(python3*), Bash(bash scripts/*), Read, AskUserQuestion
user-invocable: false
---

# Tweet - Post to X (Twitter)

Post tweets from Claude Code via the X API v2. Supports single tweets, threads (auto-split for long text), and replies.

## Workflow

0. **Setup check**: Run `bash scripts/verify-setup.sh`. If credentials are missing:
   - Run `bash scripts/setup.sh` — this pops up native macOS dialogs for each key (values never pass through the conversation)
   - Tell user to get keys from developer.x.com → app → "Keys and Tokens" if they don't have them yet
   - Re-run verify to confirm, then proceed to step 1
1. **Draft**: Take `$ARGUMENTS` as the tweet prompt. If it's a direct tweet (clear, short text), use as-is. If it's a description/topic, draft tweet text. No length restriction at this stage.
2. **Preview**: Always display the **full tweet text** in a quote block, followed by the character count. The user must see exactly what will be posted before the `AskUserQuestion` prompt. Then:
   - **If <= 280 chars**: Show the full text + `(N/280 chars)`, then `AskUserQuestion` with options:
     - "Post it" — send the tweet
     - "Edit" — user provides revised text, loop back to preview
     - "Cancel" — abort
   - **If > 280 chars**: Show the full text + `(N chars — over limit)`, then `AskUserQuestion` with options:
     - "Post as thread" — auto-split into numbered parts (see Step 2b)
     - "Shorten it" — rewrite to fit 280 chars, loop back to preview
     - "Cancel" — abort
2b. **Thread preview** (if user chose "Post as thread"): Show the **full text of every part** with per-part char counts:
   ```
   1/3: First part of the tweet text shown in full here... (278/280)

   2/3: Second part text shown in full here... (265/280)

   3/3: Final part shown in full. (42/280)
   ```
   Then `AskUserQuestion` with options:
   - "Post it" — post the thread using `--reply-to` chaining (see "Thread (pre-split)" in Step 3)
   - "Edit" — user revises, loop back to step 2
   - "Cancel" — abort
3. **Post**: Execute the appropriate command based on mode. **For any tweet text containing apostrophes, quotes, or newlines, write each part to a tempfile and post with `--from-file` — never build tweet bodies inline via `python3 -c` inside `$(...)`** (see Known Issues).
   - **Single tweet**: `python3 scripts/post.py --from-file /tmp/tweet.txt` (or `python3 scripts/post.py "short safe text"` for short ASCII bodies)
   - **Thread (pre-split)**: Write each part to its own tempfile, then post with `--reply-to` chaining. This preserves your exact tweet boundaries from the preview *and* preserves apostrophes:
     ```
     # Use the Write tool to put each part in /tmp/tweet_1.txt, /tmp/tweet_2.txt, ...
     python3 scripts/post.py --from-file /tmp/tweet_1.txt
     # capture tweet_id from output
     python3 scripts/post.py --reply-to TWEET_ID --from-file /tmp/tweet_2.txt
     # repeat for each part
     ```
   - **Thread (auto-split)**: `python3 scripts/post.py --thread --from-file /tmp/tweet_full.txt` — WARNING: this splits on word boundaries by character count, ignoring paragraph breaks. Only use for unstructured text where split points don't matter.
   - **Reply**: `python3 scripts/post.py --reply-to TWEET_ID --from-file /tmp/reply.txt`
   - **Delete (recovery)**: `python3 scripts/post.py --delete TWEET_ID` — for undoing a bad post. Only the authenticated account's own tweets can be deleted.
4. **Confirm**: Show the tweet URL(s) from the script output.

## Tweet Drafting Guidelines

Read `references/drafting-guidelines.md` for baseline rules, `config.json` for operational defaults (char target, thread style), and `LEARNINGS.local.json` for user-specific voice and preferences.

## Voice Learning

`LEARNINGS.json` in this directory is a **template only** (ships with the plugin, same for every installer — do not read it for voice, do not edit it with real preferences, and never copy its example entries into a real learnings file). The file that actually holds this user's learned voice is `LEARNINGS.local.json`, which is gitignored and per-installer.

Before drafting any tweet: if `LEARNINGS.local.json` does not exist yet in this directory, create it with an empty `entries` array by running:
```
python3 -c "import json, os; f='LEARNINGS.local.json'; os.path.exists(f) or json.dump({'entries': []}, open(f, 'w'), indent=2)"
```
Then read `LEARNINGS.local.json` to match the user's established voice and preferences. Read `config.json` for operational defaults.

After each tweet is posted (confirmed via script output), append a new entry to the `entries` array in `LEARNINGS.local.json` (never edit the tracked `LEARNINGS.json` template) for any patterns you noticed:
- Tone adjustments the user made during editing
- Phrasing preferences (words they added, removed, or rephrased)
- Structural preferences (thread vs. single tweet, use of lists, data, visuals)
- Any explicit feedback the user gave about style

Each entry has `date`, `category` (one of: `tone`, `phrasing`, `structure`, `feedback`), and `observation` fields. Do not duplicate existing observations.
If the `entries` array exceeds 30 items, consolidate related entries before appending new ones.

## Character Counting — Critical

Read `references/char-counting.md` for detailed rules. Key point: aim for **270 chars max** and use `python3 -c "print(len(...))"` for exact counts near the limit.

## Script Output

Scripts output JSON with `id`, `url`, `text` fields. Threads return an array with a `part` number per entry. Errors return `{"error": "..."}`.

## Important

- **NEVER post without explicit user approval via AskUserQuestion**
- **ALWAYS show the full tweet text** before asking the user to approve — never ask "Post it?" without displaying exactly what will be posted
- Always show character count in preview
- For threads, show the full text of every part with per-part character counts
- **For manually crafted threads: ALWAYS use --reply-to chaining**, not `--thread`. The `--thread` auto-splitter ignores paragraph structure and will break your carefully crafted tweets at arbitrary word boundaries.

## Known Issues

- `--thread` auto-splitter collapses all whitespace (including `\n\n`) and re-splits on word count, not paragraph boundaries. A previewed 7-tweet thread may become 6 tweets with mid-sentence breaks. Fix: use `--reply-to` chaining for pre-split threads. (2026-02-20)
- **Char count mismatch between preview and post.py**: Claude's estimated char count in preview often differs from `post.py`'s actual count by 5-30 chars, causing "Tweet too long" errors and retry loops. Root cause: URL length, special chars, newlines counted differently. Fix: use `python3 -c "print(len(...))"` for exact count, or aim for 270 chars. (2026-02-22)
- **Apostrophe / quote corruption from nested shell escapes**: building tweet text via `python3 -c '...'` inside `$(...)` and passing it as the positional arg to `post.py` collides the outer shell's `'\''` quoting with Python's `'''` triple-quoted strings, publishing literal `'''` in place of every apostrophe (e.g., `yesterday's` → `yesterday'''s`). **Fix: always use `--from-file PATH`** — write each tweet body to a tempfile with the Write tool, then `python3 scripts/post.py --from-file /tmp/tweet_N.txt`. Tempfiles preserve apostrophes, quotes, and newlines verbatim. (2026-04-29)
