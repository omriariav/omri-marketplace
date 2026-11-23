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
import html
import json
import os
import tempfile
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
RANK = {t: i for i, t in enumerate(("local",) + TIERS)}
ROTATE_BYTES = 5 * 1024 * 1024
KEEP_ROTATED = 10


def state_dir(cfg):
    # The environment also counts: in-process callers may pass a config dict without the key.
    return Path(cfg.get("MODEL_PICKER_STATE_DIR") or os.environ.get("MODEL_PICKER_STATE_DIR")
                or Path.home() / ".config/model-picker")


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


def read(cfg, since=0.0, tail_bytes=None):
    """All records newer than `since` (epoch seconds), oldest first, across rotated files.

    A rotated file is named log.<epoch ms when rotated>.jsonl, so every record in it is older than
    its name: files rotated before `since` are skipped unread. With `tail_bytes` (hot paths that only
    need recent records), only the end of the newest rotated file and of the current log are read.
    """
    directory = state_dir(cfg)
    rotated = [p for p in _rotated(directory) if int(p.name[len("log."):-len(".jsonl")]) >= since * 1000]
    if tail_bytes:
        rotated = rotated[-1:]
    out = []
    for path in rotated + [directory / "log.jsonl"]:
        try:
            if tail_bytes:
                lines = _tail_lines(path, tail_bytes)
            else:
                with open(path, encoding="utf-8") as fh:
                    lines = fh.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("ts", 0) >= since:
                out.append(rec)
    return out


# --- background-stamp queue ------------------------------------------------------------
# Kept apart from log.jsonl so the per-prompt hook reads a tiny file in full (no tail window, no
# rotation edge) and so delivery bookkeeping can fail loudly instead of being swallowed.
QUEUE_MAX_AGE = 86400
QUEUE_MAX_RECORDS = 500
QUEUE_LOCK_WAIT = 1.0  # seconds; a stuck holder must never stall every prompt


def _queue_path(cfg):
    return state_dir(cfg) / "pending-stamps.jsonl"


def _queue_lock(cfg):
    d = state_dir(cfg)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(d / ".queue.lock", os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + QUEUE_LOCK_WAIT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() > deadline:
                os.close(fd)
                raise OSError("stamp queue busy")
            time.sleep(0.02)


def _queue_read(cfg):
    try:
        with open(_queue_path(cfg), encoding="utf-8") as fh:
            out = []
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
            return out
    except OSError:
        return []


def _queue_write(cfg, records):
    """Atomic rewrite; raises on failure (callers must not deliver if bookkeeping failed)."""
    path = _queue_path(cfg)
    # Delivered stamps are done: keep only undelivered ones, newest QUEUE_MAX_RECORDS.
    records = [r for r in records if not r.get("delivered")][-QUEUE_MAX_RECORDS:]
    tmp = path.with_name(f".pending-stamps.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def queue_stamp(cfg, session, agent_id, text):
    """Queue a background agent's stamp for delivery on its session's next turn. Never raises."""
    try:
        fd = _queue_lock(cfg)
    except OSError:
        return False
    try:
        now = time.time()
        records = [r for r in _queue_read(cfg) if now - r.get("ts", 0) < QUEUE_MAX_AGE]
        records.append({"ts": round(now, 3), "session": session, "agent_id": agent_id, "text": text,
                        "delivered": False})
        _queue_write(cfg, records)
        return True
    except Exception:
        return False
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def take_stamps(cfg, session):
    """Pop this session's undelivered stamps (newest per agent). Marks them delivered first; if that
    write fails, returns nothing so a stamp is never shown on every prompt."""
    try:
        fd = _queue_lock(cfg)
    except OSError:
        return []
    try:
        now = time.time()
        records = [r for r in _queue_read(cfg) if now - r.get("ts", 0) < QUEUE_MAX_AGE]
        mine = [r for r in records if r.get("session") == session and not r.get("delivered") and r.get("text")]
        if not mine:
            return []
        for r in mine:
            r["delivered"] = True
        try:
            _queue_write(cfg, records)
        except Exception:
            return []
        latest = {}
        for r in mine:  # a resumed agent stops more than once: show only its newest stamp
            latest[r.get("agent_id") or r["ts"]] = r
        return [r["text"] for r in latest.values()]
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
        start = max(0, size - max_bytes)
        if start:
            fh.seek(start - 1)
            starts_clean = fh.read(1) == b"\n"  # the window begins exactly at a line start
        else:
            starts_clean = True
        fh.seek(start)
        data = fh.read().decode("utf-8", errors="replace")
    lines = data.splitlines()
    return lines if starts_clean else lines[1:]  # drop only a genuinely partial first line


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

    Returns a list of {"model", "input", "output", "cache_read", "cache_write", "cache_write_1h", "effort"} dicts,
    where cache_write counts all cache writes and cache_write_1h the 1-hour share of them, and effort is the
    reasoning effort the turn ran at (None when the transcript doesn't record it).
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
                "effort": rec.get("effort") if isinstance(rec.get("effort"), str) else None,
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


def run_effort(messages):
    """The effort most of the subagent's turns ran at, from the transcript; None if not recorded."""
    counts = {}
    for m in messages:
        if m.get("effort"):
            counts[m["effort"]] = counts.get(m["effort"], 0) + 1
    return max(counts, key=counts.get) if counts else None


def _rec_label(source):
    """Who made a recommendation, for people: the router model ("jev-latest") or "keyword rules"."""
    src = str(source or "")
    if not src or src.startswith("heuristic"):
        return "keyword rules"
    head = src.split(" (")[0].split(" + ")[0]
    if head == "command":
        return "command router"
    return head.split(":", 1)[-1].split("/")[-1] or "router"  # model ids may contain ":" themselves


def stamp_verdict(decision, effort):
    """Did the launch follow model-picker's recommendation? Same rules as the ledger's "followed": only explicit
    launches are judged, a fallback guess judges nothing. Effort is compared only when a router chose it (the
    keyword rules always say "medium"). "" when there was nothing to compare (pinned agent, no launch record)."""
    if not decision or not decision.get("rec_tier"):
        return ""
    by_router = not str(decision.get("source") or "heuristic").startswith("heuristic")
    effort_chosen = by_router or bool(decision.get("floor_effort"))  # the keyword rules' "medium" is a placeholder
    rec_effort = decision.get("rec_effort") or "medium"
    rec = f"{decision['rec_tier']}/{rec_effort}" if effort_chosen else decision["rec_tier"]
    who = _rec_label(decision.get("source")) + (" + your floor" if decision.get("floor") or decision.get("floor_effort") else "")
    chosen = decision.get("chosen_tier")
    pick = decision.get("user_pick")
    if isinstance(pick, dict) and (pick.get("tier") or pick.get("effort")):
        mine = "/".join(x for x in (pick.get("tier"), pick.get("effort") and f"{pick['effort']} effort") if x)
        said = "only guessed" if decision.get("unsure") else "said"
        return f" · your call ({mine}); {who} {said} {rec}"
    if decision.get("unsure"):
        return f" · {who} only guessed {rec}"
    if decision.get("outcome") != "explicit":
        return f" · no model set; {who} recommended {rec}"
    if not chosen:
        return f" · {who} recommended {rec}"
    if chosen != decision["rec_tier"] and not (decision["rec_tier"] == "local" and chosen == "haiku"):
        return f" · ✗ {who} recommended {rec}"
    # An unknown effort is "differs" only against a floor's effort (no effort set inherits the session's).
    if effort_chosen and effort != rec_effort and (effort or decision.get("floor_effort")):
        return f" · ✓ model as {who} recommended ({rec}), effort differs"
    return f" · ✓ followed {who} ({rec})"


def stamp_text(agent_type, model, totals, baseline_tier, effort=None, decision=None):
    """One-line ground-truth stamp: which model actually answered (and at what effort), tokens, cost, and
    whether that matched the recommendation."""
    tokens = totals["input"] + totals["output"] + totals["cache_read"] + totals["cache_write"]
    size = f"{tokens / 1e6:.2f}M" if tokens >= 1e6 else f"{tokens / 1e3:.0f}K" if tokens >= 1e3 else str(tokens)
    cost = "$0 (local)" if totals.get("local") else f"~${totals['cost']:.3f}"
    vs = ""
    if baseline_tier and totals["baseline_cost"] and totals["saving"] > 0:
        vs = f" (~${totals['baseline_cost']:.3f} on {baseline_tier})"
    at = f" at {effort} effort" if effort else ""
    return (f"[model-picker] {agent_type or 'subagent'} ran on {model}{at} · {size} tokens · {cost}{vs}"
            f"{stamp_verdict(decision, effort)}")


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
    rule          matched a recommendation that the user's own MODEL_PICKER_FLOORS rule raised
    user          the model the user asked for in their prompt
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
        if decision.get("user_pick"):
            return "user"
        if decision.get("followed") is None:
            return "unjudged"  # explicit model, but no confident recommendation to compare against
        if decision.get("followed"):
            return "rule" if decision.get("floor") else "model-picker"
        return "explicit"
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
    prompts = [r for r in recs if r.get("kind") == "prompt"]
    checked = [d for d in dec if d.get("outcome") in ("explicit", "missing")]
    explicit = [d for d in checked if d["outcome"] == "explicit"]
    user_picks = [d for d in explicit if d.get("user_pick")]
    # The user's own pick isn't a verdict on the recommendation either way.
    compared = [d for d in explicit if d.get("followed") is not None and not d.get("user_pick")]
    # Not followed, by direction: going stronger is often deliberate (a stated reason, a user rule) and costs
    # money; going cheaper saves money but is the risky direction.
    diverged = [d for d in compared if not d["followed"] and d.get("chosen_tier") in RANK and d.get("rec_tier") in RANK]
    stronger = sum(1 for d in diverged if RANK[d["chosen_tier"]] > RANK[d["rec_tier"]])
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
        "user_picks": len(user_picks),
        "compared": len(compared),
        "went_stronger": stronger,
        "went_cheaper": len(diverged) - stronger,
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
        "prompts_routed": len(prompts),
        "prompts_cheaper": sum(1 for r in prompts if r.get("session_tier") in RANK and r.get("rec_tier") in RANK
                               and RANK[r["rec_tier"]] < RANK[r["session_tier"]]),
        "prompt_tiers": _count(r.get("rec_tier") for r in prompts),
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


def _diverged(r):
    """", not followed: 5 went stronger, 1 went cheaper" (empty when everything was followed)."""
    up, down = r.get("went_stronger", 0), r.get("went_cheaper", 0)
    if not up and not down:
        return ""
    return f"; of the rest, {up} went to a stronger model, {down} to a cheaper one"


PACE_DAYS = {"24h": 1, "7d": 7}  # periods long enough, and fixed enough, to extrapolate a monthly figure


def _pace(r):
    """" At this pace, about $X a month." for a fixed 24h/7d window with a credited saving, else ""."""
    days = PACE_DAYS.get(r.get("period"))
    saved = r.get("credited_saving") or 0
    return f" At this pace, about ${saved * 30 / days:,.0f} a month." if days and saved > 0 else ""


def _credited_agents(r):
    return (r.get("saving_by_attribution") or {}).get("model-picker", {}).get("agents", 0)


def _prompts_line(r):
    tiers = ", ".join(f"{k} {v}" for k, v in sorted(r["prompt_tiers"].items(), key=lambda kv: -kv[1]))
    return (f"Prompts routed: {r['prompts_routed']} ({tiers}); {r['prompts_cheaper']} looked like work for a cheaper "
            "model than the session's")


def format_report(r):
    """Render the report as Markdown that survives any renderer: a bold title, bullet lists (never bare
    lines, which Markdown joins into one paragraph), and blank lines around the table."""
    lines = [f"**model-picker report ({r['period']})**", ""]
    if not r["launches"] and not r["subagents"] and not r.get("local_calls") and not r.get("prompts_routed"):
        return "\n".join(lines + ["No launches logged yet. The ledger fills as agents launch "
                                  "(`MODEL_PICKER_LOG` must not be `0`)."])
    tiers = ", ".join(f"{k} {v}" for k, v in sorted(r["launch_tiers"].items(), key=lambda kv: -kv[1]))
    if r["by_model"]:
        lines += [f"**Saved by model-picker: {_money(r.get('credited_saving', 0))}** ({_credited_agents(r)} agent(s) ran "
                  f"on the cheaper model it recommended).{_pace(r)}",
                  f"- All cheaper-than-session runs together: {_money(r['saving'])} "
                  f"({_pct(r['saving'], r['baseline_cost'])} of {_money(r['baseline_cost'])} at the session model); "
                  "the breakdown below the table says who gets credit for the rest.", ""]
    lines += [
        f"- Launches: {r['launches']} ({r['checked']} checked, {r['skipped']} skipped as fork/pinned/built-in"
        + (f"; {r['denied']} denied in enforce mode and retried" if r.get("denied") else "") + ")",
        f"- Explicit model: {r['explicit']}/{r['checked']} ({_pct(r['explicit'], r['checked'])})",
        f"- Followed recommendation: {r['followed']}/{r['compared']} ({_pct(r['followed'], r['compared'])})"
        + _diverged(r)
        + (f"; {r['user_picks']} more used the model you asked for" if r.get("user_picks") else ""),
        f"- Launch models: {tiers or 'n/a'}",
    ]
    if r["router_calls"]:
        lines.append(f"- Router: {r['router_hits']}/{r['router_calls']} answered by the decisions API "
                     f"(p50 {r['router_p50_ms']} ms, avg confidence {r['router_avg_conf']}); rest by heuristic")
    if r["by_model"]:
        lines += ["", "| Model | Agents | Input | Output | Cache read | Cost | At session model | Saved |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for key, row in sorted(r["by_model"].items(), key=lambda kv: -kv[1]["cost"]):
            lines.append(f"| {key} | {row['agents']} | {_tok(row['input'] + row['cache_write'])} | "
                         f"{_tok(row['output'])} | {_tok(row['cache_read'])} | ${row['cost']:.2f} | "
                         f"${row['baseline_cost']:.2f} | ${row['saving']:.2f} |")
        labels = {"model-picker": "followed model-picker's recommendation",
                  "rule": "followed your own MODEL_PICKER_FLOORS rule",
                  "user": "the model you asked for in your prompt",
                  "definition": "model set by the agent's own definition (would have happened anyway)",
                  "explicit": "explicit model that differed from the recommendation",
                  "unjudged": "explicit model with no confident recommendation to compare against",
                  "inherited": "inherited the session model", "unknown": "no launch record"}
        attr = r.get("saving_by_attribution") or {}
        lines += ["",
                  f"- Subagent spend: ${r['cost']:.2f}; at the session model it would have been "
                  f"${r['baseline_cost']:.2f}. Total difference: ${r['saving']:.2f} "
                  f"({_pct(r['saving'], r['baseline_cost'])}).",
                  f"- Credited to model-picker: ${r.get('credited_saving', 0):.2f} "
                  f"({_credited_agents(r)} agent(s) that followed its recommendation)."]
        for key in ("rule", "user", "definition", "explicit", "unjudged", "inherited", "unknown"):
            if key in attr:
                lines.append(f"  - Not credited, {labels[key]}: {attr[key]['agents']} agent(s), "
                             f"${attr[key]['saving']:.2f}")
        if r["local_agents"]:
            lines.append(f"- {r['local_agents']} subagent(s) ran on a local model, counted at $0.")
        lines += ["", "_Estimate: same tokens at list prices; a larger model may have used fewer tokens._"]
    if r.get("prompts_routed"):
        lines += ["", f"- {_prompts_line(r)}"]
    if r.get("local_calls"):
        ok = r["local_calls"] - r["local_errors"]
        lines += ["", f"- Local model (ask_local): {ok} call(s), {_tok(r['local_input'])} in / "
                      f"{_tok(r['local_output'])} out tokens, $0; ~${r['local_haiku_equiv']:.3f} at Haiku prices"
                      + (f"; {r['local_errors']} failed" if r["local_errors"] else "") + "."]
    return "\n".join(lines)


# --- HTML report -------------------------------------------------------------------

_ATTR_LABELS = {"model-picker": "Followed model-picker", "rule": "Your own rule (floors)", "user": "You asked for it",
                "definition": "Agent definition's own model",
                "explicit": "Explicit, differed from recommendation", "unjudged": "Explicit, no confident recommendation",
                "inherited": "Inherited the session model", "unknown": "No launch record"}
_TIER_ORDER = ("local", "haiku", "sonnet", "opus", "fable")

_CSS = """
:root{color-scheme:light;--bg:#fcfcfb;--card:#ffffff;--line:#e4e3df;--ink:#0b0b0b;--ink2:#52514e;--muted:#7a7974;
--s1:#2a78d6;--s2:#eb6834;--good:#006300}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#1a1a19;--card:#232322;
--line:#383835;--ink:#f0efec;--ink2:#c3c2b7;--muted:#9a998f;--s1:#3987e5;--s2:#d95926;--good:#5fc45f}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#1a1a19;--card:#232322;--line:#383835;--ink:#f0efec;--ink2:#c3c2b7;
--muted:#9a998f;--s1:#3987e5;--s2:#d95926;--good:#5fc45f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:32px 16px 48px}h1{font-size:22px;margin:0 0 4px}
h2{font-size:15px;margin:32px 0 12px;color:var(--ink2);font-weight:600}.sub{color:var(--muted);font-size:13px;margin:0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(170px,100%),1fr));gap:12px;margin-top:24px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile .k{font-size:12px;color:var(--ink2)}.tile .v{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums}
.tile .n{font-size:12px;color:var(--muted)}.tile.hero{margin-top:24px;padding:20px 22px}
.tile.hero .k{font-size:14px}.tile.hero .v{font-size:44px;line-height:1.1;color:var(--good)}.tile.hero .n{font-size:14px;color:var(--ink2)}
.card{min-width:0;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;overflow-x:auto}
.legend{display:flex;gap:16px;font-size:12px;color:var(--ink2);margin-bottom:10px}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.row{display:grid;grid-template-columns:70px 1fr;gap:10px;align-items:center;margin:6px 0}
.row .lab{font-size:13px;color:var(--ink2)}.bars{display:flex;flex-direction:column;gap:2px}
.bar{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--ink2);font-variant-numeric:tabular-nums}
.bar i{display:block;height:10px;border-radius:0 4px 4px 0;min-width:2px}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{color:var(--ink2);font-weight:600}th:first-child,td:first-child{text-align:left;white-space:normal;min-width:90px}
tr:last-child td{border-bottom:none}td.credited{color:var(--good);font-weight:600}
.inline{display:inline-block;height:8px;border-radius:0 3px 3px 0;background:var(--s1);vertical-align:middle;margin-right:6px}
ul.facts{margin:0;padding-left:18px;color:var(--ink2);font-size:13px}.foot{margin-top:28px;font-size:12px;color:var(--muted)}
"""


def _money(x):
    return f"${x:,.2f}"


def format_html(r, generated=None):
    """Render the report dict as one self-contained HTML page (no external assets, nothing fetched)."""
    e = lambda v: html.escape(str(v))
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(generated or time.time()))
    head = (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>model-picker report ({e(r['period'])})</title><style>{_CSS}</style></head><body><main>"
            f"<h1>model-picker report</h1><p class=sub>Period: {e(r['period'])} · generated {e(when)} · "
            f"from the local ledger</p>")
    if not r["launches"] and not r["subagents"] and not r.get("local_calls") and not r.get("prompts_routed"):
        return head + "<p>No launches logged yet. The ledger fills as agents launch.</p></main></body></html>"
    attr = r.get("saving_by_attribution") or {}
    out = [head]
    if r["by_model"]:
        out.append(f"<div class='tile hero'><div class=k>Saved by model-picker</div>"
                   f"<div class=v>{e(_money(r.get('credited_saving', 0)))}</div><div class=n>"
                   f"{_credited_agents(r)} agent(s) ran on the cheaper model it recommended.{e(_pace(r))}</div></div>")
    tiles = [
        ("Subagent spend", _money(r["cost"]), f"{r['subagents']} subagent(s)", ""),
        ("At the session model", _money(r["baseline_cost"]),
         "if every subagent had run on it", ""),
        ("All cheaper runs saved", _money(r["saving"]), "including your rules, agent definitions and own picks", ""),
        ("Followed recommendation", _pct(r["followed"], r["compared"]),
         f"{r['followed']} of {r['compared']} launches" + (f"; {r.get('went_stronger', 0)} went stronger, "
                                                           f"{r.get('went_cheaper', 0)} cheaper"
                                                           if r.get("went_stronger") or r.get("went_cheaper") else "")
         + (f"; {r['user_picks']} you picked" if r.get("user_picks") else ""), ""),
    ]
    out.append("<div class=tiles>")
    for k, v, n, cls in tiles:
        out.append(f"<div class='tile{cls}'><div class=k>{e(k)}</div><div class=v>{e(v)}</div><div class=n>{e(n)}</div></div>")
    out.append("</div>")

    rec, got = r.get("recommended_tiers") or {}, r.get("launch_tiers") or {}
    keys = [t for t in _TIER_ORDER if t in rec or t in got] + sorted((set(rec) | set(got)) - set(_TIER_ORDER))
    if keys:
        top = max([rec.get(k, 0) for k in keys] + [got.get(k, 0) for k in keys] + [1])
        out.append("<h2>Recommended vs launched, by model</h2><div class=card><div class=legend>"
                   "<span><span class=sw style='background:var(--s1)'></span>Recommended</span>"
                   "<span><span class=sw style='background:var(--s2)'></span>Launched</span></div>")
        for k in keys:
            out.append(f"<div class=row><div class=lab>{e(k)}</div><div class=bars>")
            for name, src, color in (("Recommended", rec, "--s1"), ("Launched", got, "--s2")):
                n = src.get(k, 0)
                out.append(f"<div class=bar title='{e(name)} {e(k)}: {n}'><i style='width:{100 * n / top:.1f}%;"
                           f"background:var({color})'></i>{n}</div>")
            out.append("</div></div>")
        out.append("</div>")

    if r["by_model"]:
        rows = sorted(r["by_model"].items(), key=lambda kv: -kv[1]["cost"])
        top = max([row["cost"] for _, row in rows] + [1e-9])
        out.append("<h2>Subagent spend by model</h2><div class=card><table><thead><tr><th>Model</th><th>Agents</th>"
                   "<th>Input</th><th>Output</th><th>Cache read</th><th style='text-align:left'>Cost</th>"
                   "<th>At session model</th><th>Saved</th></tr></thead><tbody>")
        for key, row in rows:
            w = 80 * row["cost"] / top
            out.append(f"<tr><td>{e(key)}</td><td>{row['agents']}</td><td>{_tok(row['input'] + row['cache_write'])}</td>"
                       f"<td>{_tok(row['output'])}</td><td>{_tok(row['cache_read'])}</td>"
                       f"<td style='text-align:left'><span class=inline style='width:{w:.0f}px'></span>{_money(row['cost'])}</td>"
                       f"<td>{_money(row['baseline_cost'])}</td><td>{_money(row['saving'])}</td></tr>")
        out.append("</tbody></table></div>")

    if attr:
        out.append("<h2>Who gets credit for the difference</h2><div class=card><table><thead><tr><th>Why the model "
                   "was cheaper</th><th>Agents</th><th>Saved</th></tr></thead><tbody>")
        for key in sorted(attr, key=lambda k: (k != "model-picker", -attr[k]["saving"])):
            cls = " class=credited" if key == "model-picker" else ""
            out.append(f"<tr><td>{e(_ATTR_LABELS.get(key, key))}</td><td>{attr[key]['agents']}</td>"
                       f"<td{cls}>{_money(attr[key]['saving'])}</td></tr>")
        out.append("</tbody></table></div>")

    facts = [f"Launches: {r['launches']} ({r['checked']} checked, {r['skipped']} skipped as fork/pinned/built-in"
             + (f"; {r['denied']} denied in enforce mode" if r.get("denied") else "") + ")",
             f"Explicit model: {r['explicit']}/{r['checked']} ({_pct(r['explicit'], r['checked'])})"]
    if r["router_calls"]:
        facts.append(f"Router: {r['router_hits']}/{r['router_calls']} answered by the decisions API "
                     f"(p50 {r['router_p50_ms']} ms, avg confidence {r['router_avg_conf']}); rest by heuristic")
    if r["local_agents"]:
        facts.append(f"{r['local_agents']} subagent(s) ran on a local model, counted at $0")
    if r.get("prompts_routed"):
        facts.append(_prompts_line(r))
    if r.get("local_calls"):
        facts.append(f"Local model (ask_local): {r['local_calls'] - r['local_errors']} call(s), "
                     f"{_tok(r['local_input'])} in / {_tok(r['local_output'])} out, $0; "
                     f"~${r['local_haiku_equiv']:.3f} at Haiku prices"
                     + (f"; {r['local_errors']} failed" if r["local_errors"] else ""))
    out.append("<h2>Details</h2><div class=card><ul class=facts>" + "".join(f"<li>{e(f)}</li>" for f in facts) + "</ul></div>")
    out.append("<p class=foot>Estimates: the same tokens at list prices. A larger model may have used fewer tokens. "
               "Only launches that followed model-picker's recommendation are credited to it.</p></main></body></html>")
    return "".join(out)


def write_html(cfg, r, path=None):
    """Write the HTML report (default: <state dir>/report.html) and return its path.

    Written to an owner-only temp file and renamed over the target, so the result is 0600 even when
    a wider-mode file was there before, and a symlink at the target is replaced, never followed."""
    target = Path(path).expanduser() if path else state_dir(cfg) / "report.html"
    if target.is_dir():
        raise IsADirectoryError(f"{target} is a directory; pass a file path")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".report.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(format_html(r))
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target
