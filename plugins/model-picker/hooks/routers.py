"""Optional external routers that pick a model tier and effort for a task.

Every router returns {"tier", "effort", "confidence", "source"} or raises; the
caller falls back to the keyword heuristic on any error or low confidence.
Stdlib only (urllib), so the hook keeps zero dependencies.
"""

import json
import os
import signal
import subprocess
import tempfile
import urllib.request

EFFORTS = ["low", "medium", "high"]

TIER_CRITERIA = {
    "local": "Pure text work needing no tools or files: summarize, classify, tag, extract, translate given text.",
    "haiku": "Cheap side work that is easy to verify: file search, grep, reading logs or test output, simple fetch-and-report.",
    "sonnet": "Well-scoped coding (bug fix, feature iteration, spec-driven), review, drafting, docs, repeated agent tasks.",
    "opus": "Complex engineering and judgment: debugging, multi-PR features, migrations, performance work, ambiguous tasks.",
    "fable": "Hardest open-ended work: major refactors or migrations, architecture, deep research, leading many agents, long unattended runs.",
}
EFFORT_CRITERIA = {
    "low": "Routine and easy to check; speed matters more than depth.",
    "medium": "Typical task; some reasoning and tool use.",
    "high": "Subtle, multi-step, or costly to get wrong; needs self-checking.",
}
INSTRUCTIONS = {
    "tier": "Which model tier is the cheapest one that will reliably complete this AI-agent task?",
    "effort": "How much reasoning effort does this AI-agent task need?",
}


def _post(url, body, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _confidence(answer, choice):
    """Answer confidence; else the chosen option's probability; else 1.0 (the provider gave no score)."""
    if isinstance(answer.get("confidence"), (int, float)):
        return float(answer["confidence"])
    probs = answer.get("probabilities")
    if isinstance(probs, dict) and isinstance(probs.get(choice), (int, float)):
        return float(probs[choice])
    if isinstance(probs, list):
        for p in probs:
            if isinstance(p, dict) and p.get("value") == choice and isinstance(p.get("probability"), (int, float)):
                return float(p["probability"])
    return 1.0


def _result(tier, tier_conf, effort, source):
    return {"tier": tier, "effort": effort if effort in EFFORTS else "medium",
            "confidence": float(tier_conf), "source": source}


def openai_decisions(task, tiers, cfg):
    """OpenAI Decisions format: POST {base}/v1/decisions. Also served by LiteLLM gateways."""
    base = (cfg.get("MODEL_PICKER_ROUTER_URL") or "https://api.openai.com").rstrip("/")
    model = cfg.get("MODEL_PICKER_ROUTER_MODEL") or "gpt-6-luna"
    body = {
        "model": model,
        "input": task,
        "questions": [
            {"type": "choice", "name": "tier", "instructions": INSTRUCTIONS["tier"],
             "choices": [{"value": t, "description": TIER_CRITERIA[t]} for t in tiers]},
            {"type": "choice", "name": "effort", "instructions": INSTRUCTIONS["effort"],
             "choices": [{"value": e, "description": EFFORT_CRITERIA[e]} for e in EFFORTS]},
        ],
    }
    if cfg.get("_metadata"):
        body["metadata"] = cfg["_metadata"]
    auth = {"Authorization": f"Bearer {cfg['_api_key']}"} if cfg.get("_api_key") else {}
    headers = {**auth, **cfg.get("_headers", {})}
    data = _post(f"{base}/v1/decisions", body, headers, cfg["_timeout"])
    answers = {a.get("name"): a for a in data.get("answers", []) if isinstance(a, dict)}
    tier = answers.get("tier", {})
    if tier.get("type") != "choice" or tier.get("choice") not in tiers:
        raise ValueError(f"no usable tier answer ({tier.get('type')})")
    return _result(tier["choice"], _confidence(tier, tier["choice"]), answers.get("effort", {}).get("choice"),
                   f"openai-decisions:{model}")


# Yes/no questions instead of one "which tier?" question. Decision models answer narrow questions
# better than one broad choice; plain code then maps the answers to a tier and effort, so every rule
# can be read, tested and backtested. Asked in one request, so latency matches a single question.
QUESTIONS = {
    "simple": "Is this a simple, mechanical task (copy data, upload a file, make one API call, look something up) "
              "that someone could do by following clear steps without judgment?",
    "readonly": "Does the agent only search, read or summarize material and report back, without writing code, "
                "documents or decisions?",
    "review": "Is the task to review or verify someone else's work (code, a document, edits) before it is accepted?",
    "judgment": "Does the task need expert judgment on long or specialized material (legal, compliance, financial, "
                "architecture, product strategy)?",
    "large": "Is the work large or open-ended: many components, a multi-part feature, a broad investigation, "
             "or a failure whose cause is unknown?",
    "lead": "Will the agent coordinate or delegate to other agents or helpers?",
    "visual": "Must the agent render its output and check it by eye (slides, diagrams, screenshots of a UI)?",
    "stakes": "Is a mistake both costly and hard to notice: it gates a merge or release, or people act on the "
              "result without re-checking it?",
    "autocheck": "Does the task include writing or running automated tests or a script that checks the result?",
    "synth": "Must the agent combine several sources into one synthesis, comparison or brief that someone will use "
             "to decide or act?",
}


def tier_from_answers(yes):
    """Map yes/no answers ({name: bool}) to (tier, effort), following the guide's task table and effort
    rules. Never fable: no question singles out fable-level work, so it stays a stronger-model call for
    the caller. Haiku only for side work that is cheap to verify (no costly mistake, no expert judgment)."""
    def y(k):
        return bool(yes.get(k))
    if y("lead") or y("visual"):
        tier = "opus"                                   # leading agents; render-and-check loops
    elif y("review") and (y("large") or y("judgment")):
        tier = "opus"                                   # reviews across many parts or specialized material
    elif y("judgment") and y("large"):
        tier = "opus"                                   # large-source or specialized documents
    elif y("large") and not y("readonly"):
        tier = "opus"                                   # multi-part features, unknown failures
    elif y("review") or y("stakes") or y("judgment"):
        tier = "sonnet"                                 # not cheap to verify: never haiku
    elif (y("simple") or (y("readonly") and not y("large"))) and not y("synth"):
        tier = "haiku"                                  # a brief someone acts on is not side work
    else:
        tier = "sonnet"
    if tier == "haiku":
        effort = "low"
    elif y("stakes") and not y("autocheck"):
        effort = "high"                                 # costly to get wrong and no automatic check
    else:
        effort = "medium"
    return tier, effort


def openai_questions(task, tiers, cfg):
    """OpenAI Decisions format, asked as yes/no QUESTIONS; tier and effort come from tier_from_answers."""
    base = (cfg.get("MODEL_PICKER_ROUTER_URL") or "https://api.openai.com").rstrip("/")
    model = cfg.get("MODEL_PICKER_ROUTER_MODEL") or "gpt-6-luna"
    yes_no = [{"value": "yes", "description": "yes"}, {"value": "no", "description": "no"}]
    body = {"model": model, "input": task,
            "questions": [{"type": "choice", "name": n, "instructions": q, "choices": yes_no}
                          for n, q in QUESTIONS.items()]}
    if cfg.get("_metadata"):
        body["metadata"] = cfg["_metadata"]
    auth = {"Authorization": f"Bearer {cfg['_api_key']}"} if cfg.get("_api_key") else {}
    data = _post(f"{base}/v1/decisions", body, {**auth, **cfg.get("_headers", {})}, cfg["_timeout"])
    answers = {a.get("name"): a for a in data.get("answers", []) if isinstance(a, dict)}
    yes, confs = {}, []
    for name in QUESTIONS:
        a = answers.get(name) or {}
        if a.get("choice") not in ("yes", "no"):
            raise ValueError(f"no usable answer to {name!r}")
        yes[name] = a["choice"] == "yes"
        confs.append(_confidence(a, a["choice"]))
    tier, effort = tier_from_answers(yes)
    return {**_result(tier, min(confs), effort, f"openai-questions:{model}"),
            "answers": sorted(k for k, v in yes.items() if v)}


def google_decisions(task, tiers, cfg):
    """Gemini Decisions API: POST {base}/v1beta/decisions."""
    base = (cfg.get("MODEL_PICKER_ROUTER_URL") or "https://generativelanguage.googleapis.com").rstrip("/")
    model = cfg.get("MODEL_PICKER_ROUTER_MODEL") or "gemini-decision-1"
    body = {
        "model": model,
        "input": task,
        "questions": {
            "tier": {"type": "choice", "instructions": INSTRUCTIONS["tier"],
                     "criteria": {t: TIER_CRITERIA[t] for t in tiers}},
            "effort": {"type": "choice", "instructions": INSTRUCTIONS["effort"], "criteria": EFFORT_CRITERIA},
        },
    }
    auth = {"x-goog-api-key": cfg["_api_key"]} if cfg.get("_api_key") else {}
    headers = {**auth, **cfg.get("_headers", {})}
    data = _post(f"{base}/v1beta/decisions", body, headers, cfg["_timeout"])
    answers = data.get("answers") or {}
    tier = answers.get("tier") or {}
    if tier.get("choice") not in tiers:
        raise ValueError("no usable tier answer")
    return _result(tier["choice"], _confidence(tier, tier["choice"]), (answers.get("effort") or {}).get("choice"),
                   f"google-decisions:{model}")


def run_shell(cmd, timeout, stdin=None, keep_stderr=False):
    """Run a shell command with a hard timeout that also kills its children (`a; b`, pipes), so a slow
    command never outlives the hook. Returns stdout; raises on timeout or a non-zero exit.
    keep_stderr: on a non-zero exit, the error carries the last line of stderr (`stderr`), so a failing
    router command can say why. Off for the API-key command, whose errors may echo secrets. stderr goes to a
    temp file, not a pipe: a helper the command leaves running may hold stderr open, and waiting for a pipe's
    end would then time every call out."""
    with tempfile.TemporaryFile() if keep_stderr else open(os.devnull, "wb") as errf, \
            subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errf,
                             text=True, start_new_session=True) as proc:
        try:
            out, _ = proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, AttributeError):  # AttributeError: no process groups (Windows)
                proc.kill()
            proc.wait()
            raise
        if proc.returncode:
            tail = None
            if keep_stderr:
                errf.seek(max(0, errf.seek(0, os.SEEK_END) - 4096))  # the end is enough for the last line
                lines = [ln.strip() for ln in errf.read().decode("utf-8", "replace").splitlines() if ln.strip()]
                tail = lines[-1][:160] if lines else None
            raise subprocess.CalledProcessError(proc.returncode, cmd, stderr=tail)
    return out


def command(task, tiers, cfg):
    """Any executable: JSON {task, tiers, efforts} on stdin -> JSON {tier, effort, confidence} on stdout.
    Use it for providers without a built-in adapter, or to keep private gateway logic out of the plugin."""
    cmd = cfg.get("MODEL_PICKER_ROUTER_CMD")
    if not cmd:
        raise ValueError("MODEL_PICKER_ROUTER_CMD is not set")
    out = run_shell(cmd, cfg["_timeout"], json.dumps({"task": task, "tiers": tiers, "efforts": EFFORTS}),
                    keep_stderr=True)
    data = json.loads(out)
    if data.get("tier") not in tiers:
        raise ValueError(f"command returned tier {data.get('tier')!r}")
    return _result(data["tier"], _confidence(data, data["tier"]), data.get("effort"), "command")


ROUTERS = {"openai-decisions": openai_decisions, "google-decisions": google_decisions, "command": command}
DEFAULT_KEY_ENV = {"openai-decisions": "OPENAI_API_KEY", "google-decisions": "GEMINI_API_KEY"}
