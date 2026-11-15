---
description: Copy to clipboard for Slack. Specify what to copy, or auto-detects from context.
argument-hint: [what to copy]
---

# /copy:slack - Format for Slack

Copy data/tables/text to clipboard, formatted for Slack.

**$ARGUMENTS** = `[what to copy]` (optional - if not specified, finds most recent output)

## Workflow

1. **Find content**: If specified in arguments, find that content. Otherwise, find most recent table/data/output.
2. **Decide branch by content shape**:
   - **Table-only** (the resolved content is just a table, no surrounding prose) → use the **Native Table Paste** path below. Pasting will render as a real Slack table.
   - **Mixed** (table + prose, or no table at all) → use the **ASCII / Markdown** path. Slack drops native table rendering when the paste contains anything besides a single table, so don't pretend.
3. **Copy to clipboard** per the chosen branch.
4. **Confirm**: Show preview and tell the user which branch was used (so they know what to expect on paste).

## Native Table Paste (table-only content)

Slack's composer converts a paste into a native table block **only** when the clipboard looks like a Google Sheets copy — TSV plain text plus a `<table>` wrapped with `<google-sheets-html-origin>` and a `data-sheets-root="1"` attribute. We mimic that exact shape.

Recipe (run from bash):

```bash
# 1. Write TSV plain-text fallback (rows separated by \n, columns by \t)
cat > /tmp/copy-slack.tsv <<'EOF'
Header1	Header2	Header3
Value1	Value2	Value3
EOF

# 2. Write Sheets-flavored HTML (the magic markers Slack reads)
cat > /tmp/copy-slack.html <<'EOF'
<meta charset='utf-8'><google-sheets-html-origin><table xmlns="http://www.w3.org/1999/xhtml" cellspacing="0" cellpadding="0" dir="ltr" border="1" style="table-layout:fixed;font-size:10pt;font-family:Arial;border-collapse:collapse;border:none" data-sheets-root="1" data-sheets-baot="1"><tbody><tr><td style="border:1px solid #000;padding:2px 3px;background-color:#a4c2f4;font-weight:bold;text-align:center;">Header1</td><td style="border:1px solid #000;padding:2px 3px;background-color:#a4c2f4;font-weight:bold;text-align:center;">Header2</td><td style="border:1px solid #000;padding:2px 3px;background-color:#a4c2f4;font-weight:bold;text-align:center;">Header3</td></tr><tr><td style="border:1px solid #000;padding:2px 3px;">Value1</td><td style="border:1px solid #000;padding:2px 3px;">Value2</td><td style="border:1px solid #000;padding:2px 3px;">Value3</td></tr></tbody></table>
EOF

# 3. Put both flavors on the macOS pasteboard (public.html + public.utf8-plain-text)
osascript -l JavaScript <<'EOF'
ObjC.import('AppKit');
ObjC.import('Foundation');
const pb = $.NSPasteboard.generalPasteboard;
pb.clearContents;
const html = $.NSString.stringWithContentsOfFileEncodingError('/tmp/copy-slack.html', $.NSUTF8StringEncoding, null);
const tsv  = $.NSString.stringWithContentsOfFileEncodingError('/tmp/copy-slack.tsv',  $.NSUTF8StringEncoding, null);
pb.setStringForType(tsv, 'public.utf8-plain-text');
pb.setStringForType(html, 'public.html');
EOF
```

Notes:
- `pbcopy` alone won't work — it only writes plain text, and Slack needs both flavors.
- Cell escaping: HTML-encode `<`, `>`, `&` inside cell text. For TSV, replace any literal tab inside a cell with a space.
- This path produces a **table-only** message. Any prose around it has to be sent in a separate message.

## Why mixed content falls back to ASCII

When the paste contains a `<table>` plus surrounding `<p>` paragraphs, Slack ignores the Sheets markers and renders everything as plain text — the table loses its grid. So when the user wants prose **and** a native table together, the realistic flow is two pastes (prose first, native table second) or use a Slack MCP / webhook that posts via `chat.postMessage` with a Block Kit `table` block. We don't fake it with HTML.

## Slack Formatting Rules (mixed/prose path)

Write Slack's own composer markup, per Slack's help pages [Format your messages](https://slack.com/help/articles/202288908-Format-your-messages-in-Slack) and [Format your messages with markup](https://slack.com/help/articles/360039953113-Format-your-messages-in-Slack-with-markup). Use only the syntax below, not general Markdown. Markup renders on send only when "Format messages with markup" is enabled (Slack → Preferences → Advanced → Input options).

- Bold: `*text*` (single asterisks). Never `**text**`.
- Italic: `_text_`. Never `*text*`: that is bold in Slack.
- Strikethrough: `~text~`. Never `~~text~~`.
- Inline code: `` `text` ``.
- Code block: three backticks on their own line before and after the block.
- Quote: `>` at the start of every quoted line (`>quoted text`).
- Links: Markdown `[text](url)`. Do NOT use `<url|text>`: that is API `mrkdwn` syntax and renders literally in the composer. If link text isn't important, raw URLs are the most compatible fallback (Slack auto-unfurls them).
- Bulleted list: `- ` at line start. Never `•` and never `* ` (a leading `*` collides with bold markup).
- Numbered list: `1. `, `2. `, `3. ` at line start.
- On paste, Slack converts `- ` and `1. ` lines into real bulleted and numbered lists. Keep lists flat: no nested or indented items.
- Headings: there is no heading markup. Use a `*bold*` line; never `#` / `##`.
- @mentions: they do not resolve on paste (no link, no notification). Write the name as plain `@Name` and, in the confirmation, tell the user to retype each mention in Slack and pick it from autocomplete.
- Tables in triple backticks (```) with ASCII art (│ and ─)
- Generate QuickChart URL for numeric comparisons

Sample paste (mixed/prose path):

```text
*Weekly update*

Shipped:
- New onboarding flow (_beta_)
- ~Three~ Two bugs fixed, see [the tracker](https://example.com/tracker)
- `retry_limit` raised to 5

Next:
1. Roll out to 10%
2. Review metrics with @Owner

>Blocked on legal sign-off
```

## ASCII Table Format

```
*Title*

```
Column 1     │ Column 2  │ Column 3
─────────────┼───────────┼──────────
Value 1      │ Value 2   │ Value 3
Value 4      │ Value 5   │ Value 6
```
```

## Number Formatting

Always format numbers for readability:
- `2450000` → `$2.45M`
- `15.7894` → `15.8%`
- `1247` → `1,247`

## QuickChart

Generate chart URL when data has numeric comparisons:
```python
import json, urllib.parse
config = {"type": "bar", "data": {"labels": [...], "datasets": [{"data": [...]}]}}
url = f"https://quickchart.io/chart?c={urllib.parse.quote(json.dumps(config))}&w=600&h=400"
```

## Output

After copying, show:
```
✓ Copied to clipboard (format: Slack)

Preview:
[first 5-10 lines of copied content]
```

If the content has @mentions, add one line after the preview: `Retype in Slack and pick from autocomplete: @Name1, @Name2` (pasted mentions don't resolve).
