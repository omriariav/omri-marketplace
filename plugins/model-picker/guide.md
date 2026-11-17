<!-- Source: https://claude.dev/blog/which-claude-model-should-you-build-with/ (2026-11-09).
     Only the tagged guide block below is injected into sessions. -->
<model-picker-guide>
Model selection rules (Anthropic guidance, Nov 2026). Apply when launching subagents, teammates, or workflow agents, and when recommending a model.

Pick the starting model by task:
- haiku (Haiku 5.5): file search, grep/locate, log and test-output reading, summarizing, classification, tagging, routing, high-volume extraction, simple fetch-and-report. Cheap-to-verify side work only.
- sonnet (Sonnet 5.5): well-scoped coding (bug fix, feature iteration, spec-driven), repeated agent tasks (investigation, review, drafting), docs/slides/spreadsheets, interactive chat.
- opus (Opus 5.5): everyday engineering and debugging, multi-PR features, migrations and performance work even with a clear spec, computer use / vision loops, complex financial or large-source documents. Default when unsure.
- fable (Fable 5.2): major refactors and migrations, architecture planning, leading many subagents, long unattended runs, deep research, scientific or quantitative analysis, or when opus at high effort still falls short.
When a task fits more than one row, pick the row for its hardest part: slides rendered and checked by eye are a vision loop (opus), not just slides (sonnet). Going stronger than the table for a concrete reason, or because the user set a rule, is fine; say why.

Rules:
1. Always pass an explicit `model` and `effort` when launching an Agent, teammate, or workflow agent(). This rule is an explicit instruction to set effort: use `low` for haiku-row side work, `high` for subtle or costly-to-get-wrong work (reviews that gate a merge, security, migrations, debugging a hard failure) and where rule 4 or 5 says so, `medium` otherwise; when model-picker recommends a model and effort, use those. If the user asked for a model or effort for this work, theirs win. Never rely on inheriting the session model or effort for side work.
2. Keep code edits and judgment on the main model; delegate verbose reading to cheaper subagents so the expensive context stays lean.
3. Adjust effort before switching models: low → medium → high. Past high, try the next model up at a lower effort instead.
4. If a model falls short: (a) give it a way to verify its own work (test, build, script), (b) raise effort one level, (c) move up one model, starting at lower effort.
5. With automatic outcome checks, run everything at low effort and re-run only failures at high.
6. Fan out parallel workers only for work larger than one context window. A chain of dependent steps or work that fits one context stays on one agent.
7. Switch the session model only at a natural break (after /compact or in a fresh session); the prompt cache belongs to the model that wrote it.
</model-picker-guide>
