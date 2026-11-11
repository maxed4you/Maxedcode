"""Settings the desktop window edits, kept in the same files the CLI reads.

    python -m maxedcode.appsettings get            -> {"values", "effective", "server", "files"}
    python -m maxedcode.appsettings set < changes  -> the same, after writing (changes: a JSON object; null clears)

Only what a user should touch: the server address, the model (empty = auto, as in the CLI: the server's only
model), the temperature, the default effort and the shared memory. Each lives where the CLI keeps it, so a
change shows up in both: the server and model in config.json (or settings.json when pinned there), temperature
and effort (plus top_p, top_k, min_p) in state.json (what /sampling and /effort write), the memory in the global MEMORY.md that every CLI
and window session loads. Tuned values stay out on purpose: per-model profiles in model_settings, the context
(read from the server), turn limits and tool switches, which change the cached prompt.
"""
import json
import os
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

from . import config
from .state import EFFORTS

USER_SETTINGS = Path.home() / ".maxedcode" / "settings.json"     # where config.SETTINGS reads the user layer
CONFIG_FILE = config.APP_DIR / "config.json"
MEMORY_FILE = config.MEM_DIR / "MEMORY.md"                      # memory.index_files(): global, every session
MEMORY_CHARS = 20_000
# The servers used last, newest first (the current one + RECENT - 1 before it): switching back without retyping.
# Its own file: the CLI rewrites state.json whole. No keys in it.
RECENT_FILE = config.HOME / "recent_servers.json"
RECENT = 4


def _text(value):
    text = str(value).strip()
    if not text:
        raise ValueError("empty")
    return text


def _unit(value, name):
    number = float(value)
    if not 0 <= number <= 1:
        raise ValueError(f"{name} must be 0..1")
    return number


def _top_k(value):
    number = int(float(value))
    if not 0 <= number <= 1000:
        raise ValueError("must be 0..1000")
    return number


def _temperature(value):
    number = float(value)
    if not 0 <= number <= 2:
        raise ValueError("must be 0..2")
    return number


def _effort(value):
    effort = config.norm_effort(str(value))
    if effort not in EFFORTS:
        raise ValueError("unknown effort")
    return effort


def _bool(value):
    if isinstance(value, bool):
        return value
    if str(value).strip().lower() in ("true", "on", "1", "yes"):
        return True
    if str(value).strip().lower() in ("false", "off", "0", "no"):
        return False
    raise ValueError("true or false")


def _memory(value):
    text = str(value).replace("\r\n", "\n")
    if len(text) > MEMORY_CHARS:
        raise ValueError(f"longer than {MEMORY_CHARS} characters")
    return text


# field -> (where, validator)
FIELDS = {
    "base_url": ("server", _text),
    "model": ("server", _text),
    "api_key": ("server", _text),         # written, never read back to the page (it only learns has_key)
    "server": ("server", _text),          # the server kind (servers.LABELS), set by the first-start picker
    "temperature": ("sampling", _temperature),
    "top_p": ("sampling", lambda v: _unit(v, "top_p")),            # the CLI's /sampling top_p=…
    "top_k": ("sampling", _top_k),
    "min_p": ("sampling", lambda v: _unit(v, "min_p")),
    "effort": ("state", _effort),
    "memory": ("memory", _memory),
    "auto_update": ("user", _bool),       # maxedcode/update.py reads it from settings.json (off: false)
}


def _read(path):
    with suppress(Exception):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    return {}


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def server_models():
    """Model ids the server offers (empty when it does not answer)."""
    from . import servers
    with suppress(Exception):
        base = config.BASE_URL if servers.cloud_kind(config.BASE_URL) else servers.root(config.BASE_URL) + "/v1"
        return servers.model_ids(base, config.API_KEY) or []
    return []


def _same(a, b):
    return str(a or "").rstrip("/").lower() == str(b or "").rstrip("/").lower()


def recent_servers():
    """[{"base_url", "server", "model"}], newest first."""
    with suppress(Exception):
        data = json.loads(RECENT_FILE.read_text(encoding="utf-8"))
        return [x for x in data if isinstance(x, dict) and x.get("base_url")][:RECENT]
    return []


def remember(user=None, cfg=None):
    """Puts the chosen server (settings.json over config.json) first in RECENT_FILE; written only on a change,
    so a server set by the installer or the CLI is kept too, the next time the window reads its settings."""
    user = _read(USER_SETTINGS) if user is None else user
    cfg = _read(CONFIG_FILE) if cfg is None else cfg
    base = user.get("base_url", cfg.get("base_url"))
    if not base or os.environ.get("AGENT_BASE_URL"):
        return
    entry = {"base_url": base, "server": user.get("server", cfg.get("server")) or "",
             "model": user.get("model", cfg.get("model")) or ""}
    old = recent_servers()
    if old and old[0] == entry:
        return
    with suppress(OSError):
        _write(RECENT_FILE, [entry] + [x for x in old if not _same(x["base_url"], base)][:RECENT - 1])


def started_here(base):
    """{"kind", "label"} when the agent's server is the one "Run a model" started last (launch.json "ours"), so
    Connection says where the model comes from and why; None otherwise. Reads the file only, asks nothing."""
    with suppress(Exception):
        from . import launch, servers
        o = launch.ours()
        if o and _same(servers.root(o["base"]), servers.root(base)):
            kind = o.get("server") or o.get("kind") or ""
            return {"kind": kind, "label": servers.LABELS.get(kind, kind), "model": o.get("model")}
    return None


def get():
    user, cfg, state = _read(USER_SETTINGS), _read(CONFIG_FILE), _read(config.STATE_FILE)
    remember(user, cfg)
    memory = ""
    with suppress(OSError):
        memory = MEMORY_FILE.read_text(encoding="utf-8")
    values = {
        "base_url": user.get("base_url", cfg.get("base_url")),
        "model": user.get("model", cfg.get("model")),
        "temperature": (state.get("sampling") or {}).get("temperature"),
        "top_p": (state.get("sampling") or {}).get("top_p"),
        "top_k": (state.get("sampling") or {}).get("top_k"),
        "min_p": (state.get("sampling") or {}).get("min_p"),
        "effort": state.get("effort"),
        "memory": memory,
        "auto_update": user.get("auto_update"),
    }
    models = server_models()
    detected = config.resolve_server()          # what a run does: the window and, unset, the only model
    effective = {
        "base_url": config.BASE_URL,
        "model": detected["model"] if detected["model"] != "local" or models else "",
        "temperature": config.sampling_for(detected["model"]).get("temperature"),
        "top_p": config.sampling_for(detected["model"]).get("top_p"),
        "top_k": config.top_k_for(detected["model"]),
        "min_p": config.min_p_for(detected["model"]),
        "effort": config.norm_effort(state.get("effort") or "medium"),
        "memory": "",
    }
    from . import servers
    key = user.get("api_key", cfg.get("api_key")) or ""
    server = {"online": bool(models), "models": models, "ctx": detected["ctx"], "ctx_source": config.CTX_SRC,
              "has_key": key not in ("", "local"), "key_end": key[-4:] if len(key) > 12 else "",
              "clouds": [[label, url] for label, (_, url) in servers.CLOUD.items()]}
    files = {"config": str(CONFIG_FILE), "state": str(config.STATE_FILE), "memory": str(MEMORY_FILE)}
    # nothing chose a server yet (a git clone started as the window): the page opens the first-start picker
    setup = not (values["base_url"] or os.environ.get("AGENT_BASE_URL"))
    server["kind"] = detected.get("kind", "")
    server["started_here"] = started_here(config.BASE_URL)
    # the ones before the current server; a cloud one needs its key again (keys are not kept here)
    server["recent"] = [{**x, "label": servers.LABELS.get(x["server"], "") or x["server"],
                         "cloud": bool(servers.cloud_kind(x["base_url"]))}
                        for x in recent_servers() if not _same(x["base_url"], values["base_url"])][:RECENT - 1]
    return {"values": values, "effective": effective, "server": server, "files": files, "setup_needed": setup}


def apply(changes):
    """Validate every change first, then write each file once. Raises ValueError naming the bad fields."""
    if not isinstance(changes, dict):
        raise ValueError("changes must be a JSON object")
    bad, clean = [], {}
    for key, value in changes.items():
        if key not in FIELDS:
            bad.append(f"{key}: unknown field")
            continue
        try:
            empty = value is None or (value == "" and key != "memory")
            clean[key] = None if empty else FIELDS[key][1](value)
        except (TypeError, ValueError) as error:
            bad.append(f"{key}: {error}")
    if bad:
        raise ValueError("; ".join(bad))
    user, cfg, state = _read(USER_SETTINGS), _read(CONFIG_FILE), _read(config.STATE_FILE)
    if clean.get("base_url"):
        # any address: "192.168.0.10", "pc:1234", ".../chat/completions" -> the URL that answers
        from . import servers
        key = clean.get("api_key") or user.get("api_key", cfg.get("api_key")) or ""
        clean["base_url"] = servers.find_base(clean["base_url"], key) or clean["base_url"]
        if "server" not in clean:       # a new server: its kind is found again (a cloud one at once)
            clean["server"] = servers.cloud_kind(clean["base_url"]) or None
    touched = set()
    for key, value in clean.items():
        where = FIELDS[key][0]
        if where == "memory":
            _write(MEMORY_FILE, value or "")
            continue
        if where == "server":
            # settings.json wins over config.json (config._get), so a value pinned there is edited there
            target, name = (user, "user") if key in user else (cfg, "cfg")
        elif where == "user":
            target, name = user, "user"
        elif where == "sampling":
            target, name = state.setdefault("sampling", {}), "state"
        else:
            target, name = state, "state"
        touched.add(name)
        if value is None:
            target.pop(key, None)
        else:
            target[key] = value
    if "user" in touched:
        _write(USER_SETTINGS, user)
    if "cfg" in touched:
        _write(CONFIG_FILE, cfg)
    if touched & {"user", "cfg"}:
        remember(user, cfg)
    if "state" in touched:
        _write(config.STATE_FILE, state)


def main(argv):
    command = argv[1] if len(argv) > 1 else "get"
    if command == "set":
        try:
            apply(json.loads(sys.stdin.read() or "{}"))
        except (ValueError, json.JSONDecodeError) as error:
            print(json.dumps({"error": str(error)}, ensure_ascii=False))
            return 1
        # The effective values are computed when config is imported: read them in a fresh process.
        return subprocess.run([sys.executable, "-m", "maxedcode.appsettings", "get"]).returncode
    print(json.dumps(get(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
