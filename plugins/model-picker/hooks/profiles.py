"""Router profiles: named sets of router settings in the user's config, switched with one command.

MODEL_PICKER_ROUTER_PROFILES in the config file maps a name to a profile:
  {"description": "...", "settings": {"MODEL_PICKER_ROUTER": "command", "MODEL_PICKER_ROUTER_INPUT": null, ...},
   "on_select": "shell command", "on_leave": "shell command"}
Selecting a profile writes its settings into the config file (null removes a key), keeps everything else, then
runs the previous profile's on_leave and the new one's on_select (e.g. start or stop a local model server).
A built-in "keywords" profile (no router, keyword rules only) exists unless the user defines one by that name.
"""
import json
import os
import signal
import subprocess
import tempfile

PROFILES_KEY = "MODEL_PICKER_ROUTER_PROFILES"
PROFILE_FIELDS = {"description", "settings", "on_select", "on_leave"}
BUILTIN = {"keywords": {"description": "built-in: no router, keyword rules only",
                        "settings": {"MODEL_PICKER_ROUTER": "heuristic"}}}
HOOK_TIMEOUT = 60  # seconds for on_select / on_leave


class ProfileError(ValueError):
    pass


def read_raw(path):
    """The whole config file (every key, not only MODEL_PICKER_*), or {} when it doesn't exist yet."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise ProfileError(f"{path} is not valid JSON ({exc}); fix it by hand first")
    if not isinstance(data, dict):
        raise ProfileError(f"{path} is not a JSON object")
    return data


def profiles(raw):
    """{name: profile}, validated, with the built-in ones the user didn't override."""
    user = raw.get(PROFILES_KEY) or {}
    if isinstance(user, str):
        try:
            user = json.loads(user)
        except ValueError as exc:
            raise ProfileError(f"{PROFILES_KEY} is not valid JSON ({exc})")
    if not isinstance(user, dict):
        raise ProfileError(f"{PROFILES_KEY} must be an object of name -> profile")
    out = dict(BUILTIN)
    for name, p in user.items():
        if not isinstance(p, dict) or not isinstance(p.get("settings"), dict) or not p["settings"]:
            raise ProfileError(f"profile {name!r} needs a non-empty \"settings\" object")
        extra = set(p) - PROFILE_FIELDS
        if extra:
            raise ProfileError(f"profile {name!r} has unknown fields: {', '.join(sorted(extra))}")
        for k in p["settings"]:
            if not k.startswith("MODEL_PICKER_") or k == PROFILES_KEY:
                raise ProfileError(f"profile {name!r}: {k!r} is not a model-picker setting")
        for hook in ("on_select", "on_leave"):
            if p.get(hook) is not None and not isinstance(p[hook], str):
                raise ProfileError(f"profile {name!r}: {hook} must be a shell command string")
        out[name] = p
    return out


def _effective(key, value):
    """A missing or empty router means the default, the keyword heuristic, on both sides of a comparison."""
    return "heuristic" if key == "MODEL_PICKER_ROUTER" and value in (None, "") else value


def _matches(raw, profile):
    return all(_effective(k, raw.get(k)) == _effective(k, v) for k, v in profile["settings"].items())


def current(raw, profs):
    """The profile whose settings all match the config, or None. When several match: the most specific, and
    the user's own over a built-in one."""
    hits = [n for n, p in profs.items() if _matches(raw, p)]
    return max(hits, key=lambda n: (len(profs[n]["settings"]), n not in BUILTIN or profs[n] is not BUILTIN.get(n))) \
        if hits else None


def _write(path, raw):
    """Atomic write; the previous file is kept as <name>.prev (0600 from the start, never through a symlink).
    A symlinked config is written at its target, so a link into a dotfiles repo keeps working."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        prev = path.with_name(path.name + ".prev")
        fd = os.open(prev, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(fd, 0o600)  # an older .prev may have had other modes: fix them before writing
        with os.fdopen(fd, "wb") as fh:
            fh.write(path.read_bytes())
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return path
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _run_hook(label, cmd):
    """Run an on_select / on_leave command. Its output goes nowhere, so a server it starts in the background
    can't hold the call open; only the shell is waited for. Only the exit code is reported: this text reaches
    Claude, and a command's own output may echo secrets."""
    if not cmd:
        return None
    try:
        proc = subprocess.Popen(cmd, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        return f"{label}: FAILED ({type(exc).__name__})"
    try:
        code = proc.wait(timeout=HOOK_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            proc.kill()
        proc.wait()
        return f"{label}: FAILED (still running after {HOOK_TIMEOUT} s; stopped)"
    return f"{label}: ok" if code == 0 else f"{label}: FAILED (exit {code}; run it by hand to see why)"


def describe(path, environ=None):
    """Text listing of the profiles, marking the current one."""
    raw = read_raw(path)
    profs = profiles(raw)
    cur = current(raw, profs)
    lines = [f"Router profiles (config: {path}):"]
    for name, p in profs.items():
        mark = "*" if name == cur else " "
        desc = f" — {p['description']}" if p.get("description") else ""
        lines.append(f"{mark} {name}{desc}{'  (current)' if name == cur else ''}")
    if cur is None:
        lines.append(f"Current settings match no profile (router: {raw.get('MODEL_PICKER_ROUTER') or 'heuristic'}).")
    if len(profs) == len(BUILTIN) and PROFILES_KEY not in raw:
        lines.append(f"No profiles of your own yet: add {PROFILES_KEY} to the config (see the README).")
    lines += _env_warnings(profs, environ)
    return "\n".join(lines)


def _env_warnings(profs, environ=None):
    environ = os.environ if environ is None else environ
    keys = sorted({k for p in profs.values() for k in p["settings"] if k in environ})
    return [f"Note: {', '.join(keys)} set in the environment override{'s' if len(keys) == 1 else ''} the config file."] if keys else []


def select(path, name, environ=None):
    """Switch to profile `name`. Returns the text to show."""
    raw = read_raw(path)
    profs = profiles(raw)
    if name not in profs:
        raise ProfileError(f"no profile {name!r}; profiles: {', '.join(profs)}")
    before = current(raw, profs)
    if before == name:
        return f"Already on {name!r}; nothing changed."
    new = dict(raw)
    for k, v in profs[name]["settings"].items():
        if v is None:
            new.pop(k, None)
        else:
            new[k] = v
    written = _write(path, new)
    lines = [f"Router profile: {before or 'custom settings'} -> {name}. Saved to {written} (previous file: "
             f"{written.name}.prev next to it).",
             "Takes effect on the next agent launch or prompt; no restart needed."]
    if before and before in profs:
        lines.append(_run_hook(f"on_leave ({before})", profs[before].get("on_leave")))
    lines.append(_run_hook(f"on_select ({name})", profs[name].get("on_select")))
    lines += _env_warnings({name: profs[name]}, environ)
    return "\n".join(line for line in lines if line)
