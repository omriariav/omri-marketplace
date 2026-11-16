"""Run: python3 -I plugins/model-picker/tests/test_model_picker.py"""

import json
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
TOOL_WORK = [  # (description, prompt): must never stay local
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
TEXT_WORK = [  # must stay local
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
    check("launch path: router not even offered local",
          "local" not in mp.decide({"description": "d", "prompt": "p"}, {**ALWAYS_LOCAL}, for_launch=True)["tier"]),
    check("skill path: shaky local call falls back with honest metadata",
          (lambda r: r["tier"] != "local" and r["source"].startswith("heuristic (local vetoed") and r["confidence"] is None)(
              mp.guard_local({**LV, "confidence": 0.6}, {"description": "Summarize", "prompt": "Summarize: x"}, {}))),
    check("local guard: non-local recs untouched", mp.guard_local({**LV, "tier": "sonnet"}, {"prompt": "x"}, {})["tier"] == "sonnet"),
]

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
