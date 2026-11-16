"""Run: python3 -I plugins/model-picker/tests/test_model_picker.py"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "model_picker.py"
sys.path.insert(0, str(ROOT / "hooks"))
import model_picker as mp  # noqa: E402


def run(payload, mode="advise", env=None):
    e = {**os.environ, "MODEL_PICKER_MODE": mode, **(env or {})}
    out = subprocess.run([sys.executable, "-I", str(HOOK), "pretool", str(ROOT)],
                         input=json.dumps(payload), capture_output=True, text=True, env=e).stdout
    return json.loads(out) if out.strip() else None


def agent(**inp):
    return {"tool_name": "Agent", "tool_input": inp, "cwd": "/nonexistent"}


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    return cond


results = []
rec = lambda t, a="": mp.recommend(t, a)[0]  # noqa: E731
results += [
    check("search -> haiku", rec("find where the login handler is defined") == "haiku"),
    check("search mentioning migration -> haiku", rec("Search for where the migration file lives") == "haiku"),
    check("read long-running output -> haiku", rec("Read the long-running test output") == "haiku"),
    check("find + write report -> haiku", rec("Find all callers of foo and write a short report") == "haiku"),
    check("find and fix -> sonnet", rec("find the null bug in parser.py and fix it") == "sonnet"),
    check("debug -> opus", rec("debug why the cache misses") == "opus"),
    check("entire migration -> fable", rec("migrate the entire codebase from v1 to v2") == "fable"),
    check("unknown -> opus", rec("think about this") == "opus"),
    check("scout -> haiku", rec("anything", "model-picker:scout") == "haiku"),
]

deny = run(agent(description="fix", prompt="find the bug and fix it"), "enforce")
results.append(check("enforce denies missing model",
                     deny and deny["hookSpecificOutput"]["permissionDecision"] == "deny"))
adv = run(agent(prompt="find x"))
results.append(check("advise never denies and never claims inheritance",
                     adv and "permissionDecision" not in adv["hookSpecificOutput"]
                     and "inherit" not in adv["hookSpecificOutput"]["additionalContext"]))
results.append(check("no updatedInput anywhere", "updatedInput" not in json.dumps([deny, adv])))
for t in ("fork", "claude-code-guide", "web-fetch"):
    results.append(check(f"{t} skipped", run(agent(subagent_type=t, prompt="x"), "enforce") is None))
results.append(check("off mode silent", run(agent(prompt="x"), "off") is None))
results.append(check("bad payload -> systemMessage, no deny",
                     (run({"tool_name": "Agent", "tool_input": "oops"}, "enforce") or {}).get("systemMessage", "")
                     .startswith("model-picker: hook error")))

with tempfile.TemporaryDirectory() as proj:
    d = Path(proj) / ".claude/agents"
    d.mkdir(parents=True)
    (d / "my-scout.md").write_text("---\nname: scout2\nmodel: haiku\n---\nbody\n")
    results.append(check("project agent pinned by frontmatter name (CLAUDE_PROJECT_DIR)",
                         run(agent(subagent_type="scout2", prompt="x"), "enforce",
                             {"CLAUDE_PROJECT_DIR": proj}) is None))

wf = lambda s: {"tool_name": "Workflow", "tool_input": {"script": s}}  # noqa: E731
results += [
    check("workflow: 'model' in prompt text is not a model option",
          run(wf('await agent("Look at the model code")'), "enforce") is not None),
    check("workflow: long prompt with model option passes",
          run(wf("await agent(`" + "x" * 2000 + "`, {model: 'haiku'})")) is None),
    check("workflow: counts missing",
          "1 of 2" in json.dumps(run(wf('agent("a", {label:"x"}); agent("b", {model: "haiku"})')))),
]

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
