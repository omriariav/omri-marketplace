"""Local usage ledger for model-picker: decision log, subagent token usage, cost and savings.

Everything stays on this machine in $MODEL_PICKER_STATE_DIR (default ~/.config/model-picker):
  log.jsonl     one JSON object per line, {"kind": "decision" | "usage", ...}
  log.1.jsonl   previous file after rotation (at 5 MB)

Prices are Claude API list prices per million tokens from Anthropic's "Which Claude model should
you build with?" (Nov 2026). Cache writes are not listed there; they use Anthropic's standard
multipliers: 1.25x input for 5-minute writes, 2x input for 1-hour writes. Override any of it with MODEL_PICKER_PRICES
(JSON: {"opus": {"input": 4, "output": 20, "cache_read": 0.2, "cache_write": 5}, ...}).
Savings are estimates: the same tokens priced at the session model, which a subagent without an
explicit model would have inherited. A larger model may need fewer tokens for the same task.
"""

import json
import os
import time
from pathlib import Path

PRICES = {
    # tier: input, output, cache_read, cache_write ($ per million tokens)
    "haiku": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125, "cache_write_1h": 0.20},
    "haiku_long": {"input": 0.50, "output": 2.50, "cache_read": 0.05,  # prompt > 100K tokens
                   "cache_write": 0.625, "cache_write_1h": 1.0},
    "sonnet": {"input": 2.0, "output": 10.0, "cache_read": 0.10, "cache_write": 2.5, "cache_write_1h": 4.0},
    "opus": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0, "cache_write_1h": 8.0},
    "fable": {"input": 8.0, "output": 40.0, "cache_read": 0.20, "cache_write": 10.0, "cache_write_1h": 16.0},
}
HAIKU_LONG_PROMPT = 100_000
TIERS = ("haiku", "sonnet", "opus", "fable")
ROTATE_BYTES = 5 * 1024 * 1024


def state_dir(cfg):
    return Path(cfg.get("MODEL_PICKER_STATE_DIR") or Path.home() / ".config/model-picker")


def log_path(cfg):
    return state_dir(cfg) / "log.jsonl"


def logging_enabled(cfg):
    return str(cfg.get("MODEL_PICKER_LOG", "1")).strip().lower() not in ("0", "false", "off", "no")


def append(cfg, record):
    """Append one record; never raises (the ledger must not break a hook)."""
    if not logging_enabled(cfg):
        return
    try:
        path = log_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > ROTATE_BYTES:
            path.replace(path.with_name("log.1.jsonl"))
        record = {"ts": round(time.time(), 3), **record}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        pass


def read(cfg, since=0.0):
    """All records newer than `since` (epoch seconds), oldest first, across rotated files."""
    out = []
    for name in ("log.1.jsonl", "log.jsonl"):
        path = state_dir(cfg) / name
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get("ts", 0) >= since:
                        out.append(rec)
        except OSError:
            continue
    return out


# --- transcripts -----------------------------------------------------------------

def tier_of(model):
    model = (model or "").lower()
    for tier in TIERS:
        if tier in model:
            return tier
    return None


def _tail_lines(path, max_bytes=512 * 1024):
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        data = fh.read().decode("utf-8", errors="replace")
    lines = data.splitlines()
    return lines[1:] if size > max_bytes else lines  # drop a possibly partial first line


def last_model(transcript_path):
    """Model id of the most recent assistant message in a transcript (the session model)."""
    try:
        for line in reversed(_tail_lines(transcript_path)):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            msg = rec.get("message") if isinstance(rec, dict) else None
            if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("model"):
                if msg["model"] != "<synthetic>":
                    return msg["model"]
    except (OSError, TypeError):
        pass
    return None


def transcript_usage(path):
    """Per-message usage from a transcript, deduplicated by message id (streamed chunks repeat it).

    Returns a list of {"model", "input", "output", "cache_read", "cache_write", "cache_write_1h"} dicts,
    where cache_write counts all cache writes and cache_write_1h the 1-hour share of them.
    """
    by_id, order = {}, []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            msg = rec.get("message") if isinstance(rec, dict) else None
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            usage = msg.get("usage")
            model = msg.get("model")
            if not isinstance(usage, dict) or not model or model == "<synthetic>":
                continue
            key = msg.get("id") or rec.get("requestId") or f"line{n}"
            if key not in by_id:
                order.append(key)
            # Keep the last chunk for an id: its output_tokens is the final count.
            split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
            by_id[key] = {
                "model": model,
                "input": int(usage.get("input_tokens") or 0),
                "output": int(usage.get("output_tokens") or 0),
                "cache_read": int(usage.get("cache_read_input_tokens") or 0),
                "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
                "cache_write_1h": int(split.get("ephemeral_1h_input_tokens") or 0),
            }
    return [by_id[k] for k in order]


def subagent_meta(transcript_path):
    """The sibling agent-<id>.meta.json (agentType, description, toolUseId, model), or {}."""
    try:
        p = Path(transcript_path)
        meta = p.with_name(p.name[:-len(".jsonl")] + ".meta.json") if p.name.endswith(".jsonl") else None
        return json.loads(meta.read_text(encoding="utf-8")) if meta and meta.exists() else {}
    except (OSError, ValueError, TypeError):
        return {}


def subagent_transcript(payload):
    """Locate the finished subagent's transcript from a SubagentStop payload."""
    path = payload.get("agent_transcript_path")
    if path and Path(path).exists():
        return path
    parent, agent_id = payload.get("transcript_path"), payload.get("agent_id")
    if parent and agent_id:
        p = Path(parent)
        guess = p.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
        if guess.exists():
            return str(guess)
    return None


# --- pricing ---------------------------------------------------------------------

def price_table(cfg):
    table = {k: dict(v) for k, v in PRICES.items()}
    override = cfg.get("MODEL_PICKER_PRICES")
    if override:
        try:
            data = override if isinstance(override, dict) else json.loads(override)
            for tier, prices in data.items():
                table.setdefault(tier, {}).update(prices)
        except (ValueError, AttributeError, TypeError):
            pass
    return table


def message_cost(msg, tier, table):
    """Dollar cost of one API message's tokens at `tier` prices; None if the tier is unpriced."""
    if tier not in table:
        return None
    prices = table[tier]
    if tier == "haiku":
        prompt = msg["input"] + msg["cache_read"] + msg["cache_write"]
        if prompt > HAIKU_LONG_PROMPT and "haiku_long" in table:
            prices = table["haiku_long"]
    w1h = min(msg.get("cache_write_1h", 0), msg["cache_write"])
    w5m = msg["cache_write"] - w1h
    return (msg["input"] * prices.get("input", 0) + msg["output"] * prices.get("output", 0)
            + msg["cache_read"] * prices.get("cache_read", 0) + w5m * prices.get("cache_write", 0)
            + w1h * prices.get("cache_write_1h", prices.get("cache_write", 0))) / 1_000_000


def summarize_usage(messages, baseline_tier, table):
    """Totals for one subagent: tokens, actual cost, cost at the baseline tier, and saving.

    A model with no Claude tier in its id (for example a local model behind a gateway) is
    priced at $0 and flagged `local`; its saving is the full baseline cost.
    """
    tot = {"messages": len(messages), "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
           "cost": 0.0, "baseline_cost": 0.0, "models": {}, "local": False, "unpriced": False}
    for msg in messages:
        for k in ("input", "output", "cache_read", "cache_write"):
            tot[k] += msg[k]
        tier = tier_of(msg["model"])
        tot["models"][msg["model"]] = tot["models"].get(msg["model"], 0) + 1
        if tier is None and "claude" not in msg["model"].lower():
            cost, tot["local"] = 0.0, True
        else:
            cost = message_cost(msg, tier, table)
            if cost is None:
                tot["unpriced"] = True
                cost = 0.0
        tot["cost"] += cost
        base = message_cost(msg, baseline_tier, table) if baseline_tier else None
        tot["baseline_cost"] += cost if base is None else base
    tot["saving"] = tot["baseline_cost"] - tot["cost"]
    tot["cost"], tot["baseline_cost"], tot["saving"] = (round(tot[k], 6) for k in ("cost", "baseline_cost", "saving"))
    return tot


# --- report ------------------------------------------------------------------------

PERIODS = {"today": None, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "all": 0}


def since_epoch(period, now=None):
    now = now or time.time()
    if period == "today":
        lt = time.localtime(now)
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    seconds = PERIODS.get(period)
    if seconds is None:
        raise ValueError(f"unknown period {period!r}; use one of {', '.join(PERIODS)}")
    return 0.0 if seconds == 0 else now - seconds


def report(cfg, period="7d", now=None):
    """Aggregate the ledger into a dict (see format_report for the rendered view)."""
    recs = read(cfg, since_epoch(period, now))
    dec = [r for r in recs if r.get("kind") == "decision"]
    use = [r for r in recs if r.get("kind") == "usage"]
    checked = [d for d in dec if d.get("outcome") in ("explicit", "missing")]
    explicit = [d for d in checked if d["outcome"] == "explicit"]
    compared = [d for d in explicit if d.get("followed") is not None]
    routed = [d for d in dec if d.get("source")]
    router_hits = [d for d in routed if not str(d["source"]).startswith("heuristic")]
    latencies = sorted(d["router_ms"] for d in router_hits if isinstance(d.get("router_ms"), (int, float)))
    confidences = [d["confidence"] for d in router_hits if isinstance(d.get("confidence"), (int, float))]
    by_model = {}
    for u in use:
        key = u.get("tier") or u.get("model") or "?"
        row = by_model.setdefault(key, {"agents": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                                        "cost": 0.0, "baseline_cost": 0.0, "saving": 0.0})
        row["agents"] += 1
        for k in ("input", "output", "cache_read", "cache_write", "cost", "baseline_cost", "saving"):
            row[k] += u.get(k) or 0
    tiers = {}
    for d in dec:
        tiers[d.get("chosen_tier") or "inherit"] = tiers.get(d.get("chosen_tier") or "inherit", 0) + 1
    return {
        "period": period,
        "launches": len(dec),
        "checked": len(checked),
        "explicit": len(explicit),
        "missing_model": len(checked) - len(explicit),
        "skipped": len(dec) - len(checked),
        "followed": sum(1 for d in compared if d["followed"]),
        "compared": len(compared),
        "launch_tiers": tiers,
        "recommended_tiers": _count(d.get("rec_tier") for d in checked),
        "router_calls": len(routed),
        "router_hits": len(router_hits),
        "router_p50_ms": latencies[len(latencies) // 2] if latencies else None,
        "router_avg_conf": round(sum(confidences) / len(confidences), 2) if confidences else None,
        "subagents": len(use),
        "by_model": by_model,
        "cost": round(sum(u.get("cost") or 0 for u in use), 4),
        "baseline_cost": round(sum(u.get("baseline_cost") or 0 for u in use), 4),
        "saving": round(sum(u.get("saving") or 0 for u in use), 4),
        "local_agents": sum(1 for u in use if u.get("local")),
    }


def _count(values):
    out = {}
    for v in values:
        if v:
            out[v] = out.get(v, 0) + 1
    return out


def _pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "n/a"


def _tok(n):
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.1f}K" if n >= 1e3 else str(int(n))


def format_report(r):
    lines = [f"model-picker report ({r['period']})", ""]
    if not r["launches"] and not r["subagents"]:
        return "\n".join(lines + ["No launches logged yet. The ledger fills as agents launch "
                                  "(MODEL_PICKER_LOG must not be 0)."])
    tiers = ", ".join(f"{k} {v}" for k, v in sorted(r["launch_tiers"].items(), key=lambda kv: -kv[1]))
    lines += [
        f"Launches: {r['launches']} ({r['checked']} checked, {r['skipped']} skipped as fork/pinned/built-in)",
        f"Explicit model: {r['explicit']}/{r['checked']} ({_pct(r['explicit'], r['checked'])})",
        f"Followed recommendation: {r['followed']}/{r['compared']} ({_pct(r['followed'], r['compared'])})",
        f"Launch models: {tiers or 'n/a'}",
    ]
    if r["router_calls"]:
        lines.append(f"Router: {r['router_hits']}/{r['router_calls']} answered by the decisions API "
                     f"(p50 {r['router_p50_ms']} ms, avg confidence {r['router_avg_conf']}); rest by heuristic")
    if r["by_model"]:
        lines += ["", "| Model | Agents | Input | Output | Cache read | Cost | At session model | Saved |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for key, row in sorted(r["by_model"].items(), key=lambda kv: -kv[1]["cost"]):
            lines.append(f"| {key} | {row['agents']} | {_tok(row['input'] + row['cache_write'])} | "
                         f"{_tok(row['output'])} | {_tok(row['cache_read'])} | ${row['cost']:.2f} | "
                         f"${row['baseline_cost']:.2f} | ${row['saving']:.2f} |")
        lines += ["", f"Subagent spend: ${r['cost']:.2f}; at the session model it would have been "
                      f"${r['baseline_cost']:.2f}. Estimated saving: ${r['saving']:.2f} "
                      f"({_pct(r['saving'], r['baseline_cost'])})."]
        if r["local_agents"]:
            lines.append(f"{r['local_agents']} subagent(s) ran on a local model, counted at $0.")
        lines.append("Estimate: same tokens at list prices; a larger model may have used fewer tokens.")
    return "\n".join(lines)
