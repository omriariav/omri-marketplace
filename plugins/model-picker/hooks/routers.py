"""Optional external routers that pick a model tier and effort for a task.

Every router returns {"tier", "effort", "confidence", "source"} or raises; the
caller falls back to the keyword heuristic on any error or low confidence.
Stdlib only (urllib), so the hook keeps zero dependencies.
"""

import json
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


def _result(tier, tier_conf, effort, source):
    return {"tier": tier, "effort": effort if effort in EFFORTS else "medium",
            "confidence": float(tier_conf or 0), "source": source}


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
    headers = {"Authorization": f"Bearer {cfg['_api_key']}", **cfg.get("_headers", {})}
    data = _post(f"{base}/v1/decisions", body, headers, cfg["_timeout"])
    answers = {a.get("name"): a for a in data.get("answers", []) if isinstance(a, dict)}
    tier = answers.get("tier", {})
    if tier.get("type") != "choice" or tier.get("choice") not in tiers:
        raise ValueError(f"no usable tier answer ({tier.get('type')})")
    return _result(tier["choice"], tier.get("confidence"), answers.get("effort", {}).get("choice"),
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
    headers = {"x-goog-api-key": cfg["_api_key"], **cfg.get("_headers", {})}
    data = _post(f"{base}/v1beta/decisions", body, headers, cfg["_timeout"])
    answers = data.get("answers") or {}
    tier = answers.get("tier") or {}
    if tier.get("choice") not in tiers:
        raise ValueError("no usable tier answer")
    return _result(tier["choice"], tier.get("confidence"), (answers.get("effort") or {}).get("choice"),
                   f"google-decisions:{model}")


def command(task, tiers, cfg):
    """Any executable: JSON {task, tiers, efforts} on stdin -> JSON {tier, effort, confidence} on stdout.
    Use it for providers without a built-in adapter, or to keep private gateway logic out of the plugin."""
    cmd = cfg.get("MODEL_PICKER_ROUTER_CMD")
    if not cmd:
        raise ValueError("MODEL_PICKER_ROUTER_CMD is not set")
    out = subprocess.run(cmd, shell=True, input=json.dumps({"task": task, "tiers": tiers, "efforts": EFFORTS}),
                         capture_output=True, text=True, timeout=cfg["_timeout"], check=True).stdout
    data = json.loads(out)
    if data.get("tier") not in tiers:
        raise ValueError(f"command returned tier {data.get('tier')!r}")
    return _result(data["tier"], data.get("confidence", 1.0), data.get("effort"), "command")


ROUTERS = {"openai-decisions": openai_decisions, "google-decisions": google_decisions, "command": command}
DEFAULT_KEY_ENV = {"openai-decisions": "OPENAI_API_KEY", "google-decisions": "GEMINI_API_KEY"}
