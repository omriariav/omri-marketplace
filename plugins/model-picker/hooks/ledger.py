"""Local usage ledger for model-picker: decision log, subagent token usage, cost and savings.

Everything stays on this machine in $MODEL_PICKER_STATE_DIR (default ~/.config/model-picker):
  log.jsonl                 one JSON object per line, {"kind": "decision" | "usage", ...}
  log.<epoch>.jsonl         rotated files (at 5 MB); the newest KEEP_ROTATED are kept

Prices are Claude API list prices per million tokens from Anthropic's "Which Claude model should
you build with?" (Nov 2026). Cache writes are not listed there; they use Anthropic's standard
multipliers: 1.25x input for 5-minute writes, 2x input for 1-hour writes. Override any of it with MODEL_PICKER_PRICES
(JSON: {"opus": {"input": 4, "output": 20, "cache_read": 0.2, "cache_write": 5}, ...}).
Savings are estimates: the same tokens priced at the session model, which a subagent without an
explicit model would have inherited. A larger model may need fewer tokens for the same task.
"""

import fcntl
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
KEEP_ROTATED = 10


def state_dir(cfg):
    return Path(cfg.get("MODEL_PICKER_STATE_DIR") or Path.home() / ".config/model-picker")


def log_path(cfg):
    return state_dir(cfg) / "log.jsonl"


def logging_enabled(cfg):
    return str(cfg.get("MODEL_PICKER_LOG", "1")).strip().lower() not in ("0", "false", "off", "no")


def append(cfg, record):
    """Append one record; never raises (the ledger must not break a hook).

    Concurrent hooks (parallel subagents) serialize on an flock, so rotation happens once and
    no line interleaves. Files are created 0600 inside a 0700 directory.
    """
    if not logging_enabled(cfg):
        return
    try:
        path = log_path(cfg)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        line = json.dumps({"ts": round(time.time(), 3), **record}, separators=(",", ":")) + "\n"
        lock_fd = os.open(path.with_name(".log.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if path.exists() and path.stat().st_size > ROTATE_BYTES:
                path.replace(path.with_name(f"log.{int(time.time() * 1000)}.jsonl"))
                for old in _rotated(path.parent)[:-KEEP_ROTATED]:
                    old.unlink(missing_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
    except Exception:
        pass


def _rotated(directory):
    """Rotated ledger files, oldest first."""
    files = []
    for p in directory.glob("log.*.jsonl"):
        stem = p.name[len("log."):-len(".jsonl")]
        if stem.isdigit():
            files.append((int(stem), p))
    return [p for _, p in sorted(files)]


def read(cfg, since=0.0):
    """All records newer than `since` (epoch seconds), oldest first, across rotated files."""
    out = []
    directory = state_dir(cfg)
    for path in _rotated(directory) + [directory / "log.jsonl"]:
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


def find_decision(cfg, tool_use_id):
    """The decision record logged for a launch, by its tool_use_id (latest wins), or None."""
    if not tool_use_id:
        return None
    found = None
    for rec in read(cfg, time.time() - 7 * 86400):
        if rec.get("kind") == "decision" and rec.get("tool_use_id") == tool_use_id:
            found = rec
    return found


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


def find_subagent(parent_transcript, tool_use_id):
    """(transcript_path, meta) of the subagent launched by `tool_use_id`, or (None, {})."""
    if not parent_transcript or not tool_use_id:
        return None, {}
    folder = Path(parent_transcript).with_suffix("") / "subagents"
    try:
        metas = sorted(folder.glob("agent-*.meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None, {}
    for meta_path in metas[:200]:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("toolUseId") == tool_use_id:
            transcript = meta_path.with_name(meta_path.name[:-len(".meta.json")] + ".jsonl")
            return (str(transcript) if transcript.exists() else None), meta
    return None, {}


def stamp_text(agent_type, model, totals, baseline_tier):
    """One-line ground-truth stamp: which model actually answered, tokens, cost."""
    tokens = totals["input"] + totals["output"] + totals["cache_read"] + totals["cache_write"]
    size = f"{tokens / 1e6:.2f}M" if tokens >= 1e6 else f"{tokens / 1e3:.0f}K" if tokens >= 1e3 else str(tokens)
    cost = "$0 (local)" if totals.get("local") else f"~${totals['cost']:.3f}"
    vs = ""
    if baseline_tier and totals["baseline_cost"] and totals["saving"] > 0:
        vs = f" (~${totals['baseline_cost']:.3f} on {baseline_tier})"
    return f"[model-picker] {agent_type or 'subagent'} ran on {model} · {size} tokens · {cost}{vs}"


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


def attribute(decision):
    """Why a subagent ran on its model, for crediting savings honestly.

    model-picker  explicit model that matched the recommendation, not already set by the agent definition
    definition    the agent's own definition set that model (saving would have happened anyway)
    explicit      explicit model that differed from the recommendation
    unjudged      explicit model, but the recommendation was only a fallback guess
    inherited     no model: ran on the session model (no saving)
    unknown       no launch record (hooks inactive at launch, or a non-Agent launch)
    """
    if not decision:
        return "unknown"
    outcome = decision.get("outcome")
    pinned_tier = tier_of(decision.get("pinned"))
    if outcome == "pinned" or (pinned_tier and pinned_tier == decision.get("chosen_tier")):
        return "definition"
    if outcome == "explicit":
        if decision.get("followed") is None:
            return "unjudged"  # explicit model, but no confident recommendation to compare against
        return "model-picker" if decision.get("followed") else "explicit"
    if outcome in ("missing", "denied"):
        return "inherited"
    return "unknown"


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
                # Unknown Claude model: no price, so no claimed saving either.
                tot["unpriced"] = True
                tot["baseline_cost"] += 0.0
                continue
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
    # A denied launch (enforce mode) is retried with a model; count the retry, not both.
    dec = [r for r in recs if r.get("kind") == "decision" and r.get("outcome") != "denied"]
    denied = sum(1 for r in recs if r.get("kind") == "decision" and r.get("outcome") == "denied")
    # A resumed subagent stops more than once; each usage row is cumulative, so keep the last.
    last_use = {}
    for r in recs:
        if r.get("kind") == "usage":
            last_use[r.get("agent_id") or id(r)] = r
    use = list(last_use.values())
    local_calls = [r for r in recs if r.get("kind") == "local"]
    checked = [d for d in dec if d.get("outcome") in ("explicit", "missing")]
    explicit = [d for d in checked if d["outcome"] == "explicit"]
    compared = [d for d in explicit if d.get("followed") is not None]
    routed = [d for d in dec if d.get("router_ms") is not None]  # the router was actually called
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
    by_attr = {}
    for u in use:
        a = u.get("attribution") or "unknown"
        row = by_attr.setdefault(a, {"agents": 0, "saving": 0.0})
        row["agents"] += 1
        row["saving"] += u.get("saving") or 0
    tiers = {}
    for d in dec:
        if d.get("outcome") == "skipped":
            continue
        key = d.get("chosen_tier") or d.get("chosen") or "unknown"
        tiers[key] = tiers.get(key, 0) + 1
    return {
        "period": period,
        "launches": len(dec),
        "checked": len(checked),
        "explicit": len(explicit),
        "missing_model": len(checked) - len(explicit),
        "skipped": len(dec) - len(checked),
        "denied": denied,
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
        "saving_by_attribution": {k: {"agents": v["agents"], "saving": round(v["saving"], 4)}
                                  for k, v in by_attr.items()},
        "credited_saving": round(by_attr.get("model-picker", {}).get("saving", 0.0), 4),
        "local_agents": sum(1 for u in use if u.get("local")),
        "local_calls": len(local_calls),
        "local_input": sum(r.get("input") or 0 for r in local_calls),
        "local_output": sum(r.get("output") or 0 for r in local_calls),
        "local_errors": sum(1 for r in local_calls if r.get("error")),
        "local_haiku_equiv": round(sum(r.get("haiku_equiv") or 0 for r in local_calls), 4),
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
    if not r["launches"] and not r["subagents"] and not r.get("local_calls"):
        return "\n".join(lines + ["No launches logged yet. The ledger fills as agents launch "
                                  "(MODEL_PICKER_LOG must not be 0)."])
    tiers = ", ".join(f"{k} {v}" for k, v in sorted(r["launch_tiers"].items(), key=lambda kv: -kv[1]))
    lines += [
        f"Launches: {r['launches']} ({r['checked']} checked, {r['skipped']} skipped as fork/pinned/built-in"
        + (f"; {r['denied']} denied in enforce mode and retried" if r.get("denied") else "") + ")",
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
                      f"${r['baseline_cost']:.2f}. Total difference: ${r['saving']:.2f} "
                      f"({_pct(r['saving'], r['baseline_cost'])})."]
        labels = {"model-picker": "followed model-picker's recommendation",
                  "definition": "model set by the agent's own definition (would have happened anyway)",
                  "explicit": "explicit model that differed from the recommendation",
                  "unjudged": "explicit model with no confident recommendation to compare against",
                  "inherited": "inherited the session model", "unknown": "no launch record"}
        attr = r.get("saving_by_attribution") or {}
        lines.append(f"Credited to model-picker: ${r.get('credited_saving', 0):.2f} "
                     f"({attr.get('model-picker', {}).get('agents', 0)} agent(s) that followed its recommendation).")
        for key in ("definition", "explicit", "unjudged", "inherited", "unknown"):
            if key in attr:
                lines.append(f"  not credited — {labels[key]}: {attr[key]['agents']} agent(s), "
                             f"${attr[key]['saving']:.2f}")
        if r["local_agents"]:
            lines.append(f"{r['local_agents']} subagent(s) ran on a local model, counted at $0.")
        lines.append("Estimate: same tokens at list prices; a larger model may have used fewer tokens.")
    if r.get("local_calls"):
        ok = r["local_calls"] - r["local_errors"]
        lines += ["", f"Local model (ask_local): {ok} call(s), {_tok(r['local_input'])} in / "
                      f"{_tok(r['local_output'])} out tokens, $0; ~${r['local_haiku_equiv']:.3f} at Haiku prices"
                      + (f"; {r['local_errors']} failed" if r["local_errors"] else "") + "."]
    return "\n".join(lines)
