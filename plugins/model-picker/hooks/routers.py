"""Optional external routers that pick a model tier and effort for a task.

Every router returns {"tier", "effort", "confidence", "source"} or raises; the
caller falls back to the keyword heuristic on any error or low confidence.
Stdlib only (urllib), so the hook keeps zero dependencies.
"""

import json
import os
import signal
import subprocess
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


def run_shell(cmd, timeout, stdin=None):
    """Run a shell command with a hard timeout that also kills its children (`a; b`, pipes), so a slow
    command never outlives the hook. Returns stdout; raises on timeout or a non-zero exit."""
    with subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
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
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    return out


def command(task, tiers, cfg):
    """Any executable: JSON {task, tiers, efforts} on stdin -> JSON {tier, effort, confidence} on stdout.
    Use it for providers without a built-in adapter, or to keep private gateway logic out of the plugin."""
    cmd = cfg.get("MODEL_PICKER_ROUTER_CMD")
    if not cmd:
        raise ValueError("MODEL_PICKER_ROUTER_CMD is not set")
    out = run_shell(cmd, cfg["_timeout"], json.dumps({"task": task, "tiers": tiers, "efforts": EFFORTS}))
    data = json.loads(out)
    if data.get("tier") not in tiers:
        raise ValueError(f"command returned tier {data.get('tier')!r}")
    return _result(data["tier"], _confidence(data, data["tier"]), data.get("effort"), "command")


ROUTERS = {"openai-decisions": openai_decisions, "google-decisions": google_decisions, "command": command}
DEFAULT_KEY_ENV = {"openai-decisions": "OPENAI_API_KEY", "google-decisions": "GEMINI_API_KEY"}
