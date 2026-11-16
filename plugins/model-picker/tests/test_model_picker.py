"""Run: python3 -I plugins/model-picker/tests/test_model_picker.py"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["MODEL_PICKER_CONFIG"] = "/nonexistent/model-picker.json"  # never read the real config
HOOK = ROOT / "hooks" / "model_picker.py"
sys.path.insert(0, str(ROOT / "hooks"))
import model_picker as mp  # noqa: E402
import routers  # noqa: E402


def run(payload, mode="advise", env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith("MODEL_PICKER_")}
    e.update({"MODEL_PICKER_CONFIG": "/nonexistent/model-picker.json", "MODEL_PICKER_MODE": mode, **(env or {})})
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

# --- local tier --------------------------------------------------------------
LOCAL = {"MODEL_PICKER_LOCAL_HINT": "model: haiku (gateway maps haiku to a local model)"}
results += [
    check("local: pure text -> local", mp.recommend("summarize this paragraph", local=True)[0] == "local"),
    check("local: needs files -> not local", mp.recommend("summarize the log files", local=True)[0] == "haiku"),
    check("local: disabled -> haiku", mp.recommend("summarize this paragraph")[0] == "haiku"),
    check("local: hint in deny reason",
          "gateway maps haiku" in json.dumps(run(agent(description="classify", prompt="classify this ticket text"),
                                                 "enforce", LOCAL))),
]

# --- review round 2 regressions -------------------------------------------------
results += [
    check("local: Plan stays opus", mp.recommend("extract requirements and summarize the design", "Plan", True)[0] == "opus"),
    check("local: Explore stays haiku", mp.recommend("classify the error categories", "Explore", True)[0] == "haiku"),
    check("local: rewrite parser not local", mp.recommend("Rewrite the parser module for speed", local=True)[0] != "local"),
    check("local: tag release not local", mp.recommend("tag the v1.2 release", local=True)[0] != "local"),
    check("empty description sends no prompt",
          "SSN" not in mp.task_text({"prompt": "Customer SSN list"}, {})),
    check("config 0 respected", mp._num({"K": 0}, "K", 0.5) == 0.0),
    check("INJECT false disables", not mp._enabled({"MODEL_PICKER_INJECT": False}, "MODEL_PICKER_INJECT")),
    check("missing confidence -> probability", routers._confidence({"probabilities": {"opus": 0.7}}, "opus") == 0.7),
    check("missing confidence and probs -> 1.0", routers._confidence({}, "opus") == 1.0),
]
import routers as routers  # noqa: E402,F811

# --- external routers (mock server) -------------------------------------------
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

SEEN = []


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        conf = 0.2 if "UNSURE" in json.dumps(body) else 0.9
        if self.path == "/v1/decisions":
            out = {"answers": [{"type": "choice", "name": "tier", "choice": "fable", "confidence": conf},
                               {"type": "choice", "name": "effort", "choice": "high", "confidence": 0.8}]}
        elif self.path == "/v1beta/decisions":
            out = {"answers": {"tier": {"type": "choice", "choice": "sonnet", "confidence": conf},
                               "effort": {"type": "choice", "choice": "low", "confidence": 0.7}}}
        else:
            self.send_response(500)
            self.end_headers()
            return
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


srv = HTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_port}"
OA = {"MODEL_PICKER_ROUTER": "openai-decisions", "MODEL_PICKER_ROUTER_URL": URL, "OPENAI_API_KEY": "k",
      "MODEL_PICKER_ROUTER_HEADERS": '{"x-service": "test"}', "MODEL_PICKER_ROUTER_METADATA": '{"tags": ["t"]}'}

SEEN.clear()
out = json.dumps(run(agent(description="Look around", prompt="SECRET-PROMPT find things"), "advise", OA))
req = SEEN[-1] if SEEN else {}
results += [
    check("openai router: recommendation used", "`fable`, effort high" in out and "openai-decisions" in out),
    check("openai router: headers/metadata/auth sent",
          req.get("headers", {}).get("x-service") == "test" and req["body"].get("metadata") == {"tags": ["t"]}
          and req["headers"].get("authorization") == "Bearer k"),
    check("openai router: prompt not sent by default", "SECRET-PROMPT" not in json.dumps(req.get("body"))),
]
SEEN.clear()
run(agent(description="Look around", prompt="SECRET-PROMPT"), "advise", {**OA, "MODEL_PICKER_ROUTER_INPUT": "brief"})
results.append(check("openai router: brief sends prompt head", "SECRET-PROMPT" in json.dumps(SEEN[-1]["body"])))
results.append(check("openai router: low confidence -> heuristic",
                     "unsure" in json.dumps(run(agent(description="UNSURE task", prompt="find x"), "advise", OA))))
results.append(check("router down -> heuristic, launch not blocked",
                     "router error" in json.dumps(run(agent(description="d", prompt="find x"), "advise",
                                                      {**OA, "MODEL_PICKER_ROUTER_URL": "http://127.0.0.1:9"}))))
results.append(check("read-only types skip the router",
                     "heuristic" in json.dumps(run(agent(subagent_type="Explore", description="d"), "advise", OA))))
GG = {"MODEL_PICKER_ROUTER": "google-decisions", "MODEL_PICKER_ROUTER_URL": URL, "GEMINI_API_KEY": "g"}
SEEN.clear()
out = json.dumps(run(agent(description="d", prompt="p"), "advise", GG))
results.append(check("google router: answer + api key header",
                     "`sonnet`, effort low" in out and SEEN[-1]["headers"].get("x-goog-api-key") == "g"))
CMD = {"MODEL_PICKER_ROUTER": "command",
       "MODEL_PICKER_ROUTER_CMD": f"{sys.executable} -c \"import json,sys; d=json.load(sys.stdin); "
                                  f"print(json.dumps({{'tier': d['tiers'][0], 'effort': 'low', 'confidence': 1}}))\""}
results.append(check("command router", "`haiku`, effort low" in json.dumps(run(agent(description="d"), "advise", CMD))))
with tempfile.TemporaryDirectory() as d:
    cfgf = Path(d) / "config.json"
    cfgf.write_text(json.dumps({k: v for k, v in GG.items() if k.startswith("MODEL_PICKER_")}
                               | {"MODEL_PICKER_MODE": "enforce"}))
    # API keys come from the environment only, never from the config file.
    out = run(agent(description="d", prompt="p"), env={"MODEL_PICKER_CONFIG": str(cfgf), "GEMINI_API_KEY": "g"})
    # run() always sets MODEL_PICKER_MODE=advise in env, which must win over the file.
    results.append(check("config file read; env overrides file",
                         out and "google-decisions" in json.dumps(out)
                         and "permissionDecision" not in out["hookSpecificOutput"]))
SEEN.clear()
run(agent(description="d", prompt="find x", model="opus"), "advise", OA)
results.append(check("explicit model: router not called", not SEEN))
mp.ROUTER_DEADLINE = 0.2
slow = {"MODEL_PICKER_ROUTER": "command", "MODEL_PICKER_ROUTER_CMD": "sleep 2; echo '{}'"}
import time  # noqa: E402
t0 = time.monotonic()
src = mp.decide({"description": "find x"}, {**slow, "MODEL_PICKER_ROUTER_TIMEOUT": 5})["source"]
# Either the thread deadline or the (equally capped) subprocess timeout may fire first.
results.append(check("router hard deadline -> heuristic, fast",
                     src.startswith("heuristic (router") and time.monotonic() - t0 < 1.5))
SEEN.clear()
run(agent(description="d", prompt="p"), "advise", {**OA, "OPENAI_API_KEY": "", "MODEL_PICKER_ROUTER_API_KEY_ENV": "none"})
results.append(check("keyless gateway: no auth header", SEEN and "authorization" not in SEEN[-1]["headers"]))
srv.shutdown()

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
