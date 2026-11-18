"""Run: python3 -I plugins/model-picker/tests/test_model_picker.py"""

import json
import re
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["MODEL_PICKER_CONFIG"] = "/nonexistent/model-picker.json"  # never read the real config
STATE = tempfile.mkdtemp(prefix="model-picker-test-")  # never write the real ledger
os.environ["MODEL_PICKER_STATE_DIR"] = STATE
HOOK = ROOT / "hooks" / "model_picker.py"
sys.path.insert(0, str(ROOT / "hooks"))
import model_picker as mp  # noqa: E402
import routers  # noqa: E402


def run(payload, mode="advise", env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith("MODEL_PICKER_")}
    e.update({"MODEL_PICKER_CONFIG": "/nonexistent/model-picker.json", "MODEL_PICKER_STATE_DIR": STATE,
              "MODEL_PICKER_MODE": mode, **(env or {})})
    out = subprocess.run([sys.executable, "-I", str(HOOK), "pretool", str(ROOT)],
                         input=json.dumps(payload), capture_output=True, text=True, env=e).stdout
    return json.loads(out) if out.strip() else None


def agent(**inp):
    return {"tool_name": "Agent", "tool_input": inp, "cwd": "/nonexistent"}


def _parses(text):
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def _raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


def check(name, cond):
    cond = bool(cond)
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
    check("local: never recommended for an agent launch (even pure text)",
          "local" not in json.dumps(run(agent(description="classify", prompt="classify the sentiment of this sentence: I love it, works great"),
                                        "enforce", LOCAL)).replace("pure text", "")),
    check("local: hint offered in the session rules instead",
          "gateway maps haiku" in (subprocess.run([sys.executable, "-I", str(HOOK), "session", str(ROOT)], input="{}",
                                                  capture_output=True, text=True, env={**os.environ, **LOCAL}).stdout)),
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
        elif self.path == "/api/chat":
            out = {"message": {"role": "assistant", "content": "Negative"}, "prompt_eval_count": 61,
                   "eval_count": 2, "done_reason": "stop"}
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


def _get(self):
    data = json.dumps({"models": [{"name": "qwen2.5:7b-instruct"}, {"name": "llama3.2-vision:latest"}]}).encode()
    self.send_response(200)
    self.send_header("Content-Type", "application/json")
    self.end_headers()
    self.wfile.write(data)


Mock.do_GET = _get
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
# --- v0.3 ledger: decisions, usage, pricing, report ------------------------------
import ledger  # noqa: E402


def fresh_state():
    d = tempfile.mkdtemp(prefix="model-picker-ledger-")
    return {"MODEL_PICKER_STATE_DIR": d}


def write_transcript(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def amsg(mid, model, inp=0, out=0, cr=0, cw=0, cw1h=0):
    return {"type": "assistant", "message": {"id": mid, "role": "assistant", "model": model, "content": [],
            "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
                      "cache_creation_input_tokens": cw,
                      "cache_creation": {"ephemeral_1h_input_tokens": cw1h, "ephemeral_5m_input_tokens": cw - cw1h}}}}


tbl = ledger.price_table({})
one = {"input": 1_000_000, "output": 0, "cache_read": 0, "cache_write": 0, "cache_write_1h": 0}
results += [
    check("price: opus input $4/M", abs(ledger.message_cost(one, "opus", tbl) - 4.0) < 1e-9),
    check("price: haiku prompt over 100K uses the long tier", abs(ledger.message_cost(one, "haiku", tbl) - 0.5) < 1e-9),
    check("price: haiku short prompt $0.10/M",
          abs(ledger.message_cost({**one, "input": 50_000}, "haiku", tbl) - 0.005) < 1e-9),
    check("price: 1h cache write 2x input, 5m 1.25x",
          abs(ledger.message_cost({**one, "input": 0, "cache_write": 2_000_000, "cache_write_1h": 1_000_000},
                                  "sonnet", tbl) - (2.5 + 4.0)) < 1e-9),
    check("price: override via MODEL_PICKER_PRICES",
          ledger.price_table({"MODEL_PICKER_PRICES": '{"opus": {"input": 1}}'})["opus"]["input"] == 1),
]

with tempfile.TemporaryDirectory() as d:
    sess = Path(d) / "sess.jsonl"
    write_transcript(sess, [amsg("m0", "claude-opus-5-5", inp=10, out=10)])
    sub_t = Path(d) / "sess" / "subagents" / "agent-abc.jsonl"
    write_transcript(sub_t, [amsg("m1", "claude-haiku-5-5", inp=1000, out=500, cr=20000),
                             amsg("m1", "claude-haiku-5-5", inp=1000, out=500, cr=20000),  # streamed duplicate
                             amsg("m2", "claude-haiku-5-5", inp=2000, out=100, cw=5000, cw1h=5000)])
    (sub_t.parent / "agent-abc.meta.json").write_text(json.dumps(
        {"agentType": "model-picker:scout", "description": "Find files", "toolUseId": "toolu_1"}))
    msgs = ledger.transcript_usage(sub_t)
    results += [
        check("usage: duplicates by message id counted once", len(msgs) == 2 and sum(m["input"] for m in msgs) == 3000),
        check("usage: session model from parent transcript", ledger.last_model(sess) == "claude-opus-5-5"),
    ]
    st = fresh_state()
    # SubagentStop goes through the `subagentstop` event, not `pretool`.
    proc = subprocess.run([sys.executable, "-I", str(HOOK), "subagentstop", str(ROOT)],
                          input=json.dumps({"session_id": "s", "agent_id": "abc", "transcript_path": str(sess)}),
                          capture_output=True, text=True,
                          env={**os.environ, **st, "MODEL_PICKER_VERBOSE": "1"})
    recs = ledger.read(st)
    u = recs[-1] if recs else {}
    results += [
        check("subagentstop: usage record with meta join",
              u.get("kind") == "usage" and u.get("tool_use_id") == "toolu_1" and u.get("agent_type") == "model-picker:scout"
              and u.get("tier") == "haiku" and u.get("session_model") == "claude-opus-5-5"),
        check("subagentstop: saving positive vs opus", u.get("saving", 0) > 0 and u["baseline_cost"] > u["cost"]),
        check("subagentstop: verbose note", "used" in proc.stdout and "on opus" in proc.stdout),
        check("subagentstop: stop_hook_active ignored", subprocess.run(
            [sys.executable, "-I", str(HOOK), "subagentstop", str(ROOT)],
            input=json.dumps({"stop_hook_active": True, "agent_id": "abc", "transcript_path": str(sess)}),
            capture_output=True, text=True, env={**os.environ, **st}).stdout == ""),
    ]
    local_t = Path(d) / "sess" / "subagents" / "agent-loc.jsonl"
    write_transcript(local_t, [amsg("l1", "qwen2.5:7b-instruct", inp=5000, out=500)])
    tot = ledger.summarize_usage(ledger.transcript_usage(local_t), "opus", tbl)
    results.append(check("local model: $0 cost, full baseline saved",
                         tot["local"] and tot["cost"] == 0 and tot["saving"] == tot["baseline_cost"] > 0))

    # Decisions are logged for every launch outcome; the prompt never is.
    st = fresh_state()
    base = {"session_id": "s", "tool_use_id": "t1", "transcript_path": str(sess), "cwd": "/nonexistent"}
    run({**base, "tool_name": "Agent", "tool_input": {"description": "Find logs", "prompt": "SECRET-PROMPT"}},
        "advise", st)
    run({**base, "tool_name": "Agent", "tool_input": {"description": "Find logs", "prompt": "p", "model": "haiku"}},
        "advise", st)
    run({**base, "tool_name": "Agent", "tool_input": {"description": "Fix bug", "prompt": "p", "model": "haiku"}},
        "advise", st)
    run({**base, "tool_name": "Agent", "tool_input": {"subagent_type": "fork", "prompt": "p"}}, "advise", st)
    recs = ledger.read(st)
    outcomes = [r.get("outcome") for r in recs]
    results += [
        check("decision log: one line per launch", outcomes == ["missing", "explicit", "explicit", "skipped"]),
        check("decision log: missing model resolves to session model", recs[0].get("chosen_tier") == "opus"),
        check("decision log: followed true/false", recs[1].get("followed") is True and recs[2].get("followed") is False),
        check("decision log: prompt never stored", "SECRET-PROMPT" not in Path(st["MODEL_PICKER_STATE_DIR"], "log.jsonl").read_text()),
        check("decision log: MODEL_PICKER_LOG=0 disables",
              (run({**base, "tool_name": "Agent", "tool_input": {"prompt": "p"}}, "advise",
                   {**st, "MODEL_PICKER_LOG": "0"}) or True) and len(ledger.read(st)) == 4),
    ]
    v = run({**base, "tool_name": "Agent", "tool_input": {"description": "Find logs", "prompt": "p", "model": "haiku"}},
            "advise", {**st, "MODEL_PICKER_VERBOSE": "1"})
    results.append(check("verbose: systemMessage on a clean explicit launch",
                         bool(v) and "recommends haiku" in v.get("systemMessage", "")))

    # Report aggregates both kinds.
    ledger.append(st, {"kind": "usage", "tier": "haiku", "model": "claude-haiku-5-5", "input": 1000, "output": 10,
                       "cache_read": 0, "cache_write": 0, "cost": 0.01, "baseline_cost": 0.40, "saving": 0.39})
    rep = ledger.report(st, "all")
    txt = ledger.format_report(rep)
    results += [
        check("report: counts", rep["launches"] == 5 and rep["explicit"] == 3 and rep["followed"] == 2
              and rep["compared"] == 3 and rep["skipped"] == 1),
        check("report: saving", abs(rep["saving"] - 0.39) < 1e-9 and "$0.39" in txt and "| haiku |" in txt),
        check("report: CLI", "model-picker report (all)" in subprocess.run(
            [sys.executable, "-I", str(HOOK), "report", "all"], capture_output=True, text=True,
            env={**os.environ, **st}).stdout),
        check("report: empty ledger message", "No launches logged" in ledger.format_report(ledger.report(fresh_state()))),
        check("report: bad period rejected", _raises(lambda: ledger.since_epoch("weekly"))),
    ]
    page = ledger.format_html(rep)
    evil = ledger.format_html({**rep, "launch_tiers": {"<script>x</script>": 1, "haiku": 2},
                               "recommended_tiers": {"haiku": 1, "local": 1}})
    out_html = Path(st["MODEL_PICKER_STATE_DIR"]) / "out dir" / "r.html"
    cli = subprocess.run([sys.executable, "-I", str(HOOK), "report", "--html", "--out", str(out_html), "all"],
                         capture_output=True, text=True, env={**os.environ, **st}).stdout
    results += [
        check("html: credited saving and model table", "Credited to model-picker" in page and "<td>haiku</td>" in page),
        check("html: self-contained, no external requests", not re.search(r"(src|href)=|https?://|@import", page)),
        check("html: ledger strings are escaped", "<script>" not in evil and "&lt;script&gt;" in evil),
        check("html: each tier listed once, in tier order",
              evil.count("<div class=lab>haiku</div>") == 1 and evil.index(">local<") < evil.index(">haiku<")),
        check("html: empty ledger page", "No launches logged" in ledger.format_html(ledger.report(fresh_state()))),
        check("html: CLI --out writes an owner-only file and prints its link",
              out_html.exists() and (out_html.stat().st_mode & 0o777) == 0o600 and "HTML report: file://" in cli
              and "model-picker report (all)" in cli),
        check("html: --out value is not taken as the period", "unknown period" not in cli),
    ]
    os.chmod(out_html, 0o644)
    def rep_cli(*a, extra_env=None):
        return subprocess.run([sys.executable, "-I", str(HOOK), "report", *a], capture_output=True, text=True,
                              env={**os.environ, **st, **(extra_env or {})}, cwd=st["MODEL_PICKER_STATE_DIR"])
    jh = rep_cli("all", "--json", "--html", "--out", str(out_html))
    same = rep_cli("all", "--html", "--out", "all", extra_env=None)
    no_path = rep_cli("all", "--html", "--out")
    to_dir = rep_cli("all", "--html", "--out", str(out_html.parent))
    head = rep_cli("all", "--html", "--open", "--out", str(out_html),
                   extra_env={"DISPLAY": "", "WAYLAND_DISPLAY": ""}) if sys.platform not in ("darwin", "win32") else None
    results += [
        check("html: rewrite resets an existing file to 0600", (out_html.stat().st_mode & 0o777) == 0o600),
        check("html: --json --html keeps stdout valid JSON (link on stderr)",
              _parses(jh.stdout) and "HTML report:" in jh.stderr),
        check("html: period and --out path parsed by position", "model-picker report (all)" in same.stdout
              and (Path(st["MODEL_PICKER_STATE_DIR"]) / "all").exists()),
        check("html: --out without a path is an error", no_path.returncode == 2 and "needs a file path" in no_path.stderr),
        check("html: --out to a directory gives a report error, not a hook error",
              to_dir.returncode == 2 and "could not write the HTML report" in to_dir.stderr and "hook error" not in to_dir.stdout),
    ]
    if head is not None:
        results.append(check("html: --open without a display says so instead of opening", "Could not open" in head.stdout))
    big = Path(st["MODEL_PICKER_STATE_DIR"]) / "log.jsonl"
    big.write_text("x" * (ledger.ROTATE_BYTES + 1))
    ledger.append(st, {"kind": "decision", "outcome": "skipped"})
    rotated = ledger._rotated(big.parent)
    results.append(check("ledger rotates at 5 MB", len(rotated) == 1 and big.stat().st_size < 1000))
    results.append(check("ledger files private", oct(big.stat().st_mode & 0o777) == "0o600"))

    # Review round: router stats, resumed subagents, enforce retries, unpriced models, attribution.
    st = fresh_state()
    for mid in ("x1", "x2", "x3"):
        run({**base, "tool_use_id": mid, "tool_name": "Agent",
             "tool_input": {"description": "Find logs", "prompt": "p", "model": "haiku"}}, "advise", st)
    rep = ledger.report(st, "all")
    results.append(check("router stats ignore launches that never called the router",
                         rep["router_calls"] == 0 and "Router:" not in ledger.format_report(rep)))
    run({**base, "tool_use_id": "e1", "tool_name": "Agent", "tool_input": {"description": "Find logs", "prompt": "p"}},
        "enforce", st)
    run({**base, "tool_use_id": "e2", "tool_name": "Agent",
         "tool_input": {"description": "Find logs", "prompt": "p", "model": "haiku"}}, "enforce", st)
    rep = ledger.report(st, "all")
    results.append(check("enforce deny + retry counts once", rep["launches"] == 4 and rep["denied"] == 1
                         and rep["explicit"] == 4 and rep["missing_model"] == 0))
    for cost in (0.1, 0.3):  # resumed subagent: two cumulative rows
        ledger.append(st, {"kind": "usage", "agent_id": "same", "tier": "haiku", "cost": cost,
                           "baseline_cost": 1.0, "saving": 1.0 - cost, "attribution": "model-picker"})
    rep = ledger.report(st, "all")
    results.append(check("resumed subagent counted once (last row)", rep["subagents"] == 1 and abs(rep["cost"] - 0.3) < 1e-9))
    unp = ledger.summarize_usage([{"model": "claude-legacy-x", "input": 1000, "output": 1000, "cache_read": 0,
                                   "cache_write": 0, "cache_write_1h": 0}], "opus", tbl)
    results.append(check("unpriced claude model claims no saving", unp["unpriced"] and unp["saving"] == 0))
    results.append(check("skipped launches not counted as a launch model",
                         "unknown" not in ledger.report(st, "all")["launch_tiers"]))
    results += [
        check("attribution: followed explicit -> model-picker",
              ledger.attribute({"outcome": "explicit", "followed": True, "chosen_tier": "haiku"}) == "model-picker"),
        check("attribution: definition already set it -> definition",
              ledger.attribute({"outcome": "explicit", "followed": True, "chosen_tier": "haiku",
                                "pinned": "haiku"}) == "definition"),
        check("attribution: pinned launch -> definition", ledger.attribute({"outcome": "pinned"}) == "definition"),
        check("attribution: not followed -> explicit",
              ledger.attribute({"outcome": "explicit", "followed": False, "chosen_tier": "opus"}) == "explicit"),
        check("attribution: no record -> unknown", ledger.attribute(None) == "unknown"),
    ]
    st2 = fresh_state()
    for aid, attr, sav in (("a1", "model-picker", 0.5), ("a2", "definition", 0.3)):
        ledger.append(st2, {"kind": "usage", "agent_id": aid, "tier": "haiku", "cost": 0.01,
                            "baseline_cost": sav + 0.01, "saving": sav, "attribution": attr})
    rep = ledger.report(st2, "all")
    txt = ledger.format_report(rep)
    results.append(check("report: only followed recommendations credited",
                         abs(rep["credited_saving"] - 0.5) < 1e-9 and "Credited to model-picker: $0.50" in txt
                         and "would have happened anyway" in txt))
    proc = subprocess.run([sys.executable, "-I", str(HOOK), "report", "14d"], capture_output=True, text=True,
                          env={**os.environ, **st2})
    results.append(check("report CLI: bad period is a clean error",
                         proc.returncode == 2 and "unknown period" in proc.stderr and "hook error" not in proc.stdout))

    # SubagentStop prices the baseline at the launch-time session model and attributes the saving.
    st3 = fresh_state()
    run({**base, "tool_use_id": "toolu_1", "tool_name": "Agent",
         "tool_input": {"description": "Find logs", "prompt": "p", "model": "haiku"}}, "advise", st3)
    write_transcript(sess, [amsg("m0", "claude-sonnet-5-5", inp=10, out=10)])  # user switched /model later
    subprocess.run([sys.executable, "-I", str(HOOK), "subagentstop", str(ROOT)],
                   input=json.dumps({"session_id": "s", "agent_id": "abc", "transcript_path": str(sess)}),
                   capture_output=True, text=True, env={**os.environ, **st3})
    u = [r for r in ledger.read(st3) if r.get("kind") == "usage"][-1]
    results.append(check("subagentstop: launch-time session model + attribution",
                         u.get("session_model") == "claude-opus-5-5" and u.get("attribution") == "model-picker"))

# --- v0.4: local tier via the bundled MCP server ----------------------------------
SERVER = ROOT / "mcp" / "local_server.py"


def mcp(lines, env):
    proc = subprocess.run([sys.executable, "-I", str(SERVER)], input="\n".join(json.dumps(x) for x in lines) + "\n",
                          capture_output=True, text=True, env={**os.environ, **env}, timeout=30)
    return [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]


OL = {"MODEL_PICKER_OLLAMA_URL": URL, "MODEL_PICKER_OLLAMA_MODEL": "qwen2.5:7b-instruct"}
st = fresh_state()
out = mcp([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
           {"jsonrpc": "2.0", "method": "notifications/initialized"},
           {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
           {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "ask_local", "arguments": {"prompt": "SECRET-TEXT classify"}}},
           {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "list_local_models"}},
           {"jsonrpc": "2.0", "id": 5, "method": "nope"}], {**OL, **st})
by = {r.get("id"): r for r in out}
ans = by[3]["result"]["content"][0]["text"] if 3 in by else ""
results += [
    check("mcp: initialize + no reply to notification", by[1]["result"]["serverInfo"]["name"] == "model-picker-local"
          and len(out) == 5),
    check("mcp: tools listed", [t["name"] for t in by[2]["result"]["tools"]] == ["ask_local", "list_local_models"]),
    check("mcp: ask_local answer is stamped",
          ans.startswith("Negative") and "[model-picker] answered by ollama:qwen2.5:7b-instruct" in ans),
    check("mcp: configured model marked", "(configured)" in by[4]["result"]["content"][0]["text"]),
    check("mcp: unknown method error", by[5]["error"]["code"] == -32601),
]
loc = [r for r in ledger.read(st) if r.get("kind") == "local"]
results += [
    check("mcp: call logged without text", len(loc) == 1 and loc[0]["input"] == 61
          and "SECRET-TEXT" not in Path(st["MODEL_PICKER_STATE_DIR"], "log.jsonl").read_text()
          and "Negative" not in Path(st["MODEL_PICKER_STATE_DIR"], "log.jsonl").read_text()),
    check("report: local calls section", "Local model (ask_local): 1 call(s)" in ledger.format_report(ledger.report(st, "all"))),
]
nomodel = mcp([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "ask_local", "arguments": {"prompt": "x"}}}],
              {"MODEL_PICKER_OLLAMA_URL": URL, **fresh_state()})
results.append(check("mcp: unconfigured model -> helpful error listing installed models",
                     nomodel[0]["result"]["isError"] and "qwen2.5:7b-instruct" in nomodel[0]["result"]["content"][0]["text"]))
down = mcp([{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ask_local", "arguments": {"prompt": "x"}}}],
           {**OL, "MODEL_PICKER_OLLAMA_URL": "http://127.0.0.1:9", **fresh_state()})
results.append(check("mcp: Ollama down -> isError, not a crash",
                     down[0]["result"]["isError"] and "not reachable" in down[0]["result"]["content"][0]["text"]))
off = mcp([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}], {**OL, "MODEL_PICKER_OLLAMA": "0"})
results.append(check("mcp: MODEL_PICKER_OLLAMA=0 hides tools", off[0]["result"]["tools"] == []))
results.append(check("local tier on via OLLAMA_MODEL", mp.local_hint(OL) and "ask_local" in mp.local_hint(OL)
                     and mp.decide({"description": "summarize this paragraph", "prompt": "summarize this paragraph: The cat sat on the mat all day long."},
                                   OL)["tier"] == "local"))
srv.shutdown()

# --- v0.4: model stamps -------------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    sess = Path(d) / "sess.jsonl"
    write_transcript(sess, [amsg("m0", "claude-opus-5-5", inp=10, out=10)])
    sub_dir = Path(d) / "sess" / "subagents"
    for aid, shape, tid in (("fg", "foreground", "toolu_fg"), ("bg", "background", "toolu_bg")):
        write_transcript(sub_dir / f"agent-{aid}.jsonl", [amsg(f"{aid}1", "claude-haiku-5-5", inp=1000, out=200)])
        (sub_dir / f"agent-{aid}.meta.json").write_text(json.dumps(
            {"agentType": "model-picker:scout", "toolUseId": tid, "requestShape": shape}))
    on = {**fresh_state(), "MODEL_PICKER_STAMP": "1"}

    def hook(event, payload, env):
        r = subprocess.run([sys.executable, "-I", str(HOOK), event, str(ROOT)], input=json.dumps(payload),
                           capture_output=True, text=True, env={**os.environ, **env})
        return json.loads(r.stdout) if r.stdout.strip() else None

    fg = hook("posttool", {"tool_name": "Agent", "tool_use_id": "toolu_fg", "transcript_path": str(sess)}, on)
    results += [
        check("stamp: foreground result stamped for Claude and user",
              fg and "ran on claude-haiku-5-5" in fg["hookSpecificOutput"]["additionalContext"]
              and fg["systemMessage"] == fg["hookSpecificOutput"]["additionalContext"] and "on opus" in fg["systemMessage"]),
        check("stamp: background skipped at PostToolUse",
              hook("posttool", {"tool_name": "Agent", "tool_use_id": "toolu_bg", "transcript_path": str(sess)}, on) is None),
        check("stamp: off by default",
              hook("posttool", {"tool_name": "Agent", "tool_use_id": "toolu_fg", "transcript_path": str(sess)},
                   fresh_state()) is None),
    ]
    bg = hook("subagentstop", {"agent_id": "bg", "transcript_path": str(sess)}, on)
    fgstop = hook("subagentstop", {"agent_id": "fg", "transcript_path": str(sess)}, on)
    results += [
        check("stamp: background stamp queued at SubagentStop for the next turn",
              any("ran on claude-haiku-5-5" in r.get("text", "") for r in ledger._queue_read(on))),
        check("stamp: foreground not double-stamped at SubagentStop", fgstop is None),
    ]
    ctx = hook("session", {}, {"MODEL_PICKER_STAMP": "1", "MODEL_PICKER_STAMP_REPLIES": "1", **OL})
    c = ctx["hookSpecificOutput"]["additionalContext"] if ctx else ""
    results += [
        check("session: stamp rule injected", "Model stamps are on" in c),
        check("session: reply tag rule injected", "End every reply with one line naming the model" in c),
        check("session: local tier rule injected", "ask_local" in c and "Local tier enabled" in c),
        check("session: rules off by default", "Model stamps" not in
              (hook("session", {}, {})["hookSpecificOutput"]["additionalContext"])),
    ]

# --- v0.4 review regressions --------------------------------------------------------
results += [
    check("local veto: file name", mp.recommend("summarize README.md", local=True)[0] != "local"),
    check("local veto: URL", mp.recommend("summarize the PR https://github.com/x/y/pull/1", local=True)[0] != "local"),
    check("local veto: path", mp.recommend("summarize src/app notes", local=True)[0] != "local"),
    check("local veto: doc/ticket words", mp.recommend("summarize the design doc", local=True)[0] != "local"),
    check("local still ok for pasted text", mp.recommend("translate this sentence into Hebrew", local=True)[0] == "local"),
]

err_srv = HTTPServer(("127.0.0.1", 0), type("E", (BaseHTTPRequestHandler,), {
    "log_message": lambda self, *a: None,
    "do_POST": lambda self: (self.rfile.read(int(self.headers["Content-Length"])), self.send_response(404),
                             self.send_header("Content-Type", "application/json"), self.end_headers(),
                             self.wfile.write(b'{"error":"model \'nope\' not found"}'))}))
threading.Thread(target=err_srv.serve_forever, daemon=True).start()
bad = mcp([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": [1]},
           {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "ask_local", "arguments": "oops"}},
           {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "ask_local", "arguments": {"prompt": "x", "max_tokens": -5}}},
           {"jsonrpc": "2.0", "id": 4, "method": "ping"}],
          {"MODEL_PICKER_OLLAMA_URL": f"http://127.0.0.1:{err_srv.server_port}",
           "MODEL_PICKER_OLLAMA_MODEL": "nope", **fresh_state()})
by = {r.get("id"): r for r in bad}
err_srv.shutdown()
results += [
    check("mcp: non-object params -> -32602, server survives", by[1]["error"]["code"] == -32602 and 4 in by),
    check("mcp: non-object arguments -> isError", by[2]["result"]["isError"] and "must be an object" in
          by[2]["result"]["content"][0]["text"]),
    check("mcp: HTTP 404 reported as such, not 'not reachable'",
          "HTTP 404" in by[3]["result"]["content"][0]["text"] and "not found" in by[3]["result"]["content"][0]["text"]
          and "not reachable" not in by[3]["result"]["content"][0]["text"]),
    check("mcp: ping still answered after bad requests", by[4]["result"] == {}),
]

with tempfile.TemporaryDirectory() as d:
    sess = Path(d) / "sess.jsonl"
    write_transcript(sess, [amsg("m0", "claude-opus-5-5", inp=10, out=10)])
    sub_dir = Path(d) / "sess" / "subagents"
    write_transcript(sub_dir / "agent-un.jsonl", [amsg("u1", "claude-haiku-5-5", inp=100, out=20)])
    (sub_dir / "agent-un.meta.json").write_text(json.dumps({"agentType": "x", "toolUseId": "toolu_un"}))
    on = {**fresh_state(), "MODEL_PICKER_STAMP": "1"}

    def hook2(event, payload):
        r = subprocess.run([sys.executable, "-I", str(HOOK), event, str(ROOT)], input=json.dumps(payload),
                           capture_output=True, text=True, env={**os.environ, **on})
        return json.loads(r.stdout) if r.stdout.strip() else None

    pt_unfinished = hook2("posttool", {"tool_name": "Agent", "tool_use_id": "toolu_un", "transcript_path": str(sess)})
    stop = hook2("subagentstop", {"agent_id": "un", "transcript_path": str(sess)})
    meta = json.loads((sub_dir / "agent-un.meta.json").read_text())
    (sub_dir / "agent-un.meta.json").write_text(json.dumps({**meta, "finished": True}))
    pt_after = hook2("posttool", {"tool_name": "Agent", "tool_use_id": "toolu_un", "transcript_path": str(sess)})
    results += [
        check("stamp: unknown shape, unfinished -> not stamped at PostToolUse", pt_unfinished is None),
        check("stamp: unknown shape -> stamp queued once at SubagentStop",
              len(ledger._queue_read(on)) == 1),
        check("stamp: later PostToolUse gives Claude the stamp without repeating it to the user",
              pt_after and "additionalContext" in pt_after["hookSpecificOutput"] and "systemMessage" not in pt_after),
        check("stamp: missing meta never crashes PostToolUse",
              hook2("posttool", {"tool_name": "Agent", "tool_use_id": "nope", "transcript_path": str(sess)}) is None),
    ]

# --- real-session finding: fallback guesses must not judge explicit choices ----------
results += [
    check("read verbs -> haiku", mp.recommend("report the first line of README.md")[0] == "haiku"),
    check("no signal -> opus but flagged unsure", mp.recommend("think about this")[1] == mp.UNSURE_REASON),
]
st = fresh_state()
note = run({"session_id": "s", "tool_use_id": "u1", "cwd": "/nonexistent", "tool_name": "Agent",
            "tool_input": {"description": "Ponder", "prompt": "think about this", "model": "haiku"}}, "advise", st)
d = [r for r in ledger.read(st) if r.get("kind") == "decision"][-1]
results += [
    check("unsure: no upward mismatch note on an explicit haiku launch", note is None),
    check("unsure: followed left unknown", d.get("followed") is None and d.get("unsure") is True),
    check("unsure: attribution 'unjudged', not 'explicit'", ledger.attribute(d) == "unjudged"),
    check("unsure: excluded from follow-rate denominator", ledger.report(st, "all")["compared"] == 0),
]

# --- v0.4.1: MODEL_PICKER_TIERS --------------------------------------------------
NOFABLE = {"MODEL_PICKER_TIERS": "haiku,sonnet,opus"}
big = {"description": "Migrate", "prompt": "migrate the entire codebase from v1 to v2"}
r_cap = mp.decide(big, NOFABLE)
results += [
    check("tiers: default is all Claude tiers", mp.available_tiers({}) == ["haiku", "sonnet", "opus", "fable"]),
    check("tiers: parsed and ordered", mp.available_tiers({"MODEL_PICKER_TIERS": " opus, haiku ,bogus"}) == ["haiku", "opus"]),
    check("tiers: junk-only list falls back to all", mp.available_tiers({"MODEL_PICKER_TIERS": "gpt"}) ==
          ["haiku", "sonnet", "opus", "fable"]),
    check("tiers: local listed but not configured is dropped", "local" not in mp.available_tiers(
        {"MODEL_PICKER_TIERS": "local,haiku"})),
    check("tiers: fable-level work -> opus at high effort",
          r_cap["tier"] == "opus" and r_cap["effort"] == "high" and r_cap["capped_from"] == "fable"
          and "not available" in r_cap["reason"]),
    check("tiers: available recommendation untouched", "capped_from" not in mp.decide(big, {})),
    check("tiers: haiku disabled -> next tier up, effort kept",
          mp.decide({"description": "find logs"}, {"MODEL_PICKER_TIERS": "sonnet,opus"})["tier"] == "sonnet"),
    check("tiers: local excluded -> haiku", mp.decide({"description": "summarize this paragraph"},
                                                       {**OL, "MODEL_PICKER_TIERS": "haiku,sonnet,opus"})["tier"] == "haiku"),
]
note = run(agent(description="Big job", prompt="p", model="fable"), "advise", {**NOFABLE, **fresh_state()})
results.append(check("tiers: explicit launch on an unavailable tier gets a note",
                     note and "not available in this environment" in note["hookSpecificOutput"]["additionalContext"]))
ctx = subprocess.run([sys.executable, "-I", str(HOOK), "session", str(ROOT)], input="{}", capture_output=True,
                     text=True, env={**os.environ, **NOFABLE}).stdout
results.append(check("tiers: session rules name the missing tier",
                     "Not available in this environment: fable" in ctx and "opus at high effort" in ctx))
route_out = subprocess.run([sys.executable, "-I", str(HOOK), "route"], input="migrate the entire codebase to v2",
                           capture_output=True, text=True, env={**os.environ, **NOFABLE}).stdout
results.append(check("tiers: route CLI respects the list", '"tier": "opus"' in route_out))

# The decisions API is only offered available tiers.
cap_srv = HTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=cap_srv.serve_forever, daemon=True).start()
SEEN.clear()
r_router = mp.decide({"description": "d"}, {"MODEL_PICKER_ROUTER": "openai-decisions",
                                             "MODEL_PICKER_ROUTER_URL": f"http://127.0.0.1:{cap_srv.server_port}",
                                             "MODEL_PICKER_ROUTER_API_KEY_ENV": "none", **NOFABLE})
cap_srv.shutdown()
offered = [c["value"] for c in SEEN[-1]["body"]["questions"][0]["choices"]] if SEEN else []
results += [
    check("tiers: router offered only available tiers", offered == ["haiku", "sonnet", "opus"]),
    # The mock always answers fable; a gateway that ignores the choices still gets capped.
    check("tiers: router answer outside the list is capped", r_router["tier"] in ("opus", "haiku", "sonnet")),
]

# --- v0.4.2: post-merge review of v0.4.1 ----------------------------------------------
def session_ctx(env):
    out = subprocess.run([sys.executable, "-I", str(HOOK), "session", str(ROOT)], input="{}", capture_output=True,
                         text=True, env={**os.environ, **env}).stdout
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""


results += [
    check("review: local excluded by tiers -> not advertised at session start",
          "Local tier enabled" not in session_ctx({**OL, "MODEL_PICKER_TIERS": "opus"})),
    check("review: local included -> advertised", "Local tier enabled" in session_ctx({**OL, "MODEL_PICKER_TIERS": "local,opus"})),
    check("review: non-string tier items don't crash", mp.available_tiers({"MODEL_PICKER_TIERS": [1, "opus"]}) == ["opus"]),
    check("review: typo warns instead of silently allowing everything",
          "names no Claude tier" in session_ctx({"MODEL_PICKER_TIERS": "opuss"})),
    check("review: missing middle tier steps UP keeping effort",
          (lambda r: r["tier"] == "opus" and r["effort"] == "medium")(
              mp.fit_to_available({"tier": "sonnet", "effort": "medium", "reason": "x"}, {"MODEL_PICKER_TIERS": "haiku,opus"}))),
    check("review: missing top tier still steps DOWN at high",
          (lambda r: r["tier"] == "opus" and r["effort"] == "high")(
              mp.fit_to_available({"tier": "fable", "effort": "medium", "reason": "x"}, {"MODEL_PICKER_TIERS": "haiku,sonnet,opus"}))),
]
wf = subprocess.run([sys.executable, "-I", str(HOOK), "pretool", str(ROOT)],
                    input=json.dumps({"tool_name": "Workflow", "tool_input": {"script": 'agent("a")'}}),
                    capture_output=True, text=True, env={**os.environ, **NOFABLE, **fresh_state()}).stdout
results.append(check("review: workflow advice lists only available tiers", "'opus'" in wf and "'fable'" not in wf))
den = run(agent(description="Big job", prompt="p", model="fable"), "enforce", {**NOFABLE, **fresh_state()})
results.append(check("review: enforce denies an explicit unavailable model",
                     den and den["hookSpecificOutput"].get("permissionDecision") == "deny"))
with tempfile.TemporaryDirectory() as proj:
    adir = Path(proj) / ".claude/agents"
    adir.mkdir(parents=True)
    (adir / "fab.md").write_text("---\nname: fab\nmodel: fable\n---\nbody\n")
    pin = run(agent(subagent_type="fab", prompt="p"), "advise", {**NOFABLE, "CLAUDE_PROJECT_DIR": proj, **fresh_state()})
    results.append(check("review: agent pinned to an unavailable tier gets a note",
                         pin and "pinned to fable" in pin["hookSpecificOutput"]["additionalContext"]
                         and 'model: "opus"' in pin["hookSpecificOutput"]["additionalContext"]))

# --- v0.4.2 pre-merge review ---------------------------------------------------------
results += [
    check("prereview: session text describes step-up rule",
          "next stronger available tier at the same effort" in session_ctx({"MODEL_PICKER_TIERS": "haiku,opus"})),
    check("prereview: 'local' inside a model name is not the local tier", mp.tier_of("my-local-llama") is None),
]
st = fresh_state()
run(agent(description="Big job", prompt="p", model="fable"), "enforce", {**NOFABLE, **st})
run(agent(description="Big job", prompt="p", model="opus"), "enforce", {**NOFABLE, **st})
rep = ledger.report(st, "all")
results.append(check("prereview: enforce deny + retry on an unavailable model counts once",
                     rep["launches"] == 1 and rep["denied"] == 1))

# --- ROUTER_ON_EXPLICIT --------------------------------------------------------------
oe_srv = HTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=oe_srv.serve_forever, daemon=True).start()
RT = {"MODEL_PICKER_ROUTER": "openai-decisions", "MODEL_PICKER_ROUTER_URL": f"http://127.0.0.1:{oe_srv.server_port}",
      "MODEL_PICKER_ROUTER_API_KEY_ENV": "none"}
SEEN.clear()
st = fresh_state()
run(agent(description="Look around", prompt="p", model="haiku"), "advise", {**RT, **st})
off_calls = len(SEEN)
run(agent(description="Look around", prompt="p", model="fable"), "advise", {**RT, **st, "MODEL_PICKER_ROUTER_ON_EXPLICIT": "1"})
on_calls = len(SEEN) - off_calls
oe_srv.shutdown()
d = [r for r in ledger.read(st) if r.get("kind") == "decision"]
results += [
    check("on_explicit off: explicit launch never calls the router", off_calls == 0),
    check("on_explicit on: explicit launch is judged by the router",
          on_calls == 1 and str(d[-1].get("source", "")).startswith("openai-decisions")),
    check("on_explicit on: router verdict sets followed (mock says fable)", d[-1].get("followed") is True),
]

# --- re-review: lock in verified behaviour --------------------------------------------
def outcomes(env, launches, mode):
    st = fresh_state()
    for inp in launches:
        run(agent(**inp), mode, {**env, **st})
    return [r.get("outcome") for r in ledger.read(st) if r.get("kind") == "decision"]


with tempfile.TemporaryDirectory() as proj:
    adir = Path(proj) / ".claude/agents"
    adir.mkdir(parents=True)
    (adir / "fab.md").write_text("---\nname: fab\nmodel: fable\n---\nbody\n")
    pinned_env = {**NOFABLE, "CLAUDE_PROJECT_DIR": proj}
    results += [
        check("pinned+unavailable: enforce logs denied", outcomes(pinned_env, [{"subagent_type": "fab", "prompt": "p"}],
                                                                  "enforce") == ["denied"]),
        check("pinned+unavailable: advise logs pinned", outcomes(pinned_env, [{"subagent_type": "fab", "prompt": "p"}],
                                                                 "advise") == ["pinned"]),
    ]
results += [
    check("explicit unavailable: advise logs explicit",
          outcomes(NOFABLE, [{"description": "d", "prompt": "p", "model": "fable"}], "advise") == ["explicit"]),
    check("missing model: enforce denied / advise missing",
          outcomes({}, [{"description": "d", "prompt": "p"}], "enforce") == ["denied"]
          and outcomes({}, [{"description": "d", "prompt": "p"}], "advise") == ["missing"]),
]
down = {"MODEL_PICKER_ROUTER": "openai-decisions", "MODEL_PICKER_ROUTER_URL": "http://127.0.0.1:9",
        "MODEL_PICKER_ROUTER_API_KEY_ENV": "none", "MODEL_PICKER_ROUTER_ON_EXPLICIT": "1"}
st = fresh_state()
o = run(agent(description="Find logs", prompt="p", model="haiku"), "enforce", {**down, **st})
d = [r for r in ledger.read(st) if r.get("kind") == "decision"][-1]
results.append(check("on_explicit + router down: not blocked, heuristic source",
                     (o is None or o.get("hookSpecificOutput", {}).get("permissionDecision") != "deny")
                     and str(d.get("source", "")).startswith("heuristic (router")))
slow_srv = HTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=slow_srv.serve_forever, daemon=True).start()
SEEN.clear()
run(agent(description="Big", prompt="p", model="fable"), "enforce",
    {**NOFABLE, "MODEL_PICKER_ROUTER": "openai-decisions", "MODEL_PICKER_ROUTER_URL": f"http://127.0.0.1:{slow_srv.server_port}",
     "MODEL_PICKER_ROUTER_API_KEY_ENV": "none", "MODEL_PICKER_ROUTER_ON_EXPLICIT": "1", **fresh_state()})
results.append(check("on_explicit: no router call for a launch on an unavailable model", len(SEEN) == 0))
SEEN.clear()
mm = run(agent(description="Find logs", prompt="p", model="haiku"), "advise",
         {"MODEL_PICKER_ROUTER": "openai-decisions", "MODEL_PICKER_ROUTER_URL": f"http://127.0.0.1:{slow_srv.server_port}",
          "MODEL_PICKER_ROUTER_API_KEY_ENV": "none", "MODEL_PICKER_ROUTER_ON_EXPLICIT": "1", **fresh_state()})
slow_srv.shutdown()
results.append(check("on_explicit: mismatch note uses the router verdict (mock: fable vs haiku)",
                     mm and "reads as `fable`" in mm["hookSpecificOutput"]["additionalContext"]))

# --- report renders as Markdown -------------------------------------------------------
st = fresh_state()
ledger.append(st, {"kind": "decision", "outcome": "explicit", "chosen_tier": "haiku", "rec_tier": "haiku",
                   "followed": True, "source": "openai-decisions:x (0.9)", "router_ms": 700, "confidence": 0.9})
for aid, attr in (("a1", "model-picker"), ("a2", "unknown")):
    ledger.append(st, {"kind": "usage", "agent_id": aid, "tier": "haiku", "input": 1000, "output": 10, "cache_read": 0,
                       "cache_write": 0, "cost": 0.01, "baseline_cost": 0.5, "saving": 0.49, "attribution": attr})
ledger.append(st, {"kind": "local", "model": "ollama:x", "input": 10, "output": 2, "haiku_equiv": 0.0})
md = ledger.format_report(ledger.report(st, "all")).split("\n")
bad = [ln for ln in md if ln.strip() and not ln.startswith(("- ", "  - ", "**", "|", "_"))]
results += [
    check("report markdown: every line is a heading, bullet, table row, or note", not bad),
    check("report markdown: blank line before the table",
          any(md[i] == "" and md[i + 1].startswith("| Model") for i in range(len(md) - 1))),
    check("report markdown: attribution nested under the credited bullet",
          any(ln.startswith("  - Not credited") for ln in md)),
]

# --- v0.4.4: background stamps delivered on the next turn ------------------------------
def hookrun(event, payload, env):
    r = subprocess.run([sys.executable, "-I", str(HOOK), event, str(ROOT)], input=json.dumps(payload),
                       capture_output=True, text=True, env={**os.environ, **env})
    return json.loads(r.stdout) if r.stdout.strip() else None


with tempfile.TemporaryDirectory() as d:
    sess = Path(d) / "sess.jsonl"
    write_transcript(sess, [amsg("m0", "claude-opus-5-5", inp=10, out=10)])
    sd = Path(d) / "sess" / "subagents"
    write_transcript(sd / "agent-bg1.jsonl", [amsg("b1", "claude-haiku-5-5", inp=1000, out=50)])
    (sd / "agent-bg1.meta.json").write_text(json.dumps({"agentType": "scout", "toolUseId": "t_bg1",
                                                        "requestShape": "background"}))
    on = {**fresh_state(), "MODEL_PICKER_STAMP": "1"}
    hookrun("subagentstop", {"session_id": "S1", "agent_id": "bg1", "transcript_path": str(sess)}, on)
    other = hookrun("prompt", {"session_id": "S2"}, on)
    first = hookrun("prompt", {"session_id": "S1"}, on)
    again = hookrun("prompt", {"session_id": "S1"}, on)
    ctx = (first or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
    results += [
        check("bg stamp: not delivered to another session", other is None),
        check("bg stamp: delivered to Claude on the next turn",
              "ran on claude-haiku-5-5" in ctx and "name its model" in ctx and first.get("systemMessage")),
        check("bg stamp: delivered once", again is None),
    ]
    # Resumed agent stops again with more work: the newer stamp is delivered once more.
    write_transcript(sd / "agent-bg1.jsonl", [amsg("b1", "claude-haiku-5-5", inp=1000, out=50),
                                              amsg("b2", "claude-haiku-5-5", inp=5000, out=500)])
    time.sleep(0.01)
    hookrun("subagentstop", {"session_id": "S1", "agent_id": "bg1", "transcript_path": str(sess)}, on)
    resumed = hookrun("prompt", {"session_id": "S1"}, on)
    results.append(check("bg stamp: resumed agent's newer stamp delivered",
                         resumed and "ran on claude-haiku-5-5" in resumed["hookSpecificOutput"]["additionalContext"]))
    results.append(check("bg stamp: off when STAMP is off", hookrun("prompt", {"session_id": "S1"}, fresh_state()) is None))
    sess_ctx = hookrun("session", {}, {"MODEL_PICKER_STAMP": "1"})["hookSpecificOutput"]["additionalContext"]
    results.append(check("session rules describe next-turn delivery", "arrives with your next turn" in sess_ctx))

# --- v0.4.4 review fixes ----------------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    sess = Path(d) / "sess.jsonl"
    write_transcript(sess, [amsg("m0", "claude-opus-5-5", inp=10, out=10)])
    sd = Path(d) / "sess" / "subagents"
    write_transcript(sd / "agent-x.jsonl", [amsg("x1", "claude-haiku-5-5", inp=100, out=5)])
    (sd / "agent-x.meta.json").write_text(json.dumps({"agentType": "x", "toolUseId": "t_x", "requestShape": "background"}))
    nolog = {**fresh_state(), "MODEL_PICKER_STAMP": "1", "MODEL_PICKER_LOG": "0"}
    out = hookrun("subagentstop", {"session_id": "S", "agent_id": "x", "transcript_path": str(sess)}, nolog)
    results.append(check("LOG=0: background stamp falls back to a user-visible line",
                         out and "ran on claude-haiku-5-5" in out.get("systemMessage", "")))

    on = {**fresh_state(), "MODEL_PICKER_STAMP": "1"}
    # Race: a resumed agent's second pending lands after the first delivery read: it must still be delivered.
    hookrun("subagentstop", {"session_id": "S", "agent_id": "x", "transcript_path": str(sess)}, on)
    first = hookrun("prompt", {"session_id": "S"}, on)
    ledger.queue_stamp(on, "S", "x", "[model-picker] x ran on late")
    second = hookrun("prompt", {"session_id": "S"}, on)
    third = hookrun("prompt", {"session_id": "S"}, on)
    results += [
        check("pid dedupe: first delivered", first is not None),
        check("pid dedupe: a later pending for the same agent is still delivered",
              second and "ran on late" in second["hookSpecificOutput"]["additionalContext"]),
        check("pid dedupe: nothing delivered twice", third is None),
    ]
    # Concurrency: 8 turns of one session racing for one stamp deliver it exactly once.
    ledger.queue_stamp(on, "S", "y", "[model-picker] y ran on z")
    procs = [subprocess.Popen([sys.executable, "-I", str(HOOK), "prompt", str(ROOT)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True, env={**os.environ, **on}) for _ in range(8)]
    outs = [p.communicate(json.dumps({"session_id": "S"}))[0] for p in procs]
    results.append(check("8 racing turns: stamp delivered exactly once", sum("y ran on z" in o for o in outs) == 1))
    # Another session's queued stamp is never taken by this session.
    ledger.queue_stamp(on, "OTHER", "w", "[model-picker] w ran on q")
    results.append(check("queue: other session's stamp untouched", hookrun("prompt", {"session_id": "S"}, on) is None
                         and ledger.take_stamps(on, "OTHER") == ["[model-picker] w ran on q"]))
    # Bookkeeping failure: if the queue can't be rewritten, nothing is shown (never re-shown on every prompt).
    ledger.queue_stamp(on, "S", "v", "[model-picker] v ran on u")
    qdir = Path(on["MODEL_PICKER_STATE_DIR"])
    os.chmod(qdir, 0o500)
    try:
        blocked = ledger.take_stamps(on, "S")
    finally:
        os.chmod(qdir, 0o700)
    capst = fresh_state()
    for i in range(700):
        ledger.queue_stamp(capst, f"Z{i}", "a", "t")
    results.append(check("queue capped at QUEUE_MAX_RECORDS", len(ledger._queue_read(capst)) <= ledger.QUEUE_MAX_RECORDS))
    ledger.take_stamps(capst, "Z699")
    results.append(check("queue drops delivered records", all(r.get("session") != "Z699" for r in ledger._queue_read(capst))))
    results.append(check("queue write failure: nothing delivered, stamp kept for later",
                         blocked == [] and ledger.take_stamps(on, "S") == ["[model-picker] v ran on u"]))
    results.append(check("prompt hook errors are silent", subprocess.run(
        [sys.executable, "-I", str(HOOK), "prompt", str(ROOT)], input="not json", capture_output=True, text=True,
        env={**os.environ, **on}).stdout == ""))

# read(): rotated files older than `since` are skipped by name.
st = fresh_state()
sdir = Path(st["MODEL_PICKER_STATE_DIR"])
sdir.mkdir(parents=True, exist_ok=True)
old_ms = int((time.time() - 3 * 86400) * 1000)
(sdir / f"log.{old_ms}.jsonl").write_text(json.dumps({"ts": time.time() - 3 * 86400 - 10, "kind": "decision"}) + "\n")
results.append(check("read: old rotated file skipped by its name",
                     all(r.get("kind") != "decision" for r in ledger.read(st, time.time() - 86400))))
tf = sdir / "tail.txt"
tf.write_text("aaaa\nbbbb\ncccc\n")
results += [
    check("tail: window starting exactly at a line keeps it", ledger._tail_lines(tf, 10) == ["bbbb", "cccc"]),
    check("tail: window starting mid-line drops the partial", ledger._tail_lines(tf, 8) == ["bbb", "cccc"][1:]),
]
big = sdir / "log.jsonl"
with open(big, "w") as fh:
    for i in range(38000):
        fh.write(json.dumps({"ts": time.time() - 100, "kind": "decision", "outcome": "explicit", "i": i}) + "\n")
t0 = time.monotonic()
hookrun("prompt", {"session_id": "S"}, {**st, "MODEL_PICKER_STAMP": "1"})
dt = time.monotonic() - t0
results.append(check(f"prompt hook on a ~5MB ledger stays fast ({dt*1000:.0f} ms incl. interpreter start)", dt < 0.35))


# --- local guard: `local` must be earned (both review rounds' examples) ------------------
LV = {"tier": "local", "effort": "low", "reason": "r", "source": "openai-decisions:x (0.90)", "confidence": 0.9}
TOOL_WORK = [  # (description, prompt): tool work; checked below: never `local` on launch
    ("Label vendor-dependent keys pending", "Add a pending label to the two keys in SKILL.md and open a PR"),
    ("Tidy config", "edit config.yaml and commit the change"),
    ("Docs", "update the README"), ("Bug", "fix the bug in auth"), ("Logs", "look at the logs"),
    ("Diff", "check the diff"), ("Tests", "Run pytest and report failures"), ("PR", "Review my PR #12"),
    ("Cleanup", "Delete the temp folder"), ("Debug", "Debug the failing test"), ("Test", "Add a test for login"),
    ("TODOs", "Find all TODOs in the codebase"), ("Files", "List the files in the repo"),
    ("Migrate", "execute the migration"), ("Sync", "pull the latest changes"), ("API", "curl the endpoint"),
    ("Save", "Write the results to a file"), ("Rename", "Rename the variable foo to bar across the project"),
    ("Summarize text", "Summarize this:\n" + "word " * 120 + "\nthen git commit the changes and open a PR"),
    ("Notes", "Summarize the meeting notes"),  # nothing pasted: ask_local can't see them
]
TEXT_WORK = [  # pasted-text work; never `local` on launch, but `local` via the skill/route path
    ("Summarize this email", "Summarize this email in 3 bullets:\n\nHi team, see the link and page 3.5 e.g. and/or notes."),
    ("Summarize", "Summarize the 3.5 results: revenue up 3.5%, churn flat, NPS 41 (see page 2)."),
    ("Extract names", "Extract the names from the paragraph below: Bob and/or Alice met Carol in Paris."),
    ("Translate", "Translate to French: hello, e.g. world"),
    ("Translate", "Translate: see config.yaml for details"),
    ("Summarize log", "Summarize this log: Traceback (most recent call last): File main.py line 3 KeyError 'x'"),
    ("Classify", "Classify the sentiment of this review: The battery died after two days."),
]
ALWAYS_LOCAL = {"MODEL_PICKER_ROUTER": "command", "MODEL_PICKER_OLLAMA_MODEL": "qwen2.5:7b-instruct",
                "MODEL_PICKER_ROUTER_CMD": f"{sys.executable} -c \"print('{{\\\"tier\\\": \\\"local\\\", "
                                           f"\\\"effort\\\": \\\"low\\\", \\\"confidence\\\": 0.9}}')\""}
probe = mp.decide({"description": "Translate", "prompt": "Translate to French: hello"}, ALWAYS_LOCAL)
launch_local = [p for d, p in TOOL_WORK + TEXT_WORK
                if mp.decide({"description": d, "prompt": p}, ALWAYS_LOCAL, for_launch=True)["tier"] == "local"]
results += [
    check("fake router really answers local (skill path keeps a confident local)", probe["tier"] == "local"),
    check(f"agent launches never get local, even when the router says so ({len(TOOL_WORK + TEXT_WORK)} cases)",
          not launch_local),
    check("skill path: pasted-text tasks keep a confident local",
          all(mp.decide({"description": d, "prompt": p}, ALWAYS_LOCAL)["tier"] == "local" for d, p in TEXT_WORK)),
    check("launch path: router not even offered local",
          "local" not in mp.decide({"description": "d", "prompt": "p"}, {**ALWAYS_LOCAL}, for_launch=True)["tier"]),
    check("skill path: shaky local call falls back with honest metadata",
          (lambda r: r["tier"] != "local" and r["source"].startswith("heuristic (local vetoed") and r["confidence"] is None)(
              mp.guard_local({**LV, "confidence": 0.6}, {"description": "Summarize", "prompt": "Summarize: x"}, {}))),
    check("local guard: non-local recs untouched", mp.guard_local({**LV, "tier": "sonnet"}, {"prompt": "x"}, {})["tier"] == "sonnet"),
]

wf = subprocess.run([sys.executable, "-I", str(HOOK), "pretool", str(ROOT)],
                    input=json.dumps({"tool_name": "Workflow", "tool_input": {"script": 'agent("a")'}}),
                    capture_output=True, text=True, env={**os.environ, **OL, **fresh_state()}).stdout
results.append(check("workflow advice never routes an agent() step to local",
                     "not as an agent() step" in wf and "use the local model" not in wf))


# --- v0.4.6: floors, followed-by-direction, prompt routing -------------------------------------------
FL = {"MODEL_PICKER_FLOORS": json.dumps({"review": "opus", "^audit": "fable", "bad": "local", "x": "nonsense"})}
fl_review = mp.decide({"description": "Review PR #7", "prompt": "check it"}, FL)
results += [
    check("floor: 'review' raises sonnet work to opus, with a reason and a confident verdict",
          fl_review["tier"] == "opus" and "your rule" in fl_review["reason"] and not fl_review.get("unsure")
          and fl_review["source"].endswith("+ floor")),
    check("floor: plain word matches at a word start only ('preview' is not 'review')",
          mp.decide({"description": "Preview the page title", "prompt": "p"}, FL).get("floor") is None),
    check("floor: regex patterns work", mp.decide({"description": "audit the auth module", "prompt": "p"}, FL)["tier"] == "fable"),
    check("floor: never lowers a stronger recommendation",
          mp.decide({"description": "review the major refactor of the whole repo", "prompt": "p"},
                    {**FL, "MODEL_PICKER_FLOORS": json.dumps({"review": "sonnet"})})["tier"] == "fable"),
    check("floor: local/unknown tiers and bad JSON are ignored",
          mp.decide({"description": "bad x", "prompt": "find the file"}, FL)["tier"] == "haiku"
          and mp.decide({"description": "Review PR", "prompt": "p"}, {"MODEL_PICKER_FLOORS": "{not json"})["tier"] == "sonnet"),
    check("floor: unavailable floor tier is fitted to what's available",
          mp.decide({"description": "audit logs", "prompt": "p"}, {**FL, "MODEL_PICKER_TIERS": "haiku,sonnet,opus"})["tier"] == "opus"),
]
fst = fresh_state()
run(agent(description="Review PR #7", prompt="review it", model="opus"), env={**fst, **FL})
dec = [r for r in ledger.read(fst) if r.get("kind") == "decision"][-1]
results.append(check("floor: an opus review launch now counts as followed", dec["rec_tier"] == "opus" and dec["followed"] is True))

dst = fresh_state()
for chosen, rec, followed in (("opus", "sonnet", False), ("sonnet", "opus", False), ("opus", "haiku", False), ("haiku", "haiku", True)):
    ledger.append(dst, {"kind": "decision", "outcome": "explicit", "chosen_tier": chosen, "rec_tier": rec, "followed": followed,
                        "source": "heuristic"})
drep = ledger.report(dst, "all")
results += [
    check("report: not-followed split by direction", drep["went_stronger"] == 2 and drep["went_cheaper"] == 1),
    check("report: direction shown in Markdown and HTML", "2 went to a stronger model, 1 to a cheaper one" in ledger.format_report(drep)
          and "2 went stronger, 1 cheaper" in ledger.format_html(drep)),
]

FAKE_SONNET = {"MODEL_PICKER_ROUTER": "command", "MODEL_PICKER_ROUTER_CMD": f"{sys.executable} -c \"print('{{\\\"tier\\\": \\\"local\\\", "
               f"\\\"effort\\\": \\\"low\\\", \\\"confidence\\\": 0.9}}')\"", "MODEL_PICKER_OLLAMA_MODEL": "qwen2.5:7b-instruct"}


def FAKE_ROUTER(tier, conf):
    return {"MODEL_PICKER_ROUTER": "command", "MODEL_PICKER_ROUTER_CMD":
            f"{sys.executable} -c \"print('{{\\\"tier\\\": \\\"{tier}\\\", \\\"effort\\\": \\\"medium\\\", \\\"confidence\\\": {conf}}}')\""}


_prompt_n = [0]


def prompt_hook(prompt, env, session=None):
    """Each call is its own session (a first prompt) unless a session id is given."""
    e = {k: v for k, v in os.environ.items() if not k.startswith("MODEL_PICKER_")}
    e.update({"MODEL_PICKER_CONFIG": "/nonexistent/model-picker.json", **env})
    _prompt_n[0] += 1
    sid = session or f"s-prompt-{_prompt_n[0]}"
    out = subprocess.run([sys.executable, "-I", str(HOOK), "prompt", str(ROOT)], capture_output=True, text=True, env=e,
                         input=json.dumps({"session_id": sid, "prompt": prompt, "transcript_path": "/nonexistent"})).stdout
    return json.loads(out) if out.strip() else None


def session_hook(env, session, source="compact"):
    e = {k: v for k, v in os.environ.items() if not k.startswith("MODEL_PICKER_")}
    e.update({"MODEL_PICKER_CONFIG": "/nonexistent/model-picker.json", **env})
    subprocess.run([sys.executable, "-I", str(HOOK), "session", str(ROOT)], capture_output=True, text=True, env=e,
                   input=json.dumps({"session_id": session, "source": source}))


pst = fresh_state()
ON = {**pst, "MODEL_PICKER_PROMPT_ROUTE": "1"}
secret = "Summarize the quarterly numbers in this pasted text: SECRET-TOKEN-123"
shown = prompt_hook(secret, {**ON, **FAKE_ROUTER("sonnet", 0.82)})
local_try = prompt_hook(secret, {**ON, **FAKE_SONNET})
results += [
    check("prompt route: off by default", prompt_hook("find where the parser lives in this repo", pst) is None),
    check("prompt route: shows the pick to the user only (no context for Claude)",
          bool(shown) and "→ sonnet" in shown.get("systemMessage", "") and "hookSpecificOutput" not in shown),
    check("prompt route: a router answering local shows nothing (local isn't offered for the session)", local_try is None),
    check("prompt route: line format", bool(shown) and re.search(r"model-picker: command router → sonnet/medium \(0\.82, \d+ ms\)$", shown["systemMessage"])),
    check("prompt route: skips slash commands, notifications and short replies",
          all(prompt_hook(p, ON) is None for p in ("/model-picker:report 7d", "<task-notification>done</task-notification>", "ok merge"))),
    check("prompt route: a failing router is labeled unavailable; nothing shown when none is configured",
          (prompt_hook("debug the flaky login test in CI", {**ON, "MODEL_PICKER_ROUTER": "command",
                                                            "MODEL_PICKER_ROUTER_CMD": "false"}) or {}).get("systemMessage")
          == "model-picker: command router unavailable (command exit 1)"
          and prompt_hook("debug the flaky login test in CI", ON) is None),
    check("prompt route: a bad answer (router up) is not labeled unavailable",
          prompt_hook("debug the flaky login test in CI", {**ON, "MODEL_PICKER_ROUTER": "command",
                                                           "MODEL_PICKER_ROUTER_CMD": "echo nope"}) is None),
    check("prompt route: shows the router's raw pick, not floors",
          "→ sonnet" in (prompt_hook("review PR 55 for bugs please", {**ON, **FL, **FAKE_ROUTER("sonnet", 0.4)}) or {}).get("systemMessage", "")),
]
# 0.5.0: with PROMPT_ROUTE=1 the router is called only at natural breaks.
import urllib.error  # noqa: E402
DOWN_EARLY = {"MODEL_PICKER_ROUTER": "openai-decisions", "MODEL_PICKER_ROUTER_URL": "http://127.0.0.1:9",
              "MODEL_PICKER_ROUTER_MODEL": "team/jev-latest", "MODEL_PICKER_ROUTER_API_KEY_ENV": "none"}
cnt = Path(fresh_state()["MODEL_PICKER_STATE_DIR"]) / "calls.txt"
counter = Path(cnt.parent) / "count.py"
counter.write_text("import sys, json\nopen(%r, 'a').write('x')\nprint(json.dumps({'tier': 'sonnet', 'effort': 'medium', 'confidence': 0.9}))\n" % str(cnt))
nb = {"MODEL_PICKER_STATE_DIR": str(cnt.parent), "MODEL_PICKER_PROMPT_ROUTE": "1", "MODEL_PICKER_ROUTER": "command",
      "MODEL_PICKER_ROUTER_CMD": f"{sys.executable} {counter}"}
calls = lambda: len(cnt.read_text()) if cnt.exists() else 0  # noqa: E731
first = prompt_hook("debug the flaky login test in CI", nb, session="s-nb")
second = prompt_hook("also add a retry to the upload step", nb, session="s-nb")
short_first = prompt_hook("ok", nb, session="s-nb2")
after_short = prompt_hook("debug the flaky login test in CI", nb, session="s-nb2")
c1 = calls()
session_hook(nb, "s-nb")
after_compact = prompt_hook("now write the migration for the users table", nb, session="s-nb")
again = prompt_hook("and the rollback too please", nb, session="s-nb")
c2 = calls()
bf = mp._session_file(nb, "prompt-breaks", "s-nb")
st = json.loads(bf.read_text())
bf.write_text(json.dumps({**st, "last": st["last"] - 3601}))
after_idle = prompt_hook("start on the billing export feature", nb, session="s-nb")
bf.write_text(json.dumps({**json.loads(bf.read_text()), "last": json.loads(bf.read_text())["last"] - 3601}))
c_idle = calls()
no_idle = prompt_hook("start on the billing export feature", {**nb, "MODEL_PICKER_PROMPT_ROUTE_IDLE": "0"}, session="s-nb")
no_idle_calls = calls() - c_idle
alw = {**nb, "MODEL_PICKER_PROMPT_ROUTE": "always"}
c3 = calls()
always = [prompt_hook("debug the flaky login test in CI", alw, session="s-alw") for _ in range(2)]
c4 = calls()
down = {**fresh_state(), "MODEL_PICKER_PROMPT_ROUTE": "1", "MODEL_PICKER_ROUTER": "command", "MODEL_PICKER_ROUTER_CMD": "false"}
prompt_hook("debug the flaky login test in CI", down, session="s-down")
pending_after_fail = json.loads(mp._session_file(down, "prompt-breaks", "s-down").read_text())["pending"]
# A router that comes back after failing: the next eligible prompt is routed, then the break closes.
recovered = prompt_hook("debug the flaky login test in CI", {**down, **FAKE_ROUTER("sonnet", 0.9)}, session="s-down")
closed_after = prompt_hook("debug the flaky login test in CI", {**down, **FAKE_ROUTER("sonnet", 0.9)}, session="s-down")
# An unusable reply (local, garbage) closes the break: never a billed call on every prompt.
def _bad_reply_calls(body):
    st = fresh_state()
    c = Path(st["MODEL_PICKER_STATE_DIR"]) / "calls.txt"
    sc = Path(st["MODEL_PICKER_STATE_DIR"]) / "bad.py"
    sc.write_text("import sys\nopen(%r, 'a').write('x')\nprint(%r)\n" % (str(c), body))
    env = {**st, "MODEL_PICKER_PROMPT_ROUTE": "1", "MODEL_PICKER_ROUTER": "command", "MODEL_PICKER_ROUTER_CMD": f"{sys.executable} {sc}"}
    for _ in range(3):
        prompt_hook("debug the flaky login test in CI", env, session="s-bad")
    return len(c.read_text()) if c.exists() else 0
bad_calls = [_bad_reply_calls(b) for b in ('{"tier": "local", "effort": "low", "confidence": 0.9}', "not json", '{"tier": "gpt"}')]
# A paused router keeps the break open without calling it.
pz = {**fresh_state(), **DOWN_EARLY, "MODEL_PICKER_PROMPT_ROUTE": "1"}
for _ in range(3):
    mp.record_router(pz, urllib.error.URLError(ConnectionRefusedError()), scope="prompt")
prompt_hook("debug the flaky login test in CI", pz, session="s-pz")
pz_pending = json.loads(mp._session_file(pz, "prompt-breaks", "s-pz").read_text())["pending"]
pz_fails = mp._health_read(pz).get("prompt_fails")  # still 3: the paused router was not called
# A corrupt state file counts as a break; mixed-case values.
cf = mp._session_file(nb, "prompt-breaks", "s-corrupt")
cf.parent.mkdir(parents=True, exist_ok=True)
cf.write_bytes(b"\xff{not json")
corrupt_routed = prompt_hook("debug the flaky login test in CI", nb, session="s-corrupt")
modes = [mp._prompt_route_mode({"MODEL_PICKER_PROMPT_ROUTE": v}) for v in (" ALWAYS ", "true", True, "1", "0", "off", None)]
mp.mark_break({**nb, "MODEL_PICKER_PROMPT_ROUTE": "always"}, "s-always-mark")
# Resume keeps the idle clock (last) while marking the break.
rs = mp._session_file(nb, "prompt-breaks", "s-nb")
before_last = json.loads(rs.read_text())["last"]
session_hook(nb, "s-nb", source="resume")
resumed = json.loads(rs.read_text())
results += [
    check("breaks: the session's first prompt is routed, a mid-task follow-up is not",
          bool(first) and second is None and c1 == 2),
    check("breaks: a short or slash first prompt leaves the break open for the next real prompt",
          short_first is None and bool(after_short)),
    check("breaks: /compact (SessionStart) reopens the break, once",
          bool(after_compact) and again is None and c2 == 3),
    check("breaks: a prompt after an idle hour is routed", bool(after_idle)),
    check("breaks: PROMPT_ROUTE_IDLE=0 turns the idle break off", no_idle is None and no_idle_calls == 0),
    check("breaks: PROMPT_ROUTE=always routes every prompt", all(always) and c4 - c3 == 2),
    check("breaks: a router failure keeps the break open for the next eligible prompt, which closes it",
          pending_after_fail is True and bool(recovered) and closed_after is None),
    check("breaks: an unusable reply (local, malformed, unknown tier) closes the break", bad_calls == [1, 1, 1]),
    check("breaks: a paused router keeps the break open without calling it", pz_pending is True and pz_fails == 3),
    check("breaks: a corrupt state file counts as a break", bool(corrupt_routed)),
    check("breaks: ALWAYS any case is always; other on values mean breaks",
          modes == ["always", "breaks", "breaks", "breaks", None, None, None]),
    check("breaks: SessionStart writes nothing when the prompt line is off or always",
          (mp.mark_break({"MODEL_PICKER_STATE_DIR": str(cnt.parent)}, "s-off") or True)
          and not mp._session_file(nb, "prompt-breaks", "s-off").exists()
          and not mp._session_file(nb, "prompt-breaks", "s-always-mark").exists()),
    check("breaks: resume marks a break and keeps the idle clock", resumed == {"last": before_last, "pending": True}),
]

capture = Path(pst["MODEL_PICKER_STATE_DIR"]) / "router-saw.json"
spy = Path(pst["MODEL_PICKER_STATE_DIR"]) / "spy.py"
spy.write_text("import sys, json\nopen(%r, 'w').write(sys.stdin.read())\nprint(json.dumps({'tier': 'haiku', 'effort': 'low', 'confidence': 0.9}))\n" % str(capture))
prompt_hook("please look at this: " + "word " * 600 + "TAIL-MARKER", {**ON, "MODEL_PICKER_ROUTER": "command", "MODEL_PICKER_ROUTER_INPUT": "full",
                                                                  "MODEL_PICKER_ROUTER_CMD": f"{sys.executable} {spy}"})
saw = capture.read_text() if capture.exists() else ""
results.append(check("prompt route: only the 200-char head leaves the machine, even with ROUTER_INPUT=full",
                     bool(saw) and "TAIL-MARKER" not in saw and len(json.loads(saw)["task"]) < 300))
results += [
    check("floor: a list/number in the config is ignored, not a hook error",
          mp.decide({"description": "Review PR", "prompt": "p"}, {"MODEL_PICKER_FLOORS": [{"review": "opus"}]})["tier"] == "sonnet"
          and mp.decide({"description": "Review PR", "prompt": "p"}, {"MODEL_PICKER_FLOORS": 5})["tier"] == "sonnet"),
    check("floor: savings on floored launches go to the user's rule, not model-picker",
          ledger.attribute({"outcome": "explicit", "followed": True, "floor": "review", "chosen_tier": "opus"}) == "rule"
          and ledger.attribute({"outcome": "explicit", "followed": True, "chosen_tier": "haiku"}) == "model-picker"),
    check("floor: the decision record keeps which rule applied", dec.get("floor") == "review"),
]
import time as _time
t0 = _time.monotonic()
results += [
    check("floor: agent-type floors still apply to long descriptions",
          mp.decide({"description": "x " * 400, "prompt": "p", "subagent_type": "docs-writer"},
                    {"MODEL_PICKER_FLOORS": json.dumps({"docs-writer": "fable"})})["tier"] == "fable"),
    check("router command: timeout kills the whole process group",
          _raises(lambda: routers.run_shell("sleep 5; echo late", 0.3)) and _time.monotonic() - t0 < 2
          and subprocess.run(["pgrep", "-f", "sleep 5; echo late"], capture_output=True).returncode != 0),
]
plog = [r for r in ledger.read(pst) if r.get("kind") == "prompt"]
prep = ledger.report(pst, "all")
results += [
    check("prompt route: logged without the prompt text", bool(plog) and "SECRET" not in json.dumps(plog)),
    check("prompt route: report counts routed prompts", prep["prompts_routed"] == len(plog) and "Prompts routed" in ledger.format_report(prep)),
]

# --- v0.4.7 router pause ---------------------------------------------------------
import urllib.error  # noqa: E402

DOWN = {"MODEL_PICKER_ROUTER": "openai-decisions", "MODEL_PICKER_ROUTER_URL": "http://127.0.0.1:9",
        "MODEL_PICKER_ROUTER_MODEL": "team/jev-latest", "MODEL_PICKER_ROUTER_API_KEY_ENV": "none"}
bst = {**fresh_state(), **DOWN}
n1, n2, n3 = (mp.record_router(bst, None) for _ in range(3))
health = json.loads((Path(bst["MODEL_PICKER_STATE_DIR"]) / "router-health.json").read_text())  # raw file
results += [
    check("pause: first two failures are quiet, the third pauses with one notice",
          n1 is None and n2 is None and n3 and "jev-latest failed 3× in a row (timed out)" in n3 and "10 min" in n3),
    check("pause: router_paused reports the reason", mp.router_paused(bst) == "timed out"),
    check("pause: the shared state holds no router settings in clear", "127.0.0.1" not in json.dumps(health)),
    check("pause: a launch while paused skips the router at once",
          mp.decide({"description": "debug the flaky test"}, bst)["source"] == "heuristic (router paused: timed out)"),
    check("pause: other router settings start a fresh count",
          mp.router_paused({**bst, "MODEL_PICKER_ROUTER_MODEL": "team/jev-next"}) is None),
    check("pause: MODEL_PICKER_ROUTER_PAUSE=0 never pauses", mp.router_paused({**bst, "MODEL_PICKER_ROUTER_PAUSE": "0"}) is None),
]
later = time.time() + 601
results += [
    check("pause: ends after 10 minutes", mp.router_paused(bst, now=later) is None),
    check("pause: a failed retry after the pause pauses again, quietly",
          mp.record_router(bst, None, now=later) is None and mp.router_paused(bst, now=later + 1) == "timed out"),
    check("pause: one success clears the count",
          mp.record_router(bst, {"tier": "opus"}) is None and mp.router_paused(bst) is None
          and mp.record_router(bst, None) is None),
]
rst = {**fresh_state(), **DOWN}
e401 = urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None)
e503 = urllib.error.HTTPError("http://x", 503, "Unavailable", {}, None)
note401 = mp.record_router(rst, e401)
ost = {**fresh_state(), **DOWN}
results += [
    check("pause: a rejected request (HTTP 401) pauses at once and points at the config",
          note401 and "HTTP 401" in note401 and "retry won't fix" in note401 and mp.router_paused(rst) == "HTTP 401"),
    check("pause: HTTP 503 and connection errors count toward three",
          mp.record_router(ost, e503) is None and mp.record_router(ost, urllib.error.URLError(ConnectionRefusedError())) is None
          and "unreachable" in (mp.record_router(ost, urllib.error.URLError(OSError())) or "")),
]
gst = {**fresh_state(), **DOWN}
for _ in range(5):
    mp.record_router(gst, ValueError("no usable tier answer"))
results.append(check("pause: a malformed answer doesn't count (the router is up)", mp.router_paused(gst) is None))
(Path(gst["MODEL_PICKER_STATE_DIR"]) / "router-health.json").write_text("{not json")
results.append(check("pause: a corrupt state file means not paused", mp.router_paused(gst) is None
                     and mp.record_router(gst, None) is None))

# End to end: the prompt hook and the launch hook share one count.
est = {**fresh_state(), **DOWN, "MODEL_PICKER_PROMPT_ROUTE": "1"}
p1 = prompt_hook("debug the flaky login test in CI", est)
p2 = prompt_hook("debug the flaky login test in CI", est)
l3 = run(agent(description="debug the flaky login test", prompt="p"), "advise", est)
p4 = prompt_hook("debug the flaky login test in CI", est)
fails_after = mp._health_read(est).get("fails")
l5 = run(agent(description="debug the flaky login test", prompt="p"), "advise", est)
results += [
    check("pause e2e: prompts and launches share the count; the third failure tells the user",
          all((p or {}).get("systemMessage") == "model-picker: jev-latest unavailable (unreachable)" for p in (p1, p2))
          and "failed 3× in a row (unreachable)" in (l3 or {}).get("systemMessage", "")
          and "unavailable" not in (l3 or {}).get("systemMessage", "")),  # the notice replaces the label
    check("pause e2e: while paused a prompt is labeled and calls no router",
          (p4 or {}).get("systemMessage") == "model-picker: jev-latest unavailable (unreachable, paused)" and fails_after == 3),
    check("pause e2e: while paused a launch still gets a recommendation and a short label, no repeat notice",
          "router paused: unreachable" in json.dumps(l5) and (l5 or {}).get("systemMessage")
          == "model-picker: jev-latest unavailable (unreachable, paused); keyword rules used"),
]

# A launch whose router call fails gets a label before any pause starts; an unsure router (it answered) gets none.
lf = run(agent(description="debug the flaky login test", prompt="p"), "advise", {**fresh_state(), **DOWN})
lu = run(agent(description="debug the flaky login test", prompt="p"), "advise", {**fresh_state(), **FAKE_ROUTER("haiku", 0.2)})
results += [
    check("unavailable label: a launch that can't reach the router says so",
          (lf or {}).get("systemMessage") == "model-picker: jev-latest unavailable (unreachable); keyword rules used"),
    check("unavailable label: a router that answered unsure is not labeled", "unavailable" not in json.dumps(lu)),
]

# Review round 1 of v0.4.7.
import socket  # noqa: E402

def _paused_after(errs):
    st = {**fresh_state(), **DOWN}
    for e in errs:
        mp.record_router(st, e)
    return mp.router_paused(st)

results += [
    check("pause: real exception shapes count (TimeoutError, socket.timeout, TimeoutExpired, URLError(timeout))",
          _paused_after([TimeoutError(), socket.timeout(), subprocess.TimeoutExpired("x", 1)]) == "timed out"
          and _paused_after([urllib.error.URLError(TimeoutError())] * 3) == "connect timed out"),
    check("pause: a failing router/key command counts (a wrapped curl that can't connect exits non-zero)",
          _paused_after([subprocess.CalledProcessError(28, "curl")] * 3) == "command exit 28"),
    check("pause: no API key never pauses (it costs no time, and another session may have the key)",
          _paused_after([ValueError("no API key")] * 5) is None),
    check("pause: the key source and headers are part of the fingerprint",
          mp._router_id(DOWN) != mp._router_id({**DOWN, "MODEL_PICKER_ROUTER_API_KEY_ENV": "OTHER_KEY"})
          and mp._router_id(DOWN) != mp._router_id({**DOWN, "MODEL_PICKER_ROUTER_HEADERS": '{"a": "b"}'})),
    check("pause: an absurd MODEL_PICKER_ROUTER_PAUSE is capped at a day",
          mp._pause_seconds({"MODEL_PICKER_ROUTER_PAUSE": "inf"}) == 86400
          and mp._pause_seconds({"MODEL_PICKER_ROUTER_PAUSE": "nan"}) == 600
          and mp._pause_seconds({"MODEL_PICKER_ROUTER_PAUSE": "soon"}) == 600),
]
sst = {**fresh_state(), **DOWN}
for _ in range(3):
    mp.record_router(sst, None)
stale_now = time.time() + 600 + 600 + 5   # the pause ended more than one pause ago
results.append(check("pause: after a long-expired pause one blip doesn't re-pause; three do, with a new notice",
                     mp.record_router(sst, None, now=stale_now) is None and mp.router_paused(sst, now=stale_now + 1) is None
                     and mp.record_router(sst, None, now=stale_now) is None
                     and "3× in a row" in (mp.record_router(sst, None, now=stale_now) or "")))
cst = {**fresh_state(), **DOWN}
for _ in range(3):
    mp.record_router(cst, None)
results += [
    check("pause: a new reason (HTTP 401 after timeouts) gets its own notice",
          "HTTP 401" in (mp.record_router(cst, e401) or "")),
    check("pause: prompt timeouts pause only the prompt line",
          [mp.record_router(fst := {**fresh_state(), **DOWN}, None, scope="prompt") for _ in range(1)]
          and mp.record_router(fst, None, scope="prompt") is None
          and "prompt line is paused" in (mp.record_router(fst, None, scope="prompt") or "")
          and mp.router_paused(fst) is None and mp.router_paused(fst, scope="prompt") == "timed out"),
]
# Parallel hooks: one notice, no lost counts.
par = {**fresh_state(), **DOWN}
code = ("import json,sys; sys.path.insert(0, %r); import model_picker as mp; "
        "print(json.dumps(mp.record_router(json.loads(sys.argv[1]), None)))") % str(ROOT / "hooks")
procs = [subprocess.Popen([sys.executable, "-I", "-c", code, json.dumps(par)], stdout=subprocess.PIPE, text=True)
         for _ in range(6)]
outs = [json.loads(p.communicate()[0] or "null") for p in procs]
pfails = mp._health_read(par)["launch_fails"]
results.append(check("pause: 6 parallel failures count 6 and give exactly one notice",
                     pfails == 6 and sum(1 for o in outs if o) == 1))
# The route CLI (the /model-picker skill) honors a pause but never starts one: its notice would reach Claude, not you.
rcli = {**fresh_state(), **DOWN}
for _ in range(3):
    subprocess.run([sys.executable, "-I", str(HOOK), "route", "debug the flaky login test"], capture_output=True, text=True,
                   env={**{k: v for k, v in os.environ.items() if not k.startswith("MODEL_PICKER_")}, **rcli,
                        "MODEL_PICKER_CONFIG": "/nonexistent"})
results.append(check("pause: the route CLI doesn't count toward a pause",
                     not (Path(rcli["MODEL_PICKER_STATE_DIR"]) / "router-health.json").exists()))
# A paused launch really makes no call.
spst = fresh_state()
hit = Path(spst["MODEL_PICKER_STATE_DIR"]) / "called"
SPY = {**spst, "MODEL_PICKER_ROUTER": "command",
       "MODEL_PICKER_ROUTER_CMD": f"touch {hit}; {sys.executable} -c \"print('{{}}')\""}
for _ in range(3):
    mp.record_router(SPY, None)
hit.unlink(missing_ok=True)
paused_src = mp.decide({"description": "debug the flaky test"}, SPY)["source"]
results.append(check("pause: a launch while paused never calls the router",
                     paused_src.startswith("heuristic (router paused") and not hit.exists()))

# Review round 2 of v0.4.7.
pp = {**fresh_state(), "MODEL_PICKER_PROMPT_ROUTE": "1", "MODEL_PICKER_ROUTER": "command",
      "MODEL_PICKER_ROUTER_CMD": "sleep 2.7; echo '{\"tier\": \"haiku\", \"effort\": \"low\", \"confidence\": 1}'"}
slow_prompts = [prompt_hook("debug the flaky login test in CI", pp) for _ in range(3)]
saved_deadline, mp.ROUTER_DEADLINE = mp.ROUTER_DEADLINE, 7.0  # real launch budget (an earlier test lowers it)
launch_src = mp.decide({"description": "debug the flaky test"}, pp)["source"]
mp.ROUTER_DEADLINE = saved_deadline
cp = {**fresh_state(), **DOWN}
for _ in range(3):
    mp.record_router(cp, urllib.error.URLError(ConnectionRefusedError()), scope="prompt")
results += [
    check("pause: a router slower than the prompt budget (2.7 s) pauses only the prompt line; launches still use it",
          all((p or {}).get("systemMessage") == "model-picker: command router unavailable (timed out)"
              for p in slow_prompts[:2]) and "prompt line is paused" in (slow_prompts[2] or {}).get("systemMessage", "")
          and mp.router_paused(pp) is None and mp.router_paused(pp, scope="prompt") == "timed out"
          and launch_src == "command (1.00)"),
    check("pause: on the prompt path, failing to connect pauses everything", mp.router_paused(cp) == "unreachable"),
]
os.environ["KEY_FOR_TEST"] = "old-key"
KEYED = {**DOWN, "MODEL_PICKER_ROUTER_API_KEY_ENV": "KEY_FOR_TEST"}
before = mp._router_id(KEYED)
os.environ["KEY_FOR_TEST"] = "new-key"
results.append(check("pause: a different key value starts a fresh count", mp._router_id(KEYED) != before))
del os.environ["KEY_FOR_TEST"]
ost2 = {**fresh_state(), **DOWN}
mp.record_router(ost2, None, now=time.time() - 700)
mp.record_router(ost2, None, now=time.time() - 690)
results.append(check("pause: failures from long ago (never paused) expire", mp.record_router(ost2, None) is None
                     and mp._health_read(ost2)["launch_fails"] == 1))
fut = {**fresh_state(), **DOWN}
mp._health_write(fut, {"fails": 3, "reason": "timed out", "paused_until": time.time() + 10 ** 9})
results.append(check("pause: a pause ending implausibly far ahead is ignored", mp.router_paused(fut) is None))
alt = {**fresh_state(), **DOWN}
for _ in range(3):
    mp.record_router(alt, None)
results.append(check("pause: 'unreachable' right after a 'timed out' pause re-pauses quietly",
                     mp.record_router(alt, urllib.error.URLError(OSError()), now=time.time() + 601) is None
                     and mp.router_paused(alt, now=time.time() + 602) == "unreachable"))

# Review round 3 of v0.4.7.
two = {**fresh_state(), **DOWN}
os.environ["KEY_A"], os.environ["KEY_B"] = "a", "b"
ka, kb = {**two, "MODEL_PICKER_ROUTER_API_KEY_ENV": "KEY_A"}, {**two, "MODEL_PICKER_ROUTER_API_KEY_ENV": "KEY_B"}
for _ in range(3):
    mp.record_router(ka, urllib.error.URLError(OSError()))
    mp.record_router(kb, urllib.error.URLError(OSError()))
results.append(check("pause: sessions with different keys keep separate counts in one file",
                     mp.router_paused(ka) == "unreachable" and mp.router_paused(kb) == "unreachable"))
old = {**fresh_state(), **DOWN}
mp.record_router({**old, "MODEL_PICKER_ROUTER_MODEL": "gone"}, None, now=time.time() - 2 * 86400)
mp.record_router(old, None)
results.append(check("pause: entries idle for a day are pruned",
                     list(json.loads((Path(old["MODEL_PICKER_STATE_DIR"]) / "router-health.json").read_text())["routers"])
                     == [mp._router_id(old)]))
ps = {**fresh_state(), **DOWN}
for _ in range(3):
    mp.record_router(ps, None, scope="prompt")
mp.record_router(ps, {"tier": "opus"})  # a launch answered (it had more time)
results += [
    check("pause: a launch success doesn't lift the prompt line's pause", mp.router_paused(ps, scope="prompt") == "timed out"),
    check("pause: a prompt success clears both counts",
          mp.record_router(ps, {"tier": "haiku"}, scope="prompt") is None and mp.router_paused(ps, scope="prompt") is None),
]
import unittest.mock as um  # noqa: E402
nl = {**fresh_state(), **DOWN}
with um.patch.object(mp.fcntl, "flock", side_effect=OSError(45, "Operation not supported")):
    t0 = time.monotonic()
    for _ in range(3):
        mp.record_router(nl, None)
    nolock_s = time.monotonic() - t0
results.append(check("pause: works without flock (network filesystems), with no lock wait",
                     mp.router_paused(nl) == "timed out" and nolock_s < 0.5))

# v0.4.8: follow-ups from the v0.4.7 review.
lt = {**fresh_state(), **DOWN}
for _ in range(3):
    mp.record_router(lt, None)                                   # a launch times out
    mp.record_router(lt, {"tier": "haiku"}, scope="prompt")      # a short prompt answers in time
lc = {**fresh_state(), **DOWN}
mp.record_router(lc, urllib.error.URLError(OSError()))
mp.record_router(lc, {"tier": "haiku"}, scope="prompt")
results += [
    check("pause: prompt successes don't reset launch timeouts (3 launch timeouts still pause)",
          mp.router_paused(lt) == "timed out"),
    check("pause: a prompt success still clears connection failures", mp._health_read(lc).get("fails") is None),
]
mx = {**fresh_state(), **DOWN}
mixed = [mp.record_router(mx, e, scope="prompt") for e in (None, urllib.error.URLError(TimeoutError()), None)]
cn = {**fresh_state(), **DOWN}
conn = [mp.record_router(cn, urllib.error.URLError(OSError()), scope="prompt") for _ in range(3)]
results += [
    check("pause: mixed prompt failures (timeout, connect timeout, timeout) pause the prompt line on the 3rd",
          mixed[:2] == [None, None] and "prompt line is paused" in (mixed[2] or "")
          and mp.router_paused(mx, scope="prompt") and mp.router_paused(mx) is None),
    check("pause: 3 prompt connection failures pause everything, with one notice (not two)",
          conn[:2] == [None, None] and "router calls paused" in (conn[2] or "") and "prompt line" not in conn[2]
          and mp.router_paused(cn) == "unreachable"),
]

# v0.4.8 review: no false "3× in a row" from mixing old launch timeouts with later blips.
t0 = time.time()
sa = {**fresh_state(), **DOWN}
mp.record_router(sa, None, now=t0)                                                   # 1 launch timeout
mp.record_router(sa, urllib.error.URLError(OSError()), now=t0 + 300, scope="prompt")  # a prompt blip
for k in range(30):
    mp.record_router(sa, {"tier": "haiku"}, now=t0 + 310 + k, scope="prompt")         # prompts answer
blip = mp.record_router(sa, urllib.error.URLError(OSError()), now=t0 + 840, scope="prompt")
sb = {**fresh_state(), **DOWN}
for k in range(3):
    mp.record_router(sb, None, now=t0 + k)                                           # launches time out: paused
mp.record_router(sb, {"tier": "haiku"}, now=t0 + 610, scope="prompt")
blip_b = mp.record_router(sb, urllib.error.URLError(OSError()), now=t0 + 700, scope="prompt")
lm = {**fresh_state(), **DOWN}
lmix = [mp.record_router(lm, e) for e in (None, urllib.error.URLError(OSError()), None)]
lcn = {**fresh_state(), **DOWN}
lconn = [mp.record_router(lcn, urllib.error.URLError(OSError())) for _ in range(3)]
results += [
    check("pause: one old launch timeout + two later blips isn't '3 in a row'",
          blip is None and mp.router_paused(sa, now=t0 + 841) is None
          and mp.router_paused(sa, now=t0 + 841, scope="prompt") is None),
    check("pause: after a launch-timeout pause, one prompt blip pauses nothing",
          blip_b is None and mp.router_paused(sb, now=t0 + 701) is None
          and mp.router_paused(sb, now=t0 + 701, scope="prompt") is None),
    check("pause: mixed launch failures pause launches on the 3rd; the prompt line keeps asking",
          lmix[:2] == [None, None] and "on agent launches" in (lmix[2] or "")
          and mp.router_paused(lm) and mp.router_paused(lm, scope="prompt") is None),
    check("pause: 3 launch connection failures pause everything, with one notice",
          lconn[:2] == [None, None] and "router calls paused" in (lconn[2] or "")
          and mp.router_paused(lcn, scope="prompt") == "unreachable"),
]

# v0.4.8: the stamp shows the effort the agent ran at and whether it followed the recommendation.
JEV = "openai-decisions:team/jev-latest (0.88)"
dec = lambda **k: {"rec_tier": "opus", "rec_effort": "high", "source": JEV, "chosen_tier": "opus",  # noqa: E731
                   "outcome": "explicit", **k}
results += [
    check("stamp verdict: followed", ledger.stamp_verdict(dec(), "high") == " · ✓ followed jev-latest (opus/high)"),
    check("stamp verdict: model followed, effort differs",
          ledger.stamp_verdict(dec(), "low") == " · ✓ model as jev-latest recommended (opus/high), effort differs"),
    check("stamp verdict: not followed",
          ledger.stamp_verdict(dec(chosen_tier="sonnet"), "high") == " · ✗ jev-latest recommended opus/high"),
    check("stamp verdict: keyword-rule fallback and floors are named as such",
          ledger.stamp_verdict(dec(source="heuristic (router paused: unreachable)", unsure=True), None)
          == " · keyword rules only guessed opus"
          and "jev-latest + your floor" in ledger.stamp_verdict(dec(source=JEV + " + floor", floor="review"), "high")),
    check("stamp verdict: nothing to compare (pinned agent, no launch record)",
          ledger.stamp_verdict({"outcome": "pinned"}, "high") == "" and ledger.stamp_verdict(None, "high") == ""),
    check("stamp: run effort is the most common recorded effort",
          ledger.run_effort([{"effort": "high"}, {"effort": "high"}, {"effort": "low"}, {}]) == "high"
          and ledger.run_effort([{}]) is None),
]
with tempfile.TemporaryDirectory() as d:
    sess = Path(d) / "sess.jsonl"
    write_transcript(sess, [amsg("m0", "claude-opus-5-5", inp=10, out=10)])
    sub_dir = Path(d) / "sess" / "subagents"
    write_transcript(sub_dir / "agent-ef.jsonl", [{**amsg(f"e{i}", "claude-opus-5-5", inp=1000, out=200), "effort": "high"}
                                                  for i in range(2)])
    (sub_dir / "agent-ef.meta.json").write_text(json.dumps(
        {"agentType": "general-purpose", "toolUseId": "toolu_ef", "requestShape": "foreground"}))
    est2 = {**fresh_state(), "MODEL_PICKER_STAMP": "1"}
    ledger.append(est2, {"kind": "decision", "tool_use_id": "toolu_ef", "outcome": "explicit", "chosen": "opus",
                         "chosen_tier": "opus", "rec_tier": "opus", "rec_effort": "high", "source": JEV,
                         "followed": True, "chosen_effort": "high"})
    r = subprocess.run([sys.executable, "-I", str(HOOK), "posttool", str(ROOT)], capture_output=True, text=True,
                       input=json.dumps({"tool_name": "Agent", "tool_use_id": "toolu_ef", "transcript_path": str(sess)}),
                       env={**os.environ, **est2})
    stamp = json.loads(r.stdout).get("systemMessage", "") if r.stdout.strip() else ""
    results.append(check("stamp e2e: effort and verdict in the line",
                         "general-purpose ran on claude-opus-5-5 at high effort" in stamp
                         and stamp.endswith("· ✓ followed jev-latest (opus/high)")))
st0 = fresh_state()
run(agent(description="find x", prompt="p", model="haiku", effort="low"), "advise", st0)
results.append(check("decision log: the launch's effort is recorded",
                     any(r.get("chosen_effort") == "low" for r in ledger.read(st0))))

both = {**fresh_state(), **DOWN}
first = [mp.record_router(both, None, scope="prompt") for _ in range(3)][-1]
second = [mp.record_router(both, None) for _ in range(3)][-1]
results.append(check("pause notice: says when the other kind of call is already paused",
                     "agent launches still ask it" in (first or "") and "the prompt line is paused too" in (second or "")))

results += [
    check("stamp verdict: a launch with no model isn't judged (it inherited the session model)",
          ledger.stamp_verdict(dec(outcome="missing"), "high") == " · no model set; jev-latest recommended opus/high"),
    check("stamp verdict: unknown chosen model with a confident router isn't called a guess",
          ledger.stamp_verdict(dec(chosen_tier=None), "high") == " · jev-latest recommended opus/high"),
    check("stamp verdict: keyword-rule effort (always medium) isn't compared",
          ledger.stamp_verdict(dec(source="heuristic (router timed out)", rec_effort="medium"), "high")
          == " · ✓ followed keyword rules (opus)"),
    check("stamp label: model ids with ':' and the command router",
          ledger._rec_label("openai-decisions:ft:gpt-4.1-mini:acme::abc123 (0.9)") == "ft:gpt-4.1-mini:acme::abc123"
          and ledger._rec_label("command (1.00)") == "command router"),
]

# v0.4.9: "you asked for" a model or effort in the prompt.
da = mp.detect_ask
results += [
    check("ask: model named with a verb", da("run the search on haiku please") == {"tier": "haiku", "effort": None}),
    check("ask: model + effort", da("use opus at high effort for the review") == {"tier": "opus", "effort": "high"}
          and da("Use Claude Sonnet agents, effort: low") == {"tier": "sonnet", "effort": "low"}),
    check("ask: '<model> agent' and versions", da("spawn an opus agent for this") == {"tier": "opus", "effort": None}
          and da("try sonnet 5.5 for the docs") == {"tier": "sonnet", "effort": None}),
    check("ask: negation is not a pick", da("don't use opus for this") is None and da("use sonnet instead of opus") == {"tier": "sonnet", "effort": None}),
    check("ask: two models named means no pick", da("compare running on haiku vs running on opus") is None),
    check("ask: mentions without intent are ignored",
          da("Opus is expensive and haiku is cheap") is None and da("write a haiku about spring") is None and da("write a haiku for my mom") is None),
]
ast = {**fresh_state(), "MODEL_PICKER_PROMPT_ROUTE": "1", **FAKE_ROUTER("sonnet", 0.82)}
seen_line = prompt_hook("please use opus for the code review", ast, session="s-prompt")
asked_now = mp.current_ask(ast, "s-prompt")
notif = prompt_hook("<task-notification>agent done</task-notification>", ast, session="s-prompt")
kept = mp.current_ask(ast, "s-prompt")
prompt_hook("thanks, now fix the failing test", ast, session="s-prompt")
cleared = mp.current_ask(ast, "s-prompt")
results += [
    check("ask: the prompt line says what you asked for",
          "→ sonnet/medium" in (seen_line or {}).get("systemMessage", "") and seen_line["systemMessage"].endswith("· you asked for opus")),
    check("ask: remembered for the session; notifications keep it; the next prompt replaces it",
          asked_now == {"tier": "opus", "effort": None} and notif is None and kept == asked_now and cleared is None),
]
# Launches against the remembered ask.
lst = {**fresh_state(), **FAKE_ROUTER("haiku", 0.9), "MODEL_PICKER_ROUTER_ON_EXPLICIT": "1"}
mp.remember_ask(lst, "S1", {"tier": "opus", "effort": "high"})
def launch_as(**inp):
    return run({**agent(**inp), "session_id": "S1", "tool_use_id": f"t-{len(inp)}-{inp.get('model')}-{inp.get('effort')}"},
               "advise", lst)
ok_launch = launch_as(description="review the PR", prompt="p", model="opus", effort="high")
off_launch = launch_as(description="review the PR", prompt="p", model="haiku")
none_launch = launch_as(description="review the PR", prompt="p")
decs_ask = [r for r in ledger.read(lst) if r.get("kind") == "decision"]
honored_rec = next((r for r in decs_ask if r.get("chosen_tier") == "opus"), {})
rep_ask = ledger.report(lst, "all")
results += [
    check("ask: a launch on the asked model and effort gets no advice and is logged as your pick",
          (ok_launch or {}).get("hookSpecificOutput") is None and honored_rec.get("user_pick") == {"tier": "opus", "effort": "high"}),
    check("ask: labels only: a launch on another model isn't steered toward the ask",
          "asked" not in json.dumps(off_launch) and "opus" not in json.dumps(off_launch)),
    check("ask: labels only: a no-model launch gets the router's recommendation, not the ask",
          "`haiku`" in json.dumps(none_launch) and "asked" not in json.dumps(none_launch)),
    check("ask: your picks leave the follow rate and are counted separately",
          rep_ask["user_picks"] == 1 and rep_ask["compared"] == 1 and "1 more used the model you asked for" in ledger.format_report(rep_ask)),
    check("ask: savings on your picks are yours, not model-picker's",
          ledger.attribute({**honored_rec}) == "user"),
    check("ask: the stamp says it was your call and what the router said",
          ledger.stamp_verdict(honored_rec, "high").startswith(" · your call (opus/high effort); command router said haiku")),
]

# v0.4.9 review: false positives from realistic prompts.
NOT_ASKS = [
    "is it running on opus?", "why did the subagent run on opus instead of haiku?", "I'm on opus right now and it's slow",
    "the opus model is too pricey", "try a haiku for the release notes", "use a haiku as the epigraph",
    "pick a sonnet from the anthology", "fix the bug with fable characters", "use the opus agent definition",
    "a low-effort fix", "this is a high effort change", "put max effort into this", "the effort to high-quality docs",
    "model-picker: `Explore` launched on opus, but the task reads as `haiku`, effort low.",
    "[model-picker] general-purpose ran on claude-opus-5-5 · your call (opus/high effort)",
    "see this:\n```\nclaude --model opus -p 'run on haiku'\n```",
    "> use opus for everything\nwhat do you think of that quote",
]
misfires = [p for p in NOT_ASKS if da(p) is not None]
results += [
    check(f"ask: no pick from questions, poems, prose, pasted output and code ({len(NOT_ASKS)} cases)", not misfires),
    check("ask: negation stays in its clause",
          da("no, run it on haiku") == {"tier": "haiku", "effort": None}
          and da("Don't use opus; use sonnet") == {"tier": "sonnet", "effort": None}),
    check("ask: effort forms", da("run it with high effort") == {"tier": None, "effort": "high"}
          and da("effort: low") == {"tier": None, "effort": "low"} and da("set effort to max") == {"tier": None, "effort": "max"}),
]
# Effort-only asks never mark a launch as the user's pick.
eo = {**fresh_state(), **FAKE_ROUTER("haiku", 0.9), "MODEL_PICKER_ROUTER_ON_EXPLICIT": "1"}
mp.remember_ask(eo, "S2", {"tier": None, "effort": "low"})
eo_out = run({**agent(description="find the config file", prompt="p", model="fable", effort="low"), "session_id": "S2",
              "tool_use_id": "t-eo"}, "advise", eo)
eo_dec = next((r for r in ledger.read(eo) if r.get("kind") == "decision"), {})
results.append(check("ask: an effort-only ask is not a model pick (normal verdict and advice stay)",
                     not eo_dec.get("user_pick") and "reads as" in json.dumps(eo_out)))
# Clearing: slash commands and pasted markup clear the ask; notifications keep it; MODEL_PICKER_ASK=0 and LOG=0 turn it off.
cl = {**fresh_state(), "MODEL_PICKER_PROMPT_ROUTE": "1", **FAKE_ROUTER("sonnet", 0.82)}
prompt_hook("use opus for the review please", cl, session="s-prompt")
prompt_hook("/code-review high", cl, session="s-prompt")
after_slash = mp.current_ask(cl, "s-prompt")
prompt_hook("use opus for the review please", cl, session="s-prompt")
prompt_hook("<div>pasted html here</div>", cl, session="s-prompt")
after_html = mp.current_ask(cl, "s-prompt")
off_ask = {**fresh_state(), "MODEL_PICKER_ASK": "0"}
mp.remember_ask(off_ask, "S3", {"tier": "opus", "effort": None})
nolog = {**fresh_state(), "MODEL_PICKER_LOG": "0"}
mp.remember_ask(nolog, "S3", {"tier": "opus", "effort": None})
short = prompt_hook("use opus", {**fresh_state(), "MODEL_PICKER_PROMPT_ROUTE": "1", **FAKE_ROUTER("sonnet", 0.82)})
results += [
    check("ask: slash commands and pasted markup clear the ask", after_slash is None and after_html is None),
    check("ask: MODEL_PICKER_ASK=0 and MODEL_PICKER_LOG=0 store nothing",
          mp.current_ask(off_ask, "S3") is None and not (Path(off_ask["MODEL_PICKER_STATE_DIR"]) / "asked").exists()
          and mp.current_ask(nolog, "S3") is None),
    check("ask: no bare 'you asked for' line when the router didn't answer (2-word prompt)", short is None),
]

# v0.4.9 re-review: negation forms, comparisons, hyphens, pasted output, quotes, punctuation, honored effort.
NOT_ASKS2 = ["Don’t use opus for this.", "You can't use opus here, run it", "We won't use opus for this one.",
             "Shouldn't run on opus.", "use haiku or sonnet, whichever is cheaper", "use haiku vs opus",
             "Run haiku and opus side by side to compare", "Do a quick pass on haiku-level complexity bugs",
             "  ⎿  [model-picker] Explore ran on claude-opus-5-5 at high effort", "Write ‘use opus for reviews’ in the README",
             "Explain this:\n```\nuse opus\n" + "x" * 5000, "run on opus？", "Run on opus, e.g. for the review?",
             "use the opus agent's definition"]
results += [
    check(f"ask: re-review cases give no pick ({len(NOT_ASKS2)} cases)", not [p for p in NOT_ASKS2 if da(p)]),
    check("ask: version forms still count", da("use the haiku-5-5 model") == {"tier": "haiku", "effort": None}
          and da("use Opus 5.5 for the migration") == {"tier": "opus", "effort": None}),
]
hp = {**fresh_state(), **FAKE_ROUTER("haiku", 0.9), "MODEL_PICKER_ROUTER_ON_EXPLICIT": "1"}
mp.remember_ask(hp, "S4", {"tier": "opus", "effort": "high"})
run({**agent(description="review the PR", prompt="p", model="opus"), "session_id": "S4", "tool_use_id": "t-hp"}, "advise", hp)
hp_dec = next((r for r in ledger.read(hp) if r.get("kind") == "decision"), {})
off2 = {**fresh_state(), "MODEL_PICKER_ASK": "0", "MODEL_PICKER_PROMPT_ROUTE": "1", **FAKE_ROUTER("sonnet", 0.82)}
results += [
    check("ask: a launch without the asked effort records only the model as honored",
          hp_dec.get("user_pick") == {"tier": "opus", "effort": None}),
    check("ask: MODEL_PICKER_ASK=0 also drops the prompt-line suffix",
          "you asked" not in (prompt_hook("please use opus for the code review", off2) or {}).get("systemMessage", "")),
]

# v0.4.10: floors can set a minimum effort ("opus/high").
FE = {"MODEL_PICKER_FLOORS": json.dumps({"review": "opus/high", "docs": "sonnet", "bad": "opus/extreme"})}
base_rec = {"tier": "opus", "effort": "medium", "reason": "r", "source": "x", "unsure": False}
r_eff = mp.apply_floors(base_rec, {"description": "Review PR 60"}, FE)
r_both = mp.apply_floors({**base_rec, "tier": "haiku", "effort": "low"}, {"description": "review the diff"}, FE)
r_keep = mp.apply_floors({**base_rec, "effort": "high"}, {"description": "review the diff"}, {"MODEL_PICKER_FLOORS": '{"review": "opus/medium"}'})
results += [
    check("floor effort: parsed; an unknown effort level skips the entry",
          [(f[1], f[2], f[3]) for f in mp._floors(FE)] == [("review", "opus", "high"), ("docs", "sonnet", None)]),
    check("floor effort: raises effort alone when the model already meets the floor (not credited as the rule's model)",
          r_eff["tier"] == "opus" and r_eff["effort"] == "high" and not r_eff.get("floor")
          and r_eff["floor_effort"] == "high" and "effort raised to high by your rule \"review\"" in r_eff["reason"]),
    check("floor effort: raises model and effort together", (r_both["tier"], r_both["effort"]) == ("opus", "high")),
    check("floor effort: never lowers effort", r_keep["effort"] == "high" and not r_keep.get("floor")),
]
fst2 = {**fresh_state(), **FE}
below = run(agent(description="Review PR 60 for bugs", prompt="p", model="opus"), "advise", fst2)
at = run(agent(description="Review PR 60 for bugs", prompt="p", model="opus", effort="high"), "advise", fst2)
results += [
    check("floor effort: a launch under the floor (no effort set) is reminded of the rule",
          "matches your MODEL_PICKER_FLOORS rule (at least opus/high effort)" in json.dumps(below)
          and "with no effort set" in json.dumps(below)),
    check("floor effort: a launch that meets the floor gets no reminder", "MODEL_PICKER_FLOORS" not in json.dumps(at)),
]

# v0.4.10 review: fitted floors, stronger tiers meet effort floors, labels, stamp effort.
nf = {"MODEL_PICKER_TIERS": "haiku,sonnet,opus", "MODEL_PICKER_FLOORS": '{"review": "fable/max"}'}
fit_rec = mp.decide({"description": "review the auth change"}, nf, use_router=False)
results += [
    check("floor: an unavailable floor tier steps to an available one, keeping a max effort",
          (fit_rec["tier"], fit_rec["effort"]) == ("opus", "max") and fit_rec["floor_min"] == "opus/max"),
    check("floor: no reminder demands an unavailable tier",
          mp._below_floor(fit_rec, "opus", "max") is None and mp._below_floor(fit_rec, "opus", "high") == "opus/max effort"),
]
sh = {"MODEL_PICKER_FLOORS": '{"review": "sonnet/high"}'}
strong = mp.apply_floors({**base_rec, "tier": "fable", "effort": "low"}, {"description": "review"}, sh)
results += [
    check("floor: a stronger model meets an effort floor at any effort",
          strong["effort"] == "low" and not strong.get("floor_effort")
          and mp._below_floor({"floor_min": "sonnet/high"}, "opus", "medium") is None
          and mp._below_floor({"floor_min": "sonnet/high"}, "sonnet", "medium") == "sonnet/high effort"),
]
mix = mp.apply_floors({**base_rec, "tier": "haiku", "effort": "low"}, {"description": "review the docs and tests"},
                      {"MODEL_PICKER_FLOORS": json.dumps({"review": "opus", "docs": "sonnet/high", "test": "haiku"})})
results += [
    check("floor: the reason names only the rules that set the minimum",
          mix["tier"] == "opus" and mix["floor"] == "review" and "docs" not in mix["reason"] and "test" not in mix["reason"]),
    check("stamp: an effort set by a floor is compared even on keyword-rule recommendations",
          ledger.stamp_verdict({"rec_tier": "opus", "rec_effort": "high", "source": "heuristic + floor", "chosen_tier": "opus",
                                "outcome": "explicit", "floor_effort": "high"}, "medium").endswith("effort differs")),
]

# v0.4.10 re-review.
xf = {"MODEL_PICKER_TIERS": "haiku,sonnet,opus", "MODEL_PICKER_FLOORS": '{"review": "opus/xhigh"}'}
xr = mp.decide({"description": "plan the architecture for a major refactor, review it"}, xf, use_router=False)
both = mp.apply_floors({**base_rec, "tier": "sonnet", "effort": "medium"}, {"description": "review"}, {"MODEL_PICKER_FLOORS": '{"review": "opus/high"}'})
results += [
    check("stamp: a router verdict with unknown effort doesn't claim 'effort differs'",
          ledger.stamp_verdict(dec(), None) == " · ✓ followed jev-latest (opus/high)"),
    check("floor: an unavailable stronger pick is fitted before the floor applies",
          (xr["tier"], xr["effort"]) == ("opus", "xhigh") and mp._below_floor(xr, "opus", "xhigh") is None),
    check("floor: raising model and effort records the effort too, and the stamp compares it",
          both.get("floor") == "review" and both.get("floor_effort") == "high"
          and ledger.stamp_verdict({"rec_tier": "opus", "rec_effort": "high", "source": "heuristic + floor", "chosen_tier": "opus",
                                    "outcome": "explicit", "floor": "review", "floor_effort": "high"}, None).endswith("effort differs")),
    check("stamp: an effort-only floor is named", "+ your floor" in ledger.stamp_verdict(
          {"rec_tier": "sonnet", "rec_effort": "high", "source": "heuristic + floor", "chosen_tier": "opus",
           "outcome": "explicit", "floor_effort": "high"}, "medium")),
]

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
