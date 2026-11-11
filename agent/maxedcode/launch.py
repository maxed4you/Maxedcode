"""Start and stop a model server on this PC the way each user starts it, in one click.

The window (Settings -> Models, the model menu under the input) and the CLI (/local) use it. Every program
has its ways to start (catalog()["servers"][i]["ways"]), and each user keeps their own:

* builtin  - a model and a context, plus this program's own settings (OPTIONS: GPU offload, cache format,
             parallel slots, pictures, own flags, environment), saved per program:
             Strata `strata launch --model <size> --context <n> --no-tui --yes`; LM Studio `lms load <model>
             --context-length <n> --parallel 1`; Ollama OLLAMA_CONTEXT_LENGTH and its other variables (a request
             through /v1 cannot set the window: Ollama reloads the model with its default one), the server
             restarted when it serves other values; llama.cpp `llama-server -m <file.gguf> -c <n>` (GGUF files
             found in LM Studio's folder, the Hugging Face and llama.cpp caches, ~/models).
* preset   - what the program itself saved: Strata's presets (`strata launch --list-presets`).
* script   - a start script or a command line, run as it is in a console window of its own, then the server is
             awaited at its address: found ones (<strata>\\launcher\\bat\\*.bat) and the user's own ("+ own
             way"), for any program - also for the ones with no builtin start ("other": KoboldCpp, vLLM,
             Jan, text-generation-webui, anything with an OpenAI-compatible API).

    python -m maxedcode.launch get                  -> {"servers": [...], "last": kind}
    python -m maxedcode.launch start < {"kind", "way", "model", "ctx", "opts"}
                                                    -> progress lines {"text", "pct"?}, the last one
                                                       {"done": true, "ok", "base", "model", "ctx", "error"}
    python -m maxedcode.launch stop < {"kind", "base"?}  -> {"ok", "text"}
    python -m maxedcode.launch set < {"do": "way-save" | "way-remove", ...}  -> get's answer

A started server is chosen for the agent at once (appsettings: base_url, server, model). Standard library only,
apart from maxedcode.servers (and appsettings / config when a start is saved).
"""
import collections
import concurrent.futures as cf
import json
import os
import re
import secrets
import shlex
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
from contextlib import suppress
from pathlib import Path

from . import servers

KINDS = ("strata", "lmstudio", "ollama", "llamacpp")     # the ones with a builtin start
OTHER = "other"                                          # any other server: own ways only
DOWNLOAD = {"strata": "https://github.com/Niko1221/Strata", "lmstudio": "https://lmstudio.ai/download",
            "ollama": "https://ollama.com/download", "llamacpp": "https://github.com/ggml-org/llama.cpp/releases"}
# what each one is, for someone who has none of them yet
ABOUT = {"strata": "fast engine for big MoE models on one GPU",
         "lmstudio": "an app with a model catalog: the easiest start",
         "ollama": "a small background service: `ollama pull <model>`",
         "llamacpp": "llama-server and a .gguf file",
         OTHER: "KoboldCpp, vLLM, Jan, text-generation-webui or any server with an OpenAI-compatible API"}
CTX_STEPS = (16384, 32768, 65536, 131072, 262144)
DEFAULT_CTX = 32768
WAIT = {"strata": 900, "lmstudio": 900, "ollama": 600, "llamacpp": 600, OTHER: 900}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | NO_WINDOW


def _o(key, label, choices, hint=None, default="", flag=None, env=None):
    """One setting of a builtin start: label / hint / choice names are [ru, en] (the page picks one)."""
    return {"key": key, "label": label, "hint": hint, "choices": choices, "default": default, "flag": flag, "env": env}


AS_IS = ["как обычно", "as usual"]
# The settings of each builtin start (Settings -> Models -> More settings): a choice "" keeps the program's own
# default. "args" and "env" (free text) are added for every program but Ollama (env only: `ollama serve` has no
# model flags).
OPTIONS = {
    "strata": [
        _o("vision", ["Картинки", "Pictures"], [["", AS_IS], ["off", ["выкл — меньше памяти", "off: less memory"]],
                                                 ["cpu", ["читает процессор", "read by the CPU"]],
                                                 ["gpu", ["читает видеокарта", "read by the GPU"]]],
           ["Может ли модель смотреть картинки. Выкл — больше памяти под текст.", "Whether the model reads pictures. Off leaves more memory for text."],
           flag="--vision"),
        _o("kv", ["Память разговора", "Conversation memory"],
           [["", AS_IS], ["q4_0", ["q4_0 — вдвое меньше памяти", "q4_0: half the memory"]], ["int8", ["int8", "int8"]],
            ["k8v4", ["k8v4", "k8v4"]], ["fp16", ["fp16 — точнее, больше памяти", "fp16: exact, more memory"]]],
           ["Формат кэша контекста (--kv).", "The context cache format (--kv)."], flag="--kv"),
        _o("drafting", ["Ускорение черновиком", "Drafting"],
           [["", AS_IS], ["mtp", ["mtp", "mtp"]], ["dflash2", ["dflash2", "dflash2"]], ["off", ["выкл", "off"]]],
           ["Как модель угадывает следующие слова наперёд (--drafting).", "How the model guesses the next words ahead (--drafting)."],
           flag="--drafting"),
        _o("parallel", ["Запросов одновременно", "Requests at once"], [["", AS_IS], ["1", ["1", "1"]], ["2", ["2", "2"]], ["4", ["4", "4"]]],
           ["Агенту хватает одного; больше — больше памяти.", "One is enough for the agent; more takes more memory."], flag="--parallel"),
    ],
    "lmstudio": [
        _o("gpu", ["Видеокарта", "GPU offload"], [["", ["авто", "auto"]], ["max", ["вся модель на видеокарте", "the whole model on the GPU"]],
                                                    ["off", ["только процессор", "the CPU only"]]],
           ["Сколько модели держать в видеопамяти (--gpu).", "How much of the model goes to the graphics memory (--gpu)."], flag="--gpu"),
        _o("parallel", ["Запросов одновременно", "Requests at once"], [["1", ["1 — для агента", "1: for the agent"]], ["2", ["2", "2"]], ["4", ["4", "4"]]],
           ["LM Studio по умолчанию держит 4 — это вчетверо больше памяти под кэш.", "LM Studio keeps 4 by default: four times the cache memory."],
           default="1", flag="--parallel"),
    ],
    "ollama": [
        _o("flash", ["Flash attention", "Flash attention"], [["", AS_IS], ["1", ["вкл", "on"]], ["0", ["выкл", "off"]]],
           ["Быстрее и меньше памяти на длинном контексте.", "Faster and less memory on a long context."], env="OLLAMA_FLASH_ATTENTION"),
        _o("kv", ["Память разговора", "Conversation memory"],
           [["", ["f16 — как обычно", "f16: as usual"]], ["q8_0", ["q8_0 — вдвое меньше памяти", "q8_0: half the memory"]],
            ["q4_0", ["q4_0 — вчетверо меньше", "q4_0: a quarter"]]],
           ["Формат кэша контекста; нужен flash attention.", "The context cache format; needs flash attention."], env="OLLAMA_KV_CACHE_TYPE"),
        _o("parallel", ["Запросов одновременно", "Requests at once"], [["1", ["1 — для агента", "1: for the agent"]], ["2", ["2", "2"]], ["4", ["4", "4"]]],
           ["Каждый слот берёт свой кэш контекста.", "Every slot takes its own context cache."], default="1", env="OLLAMA_NUM_PARALLEL"),
        _o("keep", ["Держать модель в памяти", "Keep the model loaded"],
           [["30m", ["30 минут", "30 minutes"]], ["2h", ["2 часа", "2 hours"]], ["-1", ["всегда", "always"]]],
           ["Иначе Ollama выгружает её через 5 минут простоя и кэш пропадает.", "Otherwise Ollama unloads it after 5 idle minutes and the cache is lost."],
           default="30m", env="OLLAMA_KEEP_ALIVE"),
    ],
    "llamacpp": [
        _o("ngl", ["Слои на видеокарте", "Layers on the GPU"], [["", ["авто", "auto"]], ["999", ["все", "all"]], ["0", ["ни одного — процессор", "none: the CPU"]]],
           ["-ngl", "-ngl"], flag="-ngl"),
        _o("fa", ["Flash attention", "Flash attention"], [["", ["авто", "auto"]], ["on", ["вкл", "on"]], ["off", ["выкл", "off"]]], ["-fa", "-fa"], flag="-fa"),
        _o("kv", ["Память разговора", "Conversation memory"],
           [["", ["f16 — как обычно", "f16: as usual"]], ["q8_0", ["q8_0 — вдвое меньше памяти", "q8_0: half the memory"]],
            ["q4_0", ["q4_0 — вчетверо меньше", "q4_0: a quarter"]]], ["-ctk / -ctv", "-ctk / -ctv"]),
        _o("parallel", ["Запросов одновременно", "Requests at once"], [["1", ["1 — для агента", "1: for the agent"]], ["2", ["2", "2"]], ["4", ["4", "4"]]],
           ["-np: контекст делится между слотами.", "-np: the context is split between the slots."], default="1", flag="-np"),
        _o("vision", ["Картинки (mmproj)", "Pictures (mmproj)"], [["", ["если есть mmproj рядом", "when an mmproj is next to it"]], ["off", ["выкл", "off"]]]),
    ],
}
TEXT_OPTS = {"args": {"key": "args", "type": "text", "label": ["Свои флаги", "Own flags"],
                      "hint": ["Добавляются к команде запуска как есть.", "Added to the start command as they are."]},
             "env": {"key": "env", "type": "text", "label": ["Переменные окружения", "Environment"],
                     "hint": ["ИМЯ=значение через пробел.", "NAME=value, space separated."]}}


def options(kind):
    extra = ["env"] if kind == "ollama" else ["args", "env"]
    return OPTIONS.get(kind, []) + [TEXT_OPTS[k] for k in extra] if kind in KINDS else []


def opt(opts, kind, key):
    """A chosen value, or the option's default ("" = the program's own)."""
    o = next((x for x in OPTIONS.get(kind, []) if x["key"] == key), None)
    v = (opts or {}).get(key)
    return str(v) if v not in (None, "") else (o or {}).get("default", "") if o else ""


def opt_flags(kind, opts, skip=()):
    """The flags of the chosen settings that are plain flags, plus the own flags."""
    out = []
    for o in OPTIONS.get(kind, []):
        v = opt(opts, kind, o["key"])
        if o.get("flag") and v and o["key"] not in skip:
            out += [o["flag"], v]
    with suppress(ValueError):
        out += shlex.split(str((opts or {}).get("args") or ""), posix=os.name != "nt")
    return out


def opt_env(kind, opts):
    """Environment of the chosen settings (options with env, plus the free text NAME=value ...)."""
    env = {o["env"]: opt(opts, kind, o["key"]) for o in OPTIONS.get(kind, []) if o.get("env") and opt(opts, kind, o["key"])}
    for part in str((opts or {}).get("env") or "").split():
        name, sep, value = part.partition("=")
        if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            env[name] = value
    return env


# ---------- own state: the last way and model per program, own ways, started processes ----------

def home():
    with suppress(Exception):
        from . import config
        return Path(config.HOME)
    return Path.home() / ".maxedcode"


def state_file():
    return home() / "launch.json"


MAPS = ("model", "ctx", "way", "opts", "pid", "paths", "applied")     # launch.json fields that are objects
LISTS = ("ways", "kobold", "gguf_dirs", "lms_models", "scripts")         # ... and lists


def load_state():
    """launch.json; a field of the wrong type (a hand edit, a cut write) is dropped, not a crash of every start."""
    with suppress(Exception):
        data = json.loads(state_file().read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            for key in MAPS + LISTS + ("ours",):
                want = list if key in LISTS else dict
                if key in data and not isinstance(data[key], want):
                    data.pop(key)
            return data
    return {}


def save_state(data):
    path = state_file()
    # a temporary file of its own per writer: the window, a start job and the CLI write at the same moment, and
    # one shared launch.tmp was rewritten under another's replace (a write lost, or a cut file)
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for _ in range(5):          # Windows refuses a replace while another process reads the file: retried
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.05)
    except OSError:
        pass
    finally:
        with suppress(OSError):
            tmp.unlink()


def remember(kind, model=None, ctx=None, way=None, opts=None, pid=None, script=None, **extra):
    """The last start of a program: its way, model, the context per model, its settings."""
    st = load_state()
    st["kind"] = kind
    if model is not None:
        st.setdefault("model", {})[kind] = model
    if ctx and model:
        st.setdefault("ctx", {})[f"{kind}|{model}"] = int(ctx)
    if script:                      # a script picked by path: kept as an own way
        way = way or save_way(kind, Path(script).stem, script, st=st)["id"]
    if way:
        st.setdefault("way", {})[kind] = way
    if opts is not None:
        st.setdefault("opts", {})[kind] = {k: v for k, v in opts.items() if v not in (None, "")}
    if pid is not None:
        st.setdefault("pid", {})[kind] = pid
    st.update(extra)
    save_state(st)


def own_ways(st=None):
    st = load_state() if st is None else st
    # the picked files of the first version ("scripts", Strata only) are own ways now
    for path in st.pop("scripts", None) or []:
        st.setdefault("ways", []).append({"id": "own:" + secrets.token_hex(4), "kind": "strata", "type": "script",
                                          "name": Path(path).stem, "command": path, "base": ""})
    return [w for w in st.get("ways") or [] if isinstance(w, dict) and w.get("command")]


def norm_base(addr, kind):
    """An address typed in any form -> http://host:port/v1 (the program's default port when none is given)."""
    addr = str(addr or "").strip()
    if not addr:
        return servers.default_base(kind) if kind in KINDS else ""
    if addr.startswith(":") or addr.isdigit():      # ":5001" / "5001": a port on this PC
        addr = "127.0.0.1:" + addr.lstrip(":")
    p = servers.split_addr(addr)
    if not p:
        return addr
    scheme, host, port, path = p
    port = port or (port_of(kind) if kind in KINDS else 8080)
    return f"{scheme}://{host}:{port}{path or '/v1'}"


def save_way(kind, name, command, base="", way_id=None, st=None):
    """Adds or changes an own way to start a program: a script file or a command line, and its address."""
    own = st is not None
    st = load_state() if st is None else st
    ways = own_ways(st)
    w = next((x for x in ways if x["id"] == way_id), None) if way_id else None
    if not w:
        w = next((x for x in ways if x["kind"] == kind and x["command"] == command), None)
    if not w:
        w = {"id": "own:" + secrets.token_hex(4), "kind": kind, "type": "script"}
        ways.append(w)
    w.update({"name": str(name or Path(str(command).strip('"')).stem or "my start")[:60], "command": str(command).strip(),
              "base": norm_base(base, kind) if base else ""})
    st["ways"] = ways
    if not own:
        save_state(st)
    return w


def remove_way(way_id):
    st = load_state()
    st["ways"] = [w for w in own_ways(st) if w["id"] != way_id]
    for kind, w in list((st.get("way") or {}).items()):
        if w == way_id:
            st["way"].pop(kind)
    save_state(st)


# What this app started (the last start from here): {"kind" (the program tab), "server" (the kind that answered),
# "base", "model" (LM Studio / Ollama: the model loaded from here), "serve" (the server process itself was
# started from here), "pid" (the process listening on its port then)}. A new start from here closes it first;
# anything the user started by hand is never closed or unloaded by a start (only by their own Stop).
def ours(st=None):
    o = (load_state() if st is None else st).get("ours")
    return o if isinstance(o, dict) and o.get("base") else None


def _port(base):
    with suppress(ValueError):
        return urllib.parse.urlsplit(str(base)).port
    return None


def mine(kind=None, base=None):
    """The record of this app's start when it is the one running now (same program or address, and the same
    process on its port: a server the user started again by hand is theirs), else None."""
    o = ours()
    if not o:
        return None
    if kind and kind not in (o.get("kind"), o.get("server")) and not base:
        return None
    if base and str(base).rstrip("/").lower() != str(o["base"]).rstrip("/").lower():
        return None
    if servers.port_down(o["base"]):
        return None
    pid = o.get("pid")
    return o if not pid or listener(_port(o["base"])) == pid else None


def keep_ours(record):
    """Remembers the server a start from here brought up (see ours())."""
    st = load_state()
    st["ours"] = {**record, "pid": listener(_port(record["base"])) if _port(record["base"]) else None}
    save_state(st)


def forget_ours(kind=None, base=None):
    st = load_state()
    o = ours(st)
    if o and (not kind or kind in (o.get("kind"), o.get("server"))) and \
            (not base or str(base).rstrip("/").lower() == str(o["base"]).rstrip("/").lower()):
        st.pop("ours", None)
        save_state(st)


def release(o, say):
    """Closes what a start from here brought up, and only that: LM Studio / Ollama unload the model loaded from
    here (the user's own models stay) and their server stops only when it was started from here and holds
    nothing else; Strata, llama.cpp and script servers are this app's whole: stopped."""
    kind = o.get("server") or o.get("kind")
    base = o["base"]
    label = servers.LABELS.get(kind, kind)
    say(f"closing {label} started from here before")
    if kind == "lmstudio":
        lms = lms_cli()
        if lms and o.get("model"):
            run([lms, "unload", o["model"]], 120)
        if lms and o.get("serve") and not servers.loaded_models(base, kind="lmstudio"):
            run([lms, "server", "stop"], 60)
        forget_ours()
        return True
    if kind == "ollama":
        r = servers.root(base)
        if o.get("model"):
            with suppress(Exception):
                servers.http(r + "/api/generate", body={"model": o["model"], "keep_alive": 0}, timeout=60)
        if o.get("serve") and not servers.loaded_models(base, kind="ollama"):
            ollama_kill()
        forget_ours()
        return True
    ok = stop(o.get("kind") if o.get("kind") in STOP else kind, say, base=base)["ok"]
    forget_ours()
    return ok


def outside(label, port):
    """The words for a server the user started by hand on the port a start needs."""
    return _fail(f"{label} already runs on port {port}, started outside maxedcode: it is left alone. Close it "
                 "yourself, or just use it (Settings -> Connection)")


# Program files looked for on the disks ("Find programs"); the user can also pick one ("Where is it…").
EXE = ".exe" if os.name == "nt" else ""
PROGRAM_FILES = {"llama-server" + EXE: "llamacpp", "ollama" + EXE: "ollama", "lms" + EXE: "lmstudio", "strata.py": "strata"}
SKIP_DIRS = {"windows", "$recycle.bin", "system volume information", "node_modules", ".git", "__pycache__",
             "winsxs", "steamapps", "site-packages", ".cache", "temp", "tmp", "proc", "sys", "dev", "snap",
             "windowsapps", "packages", "$windows.~bt", "$windows.~ws", "msocache", "recovery", "perflogs",
             "microsoft", "windows defender", "windows kits", "dotnet", "assembly", "installer", "crashdumps",
             "inetcache", "cache", "caches", "logs", "lib", "include", "share", "locale", "locales", "icons", "fonts"}


def saved_path(kind):
    """A program file the user picked or "Find programs" found, while it is still there."""
    path = (load_state().get("paths") or {}).get(kind)
    return path if path and Path(path).exists() else None


def strata_folder(path):
    """A Strata checkout / terminal install from any file or folder in it (strata.py next to bin\\strata.cmd)."""
    for d in [Path(path)] + list(Path(path).parents)[:3]:
        if (d / "strata.py").is_file() and any((d / "bin" / x).is_file() for x in ("strata.cmd", "strata", "strata.sh")):
            return d
    return None


def check_path(kind, path):
    """The file to keep for a program, from what the user picked: llama-server(.exe), ollama(.exe), lms(.exe)
    (or LM Studio.exe: its lms is next to the user's .lmstudio), any file of a Strata folder. -> path | None."""
    path = Path(str(path or "").strip().strip('"'))
    if not path.exists():
        return None
    name = path.name.lower()
    if kind == "strata":
        d = strata_folder(path)
        return str(d) if d else None
    if kind == "lmstudio" and name.startswith("lm studio"):
        return lms_cli() and str(path)
    want = {"llamacpp": "llama-server", "ollama": "ollama", "lmstudio": "lms"}.get(kind)
    return str(path) if want and path.is_file() and path.stem.lower() == want else None


def set_path(kind, path):
    good = check_path(kind, path)
    if good:
        st = load_state()
        st.setdefault("paths", {})[kind] = good
        save_state(st)
    return good


def find_programs(seconds=30, roots=None):
    """Looks for the model programs on this PC's disks (the program folders, the home folder, then each drive
    a few levels deep) for at most `seconds`: -> {kind: path} of the ones found where the usual places did not
    have them, kept in launch.json. KoboldCpp files become a found way of "other"."""
    end = time.monotonic() + seconds
    given, roots = roots, []
    if given:
        roots = list(given)
    elif os.name == "nt":
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA"):
            if os.environ.get(env):
                roots.append((Path(os.environ[env]), 4))
        roots.append((Path.home(), 4))
        roots += [(Path(f"{d}:\\"), 4) for d in "CDEFGHIJ" if Path(f"{d}:\\").exists()]
    else:
        roots += [(Path.home(), 5), (Path("/opt"), 4), (Path("/usr/local"), 3), (Path("/Applications"), 4)]
    found, kobold, seen = {}, [], set()

    def walk(root, depth):
        # breadth first: the shallow folders (where programs usually are) before the deep ones
        queue = collections.deque([(root, 0)])
        while queue and time.monotonic() < end and len(found) < len(set(PROGRAM_FILES.values())):
            folder, level = queue.popleft()
            key = str(folder).lower()
            if key in seen:
                continue
            seen.add(key)
            with suppress(OSError):
                for entry in os.scandir(folder):
                    name = entry.name.lower()
                    with suppress(OSError):
                        if entry.is_dir(follow_symlinks=False):
                            if level < depth and name not in SKIP_DIRS and not name.startswith((".", "$")):
                                queue.append((Path(entry.path), level + 1))
                        elif name in PROGRAM_FILES:
                            kind = PROGRAM_FILES[name]
                            if kind not in found:
                                good = check_path(kind, entry.path)
                                if good:
                                    found[kind] = good
                        elif name.startswith("koboldcpp") and name.endswith(EXE or ".py") and len(kobold) < 4:
                            kobold.append(entry.path)
    # every root at once: the disks are read in parallel (one slow drive no longer holds the others)
    with cf.ThreadPoolExecutor(len(roots) or 1) as pool:
        list(pool.map(lambda r: walk(*r), roots))
    st = load_state()
    have = {"llamacpp": shutil.which("llama-server"), "ollama": ollama_cli(), "lmstudio": lms_cli(),
            "strata": str(servers.strata_source() or "") or None}
    st.setdefault("paths", {}).update({k: v for k, v in found.items() if not have.get(k)})
    st["kobold"] = kobold
    save_state(st)
    return {k: v for k, v in found.items() if not have.get(k)}, kobold


def log_file(kind):
    d = home() / "launch"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{kind}.log"


def tail(path, lines=6):
    with suppress(OSError):
        text = Path(path).read_text(encoding="utf-8", errors="replace").replace("\r", "\n")
        return [x.strip() for x in text.splitlines() if x.strip()][-lines:]
    return []


# ---------- contexts ----------

def ctx_options(max_ctx=None, *extra):
    """The context sizes offered: the usual steps up to what the model can take, plus its own and the last one."""
    out = {c for c in CTX_STEPS if not max_ctx or c <= max_ctx}
    out |= {int(x) for x in (max_ctx, *extra) if x}
    return sorted(c for c in out if c >= servers.MIN_WINDOW) or [servers.MIN_WINDOW]


def default_ctx(kind, model, st=None):
    """The last context used with this model, else what its setup chose, else 32K (or less when it cannot)."""
    st = load_state() if st is None else st
    last = (st.get("ctx") or {}).get(f"{kind}|{model.get('id')}")
    if last:
        return int(last)
    if model.get("ctx"):
        return int(model["ctx"])
    top = model.get("max_ctx")
    return min(DEFAULT_CTX, top) if top and top >= servers.MIN_WINDOW else DEFAULT_CTX


# ---------- processes ----------

def run(cmd, timeout=60, **kw):
    """A command's (exit code, output); never raises (a missing program is exit 127)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, **kw)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        return 124, str(e.stdout or "")
    except OSError as e:
        return 127, str(e)


def spawn(cmd, log=None, env=None, cwd=None, console=False):
    """Starts a server that outlives this process (its own console when it asks questions or shows progress)."""
    out = open(log, "ab") if log else subprocess.DEVNULL
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if console and os.name == "nt" else DETACHED
    try:
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT if log else out,
                                env=env, cwd=cwd, close_fds=True,
                                creationflags=flags if os.name == "nt" else 0,
                                **({} if os.name == "nt" else {"start_new_session": True}))
    finally:
        if log:
            out.close()


def listener(port):
    """The PID listening on a local TCP port, or None."""
    if os.name == "nt":
        code, out = run(["netstat", "-ano", "-p", "TCP"], 15)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[1].rsplit(":", 1)[-1] == str(port):
                with suppress(ValueError):
                    return int(parts[4])
        return None
    code, out = run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], 10)
    with suppress(ValueError, IndexError):
        return int(out.split()[0])
    return None


def kill(pid):
    if not pid:
        return
    if os.name == "nt":
        run(["taskkill", "/PID", str(pid), "/T", "/F"], 30)
        return
    import signal
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with suppress(OSError):
            os.killpg(os.getpgid(pid), sig)
        with suppress(OSError):
            os.kill(pid, sig)
        time.sleep(1.5)
        if not alive(pid):
            return


def alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        code, out = run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], 10)
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def port_of(kind):
    return next((p for p, k in servers.PORTS.items() if k == kind), 8080)


def free_port(kind, say):
    """Another model server on the port this one needs (Strata and llama.cpp share 8080): stop it first."""
    port = port_of(kind)
    base = f"http://127.0.0.1:{port}/v1"
    if servers.port_down(base):
        return True
    other = servers.kind_of(base, timeout=2)
    if other == kind:
        return True
    if other not in KINDS:          # not a model server this file knows: never stopped from here
        say(f"port {port} is taken by another program (PID {listener(port) or '?'}): close it first")
        return False
    o = mine(base=base)
    if not o:                       # started by hand: left alone
        say(f"port {port} is taken by {servers.LABELS.get(other, other)}, started outside maxedcode: close it first")
        return False
    release(o, say)
    return wait_down(base, 30)


def make_room(base, label, say):
    """The same program on the port the start needs: closed when it was started from here, then the port must be
    free before the new one starts - else the old server's answer looked like the new one was ready at once (the
    window said "loaded" while the new model had not even begun). -> None, or the failure to return."""
    if servers.port_down(base):
        return None
    o = mine(base=base)
    if not o:
        return outside(label, _port(base))
    release(o, say)
    if not wait_down(base, 30):
        return _fail(f"the {label} started from here before still answers on port {_port(base)}: close its "
                     "window, then try again")
    return None


def wait_down(base, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if servers.port_down(base):
            return True
        time.sleep(1)
    return servers.port_down(base)


def wait_up(base, seconds, say, proc=None, log=None, what="the server", check=None):
    """Polls until the server answers (check(), else /v1/models) -> True; False on time out or when proc died."""
    start = time.monotonic()
    shown = ""
    while time.monotonic() - start < seconds:
        ok = False
        with suppress(Exception):
            ok = check() if check else servers.model_ids(base, timeout=2) is not None
        if ok:
            return True
        if proc is not None and proc.poll() is not None:     # the server itself ended before it was ready
            return False
        last = (tail(log, 1) or [""])[-1] if log else ""
        text = f"waiting for {what}… {int(time.monotonic() - start)} s" + (f" · {last[:140]}" if last else "")
        if text != shown:
            say(text)
            shown = text
        time.sleep(1.5)
    return False


def _ready(kind, base):
    """The server answers, and Strata's engine runs (its API answers before the model is in memory). One that
    asks for a key (401/403) is up too: the key is the Connection tab's business, not a start that never ends."""
    try:
        if servers.model_ids(base, timeout=2) is None:
            return False
    except urllib.error.HTTPError:
        return True
    if kind == "strata" or servers.kind_of(base, timeout=2) == "strata":
        return (strata_health(base) or {"loaded": False}).get("loaded") is not False
    return True


def console(command, cwd=None, title="maxedcode"):
    """Runs a start script or a command line as it is, in a console window of its own (the user sees what it
    prints and answers its questions); macOS / Linux: in the background, its output in a log."""
    path = Path(str(command).strip().strip('"'))
    is_file = path.is_file()
    cwd = cwd or (str(path.parent) if is_file else None)
    if os.name == "nt":
        title = title.replace('"', "'")
        if is_file and path.suffix.lower() == ".ps1":
            line = f'cmd /c start "{title}" powershell -NoExit -ExecutionPolicy Bypass -File "{path}"'
        else:
            # cmd /k keeps the window when the server ends with an error; ""…"" survives spaces in the path
            line = f'cmd /c start "{title}" cmd /k ""{path}""' if is_file else f'cmd /c start "{title}" cmd /k {command}'
        return spawn(line, cwd=cwd)
    cmd = ["sh", str(path)] if is_file and not os.access(path, os.X_OK) else [str(path)] if is_file else ["sh", "-c", command]
    return spawn(cmd, log=log_file("script"), cwd=cwd)


def script_start(kind, way, say):
    """A start script or command (found or the user's own): run as it is, then wait for the server at its
    address. The program that answers is asked which it is (an "other" way may be KoboldCpp, vLLM …)."""
    command = str(way.get("command") or "").strip()
    if not command:
        return _fail("this way has no command")
    path = Path(command.strip('"'))
    # a path (quoted, or one word with a folder in it) must exist; anything else is a command line
    quoted = command.startswith('"') and command.endswith('"') and command.count('"') == 2
    if (quoted or (" " not in command and ("\\" in command or "/" in command))) and not path.is_file():
        return _fail(f"the start script is not there: {command}")
    base = way.get("base") or (servers.default_base(kind) if kind in KINDS else "")
    if not base:
        return _fail("the address of this server is not set: edit the way and give it (e.g. 127.0.0.1:5001)")
    if kind in KINDS and _port(base) == port_of(kind):
        if not free_port(kind, say):
            return _fail(f"port {port_of(kind)} is still taken")
        busy = make_room(base, servers.LABELS[kind], say)    # the same program runs: closed only when ours
        if busy:
            return busy
    elif not servers.port_down(base) and _ready(kind, base):
        say("it is already running")
        return _done(servers.kind_of(base, timeout=2) or kind, base, None)
    say(f"starting {way.get('name') or path.name} in its own window")
    label = servers.LABELS.get(kind) if kind in KINDS else "server"
    console(command, title=f"{label} · {way.get('name') or path.stem}")
    remember(kind, way=way.get("id"))
    if not wait_up(base, WAIT.get(kind, 900), say, what=f"{label} at {base} (its window shows the progress)",
                   log=None if os.name == "nt" else log_file("script"), check=lambda: _ready(kind, base)):
        return _fail(f"nothing answered at {base}: see the script's window")
    real = servers.kind_of(base, timeout=2) or (kind if kind in KINDS else "")
    model = None
    if real in servers.JIT_KINDS:
        on = servers.loaded_models(base, kind=real) or []
        model = on[0][0] if on else None
    return _done(real or kind, base, model)


# ---------- Strata ----------

def strata_cli():
    """[python, strata.py] with the Python bin\\strata.cmd would pick (STRATA_PYTHON, the bundled runtime, the
    source checkout's .venv), else the strata command. Not the .cmd itself: a batch file run without a console
    ended with "Terminate batch job (Y/N)?" while `strata launch` handed the server over, an exit code that
    looked like a failed start (and the engine went with it)."""
    src = servers.strata_source() or (Path(saved_path("strata")) if saved_path("strata") else None)
    if src:
        win = os.name == "nt"
        for py in (servers.env_value("STRATA_PYTHON"), src / "python" / ("python.exe" if win else "bin/python3"),
                   src / ".venv" / ("Scripts/python.exe" if win else "bin/python")):
            if py and Path(py).is_file():
                return [str(py), str(src / "strata.py")], src
        exe = src / "bin" / ("strata.cmd" if win else "strata")
        if exe.is_file():
            return [str(exe)], src
    on_path = shutil.which("strata")
    return ([on_path], None) if on_path else (None, None)


def strata_health(base):
    """Strata's /health: "loaded" is whether its engine runs (the API answers before the model is in memory)."""
    h = servers.get(servers.root(base) + "/health", timeout=2)
    return h if isinstance(h, dict) else None


def strata_starting(cli, src):
    """`strata launch --status`: a launch that is starting or ready (it runs on after the command returns)."""
    code, out = run([*cli, "launch", "--status", "--output", "json"], 30, cwd=str(src) if src else None)
    return any(x.get("state") in ("starting", "ready") for x in (_json(out) or {}).get("running") or []
               if isinstance(x, dict))


def strata_models():
    cli, _ = strata_cli()
    if not cli:
        return []
    code, out = run([*cli, "models", "list", "--installed", "--output", "json"], 60)
    models = []
    for m in (_json(out) or {}).get("models") or []:
        if isinstance(m, dict) and m.get("id") and m.get("component_status", "present") == "present":
            models.append({"id": str(m["id"]), "name": str(m.get("name") or m["id"]), "ctx": m.get("context_tokens"),
                           "max_ctx": None, "size": None})
    return models


def strata_presets():
    """The presets saved in Strata itself (`strata launch --save-preset NAME`, shared with the Strata app)."""
    cli, src = strata_cli()
    if not cli:
        return []
    code, out = run([*cli, "launch", "--list-presets", "--output", "json"], 60, cwd=str(src) if src else None)
    names = []
    for p in (_json(out) or {}).get("presets") or []:
        name = p.get("name") if isinstance(p, dict) else p
        if name:
            names.append(str(name))
    return names


def strata_scripts():
    """Start scripts a launcher wrote into the Strata folder (<strata>\\launcher\\bat\\*.bat, launcher/sh)."""
    _, src = strata_cli()
    found = []
    if src:
        for d in (src / "launcher" / "bat", src / "launcher" / "sh"):
            with suppress(OSError):
                found += sorted(str(p) for p in d.iterdir() if p.suffix.lower() in (".bat", ".cmd", ".sh", ".ps1"))
    return found


def _json(text):
    """The JSON object in a command's output (it may print lines before it)."""
    text = str(text or "")
    with suppress(ValueError):
        return json.loads(text[text.index("{"):])
    return None


def strata_flags(cli, model, ctx):
    """`--model <size>`; a size from another family ("coder-iq1_m") is `--family coder --model iq1_m`: the plan
    of the plain name is asked first, so a size with a dash of its own (ud-q4_k_xl) stays whole."""
    plain = ["--model", model]
    if "-" not in model:
        return plain
    code, out = run([*cli, "launch", "--plan", *plain, "--context", str(ctx), "--no-tui", "--yes",
                     "--output", "json"], 60)
    if (_json(out) or {}).get("ok"):
        return plain
    family, size = model.split("-", 1)
    return ["--family", family, "--model", size]


def strata_start(model, ctx, say, opts=None, preset=None):
    base = servers.default_base("strata")
    log = log_file("strata")
    cli, src = strata_cli()
    if not cli:
        return _fail("the strata command is not found: install Strata, or add your own way to start it")
    if not preset and not model:
        return _fail("pick a model")
    if not free_port("strata", say):
        return _fail("port 8080 is still taken")
    busy = make_room(base, "Strata", say)      # another model or context: closed only when started from here
    if busy:
        return busy
    if preset:
        args = ["--preset", preset]
    else:
        args = [*strata_flags(cli, model, ctx), "--context", str(ctx), *opt_flags("strata", opts)]
    log.write_text("", encoding="utf-8")
    say("strata launch " + " ".join(args))
    proc = spawn([*cli, "launch", *args, "--no-tui", "--yes"], log=log, cwd=str(src) if src else None,
                 env={**os.environ, "PYTHONUTF8": "1", "STRATA_NO_PROMPT": "1", **opt_env("strata", opts)})
    if preset:
        remember("strata", way="preset:" + preset)
    else:
        remember("strata", model=model, ctx=ctx, way="builtin", opts=opts or {})
    # `strata launch` hands the server to Strata's own runtime and returns: its exit is no verdict while
    # --status says the launch is starting. Ready = /health says the engine runs.
    start, shown, checked, misses = time.monotonic(), "", 0.0, 0
    while time.monotonic() - start < WAIT["strata"]:
        h = strata_health(base)
        if h and h.get("loaded") is not False:
            return _done("strata", base, None)
        # an exit 0 is the hand-over too: --status tells whether a launch still runs (asked twice then, an
        # error exit once), else an exit 0 with nothing running waited the whole 15 minutes
        code = proc.poll()
        if code is not None and not h and time.monotonic() - checked > 10:
            checked = time.monotonic()
            misses = 0 if strata_starting(cli, src) else misses + 1
            if misses >= (2 if code == 0 else 1):
                return _fail("Strata did not start: " + (" | ".join(tail(log, 3)) or "no output"))
        secs = int(time.monotonic() - start)
        text = (f"Strata is loading {model or preset} into memory… {secs} s" if h else
                f"starting Strata… {secs} s" + (f" · {tail(log, 1)[-1][:140]}" if tail(log, 1) else ""))
        if text != shown:
            say(text)
            shown = text
        time.sleep(2)
    return _fail(f"Strata did not get ready in {WAIT['strata'] // 60} minutes: " + (" | ".join(tail(log, 3)) or "no output"))


def strata_stop(say):
    cli, src = strata_cli()
    base = servers.default_base("strata")
    if cli:
        say("strata launch --stop")
        run([*cli, "launch", "--stop"], 240, cwd=str(src) if src else None)
    if not servers.port_down(base) and servers.kind_of(base, timeout=2) == "strata":
        pid = listener(8080)        # started by a script or the Strata app: the process on the port
        if pid:
            say(f"stopping the Strata process (PID {pid})")
            kill(pid)
    return wait_down(base, 30)


# ---------- LM Studio ----------

def lms_cli():
    saved = saved_path("lmstudio")
    if saved and Path(saved).name.lower().startswith("lms"):
        return saved
    h = Path.home()
    exe = "lms.exe" if os.name == "nt" else "lms"
    found = shutil.which("lms") or next((str(p) for p in (h / ".lmstudio" / "bin" / exe,
                                                          h / ".cache" / "lm-studio" / "bin" / exe) if p.is_file()), None)
    return found


def lms_folder():
    """LM Studio's model folder (its settings.json "downloadsFolder", else ~/.lmstudio/models)."""
    h = Path.home()
    with suppress(Exception):
        d = json.loads((h / ".lmstudio" / "settings.json").read_text(encoding="utf-8")).get("downloadsFolder")
        if d and Path(d).is_dir():
            return Path(d)
    return next((d for d in (h / ".lmstudio" / "models", h / ".cache" / "lm-studio" / "models") if d.is_dir()), None)


def lms_models():
    """The downloaded models. `lms ls` only while LM Studio's server runs: the lms command wakes LM Studio up
    (a look at Settings started it). Off: the list seen last time, else the .gguf files of its model folder
    (publisher/repo/file.gguf, the id `lms load` takes)."""
    base = servers.default_base("lmstudio")
    lms = lms_cli()
    models = []
    if not servers.port_down(base):
        if lms:
            code, out = run([lms, "ls", "--json"], 30)
            with suppress(ValueError):
                for m in json.loads(out[out.index("["):]):
                    if isinstance(m, dict) and m.get("modelKey") and m.get("type") in ("llm", "vlm", None):
                        models.append({"id": m["modelKey"], "name": m.get("displayName") or m["modelKey"],
                                       "size": m.get("sizeBytes"), "max_ctx": m.get("maxContextLength"),
                                       "vision": bool(m.get("vision")), "params": m.get("paramsString"),
                                       "quant": (m.get("quantization") or {}).get("name")})
        if not models:
            d = servers.get(servers.root(base) + "/api/v0/models")
            for m in (d or {}).get("data") or [] if isinstance(d, dict) else []:
                if isinstance(m, dict) and m.get("id") and m.get("type") in ("llm", "vlm", None):
                    models.append({"id": m["id"], "name": m["id"], "size": None, "max_ctx": m.get("max_context_length"),
                                   "vision": m.get("type") == "vlm", "quant": m.get("quantization")})
        if models:
            remember_quiet(lms_models=models)
        return models
    cached = load_state().get("lms_models")
    if isinstance(cached, list) and cached:
        return [m for m in cached if isinstance(m, dict) and m.get("id")]
    root = lms_folder()
    for path, size in gguf_files(roots=[root]) if root else []:
        rel = path.relative_to(root).as_posix()
        if rel.count("/") != 2:         # LM Studio's own layout only: publisher/repo/file.gguf
            continue
        models.append({"id": rel, "name": SHARD.sub(".gguf", path.name)[:-5], "size": size, "max_ctx": gguf_ctx(path),
                       "vision": any(x.name.lower().startswith("mmproj") for x in path.parent.glob("*.gguf"))})
    return models


def remember_quiet(**values):
    """Keeps values in launch.json without touching the last started program."""
    st = load_state()
    st.update(values)
    save_state(st)


def lms_start(model, ctx, say, opts=None, force=False):
    base = servers.default_base("lmstudio")
    lms = lms_cli()
    if not lms:
        return _fail("LM Studio's command `lms` is not found: open LM Studio once (it sets lms up), then try again")
    if not model:
        return _fail("pick a model")
    opts = opts or {}
    prev = mine("lmstudio", base)
    serve = bool(prev and prev.get("serve"))
    if servers.port_down(base):
        say("lms server start")
        run([lms, "server", "start"], 90)
        serve = True
        if not wait_up(base, 60, say, what="the LM Studio server"):
            return _fail("the LM Studio server did not start: open LM Studio -> Developer -> Start Server")
    on = servers.loaded_models(base, kind="lmstudio") or []
    same_opts = ((load_state().get("applied") or {}).get("lmstudio") or {}) == opts
    hit = next((m for m in on if servers.same_model(m[0], model)), None)
    if hit and not force and (not prev or not servers.same_model(prev.get("model"), hit[0]) or
                              ((hit[1] or 0) == int(ctx) and same_opts)):
        # loaded already: as asked, or by the user by hand (their load and its context are left as they are)
        say(f"{model} is already loaded" + (f" with {hit[1]} tokens" if hit[1] else ""))
        remember("lmstudio", model=model, ctx=ctx, way="builtin", opts=opts)
        return {**_done("lmstudio", base, hit[0]), "serve": serve}
    if prev and prev.get("model") and any(servers.same_model(prev["model"], m[0]) for m in on):
        say(f"lms unload {prev['model']} (loaded from here before)")
        run([lms, "unload", prev["model"]], 120)
    if on and not prev:
        say("LM Studio holds models loaded by hand: they stay")
    # one slot by default: the agent sends one request at a time, and LM Studio's default 4 slots took 4x the
    # cache memory (an 8B model at 32K spilled out of a 12 GB card and answered 20 tokens in 45 s)
    flags = opt_flags("lmstudio", opts)
    if "--parallel" in flags and "--parallel" not in run([lms, "load", "--help"], 20)[1]:
        i = flags.index("--parallel")
        del flags[i:i + 2]
    cmd = [lms, "load", model, "--context-length", str(ctx), *flags, "-y"]
    say("lms load " + " ".join(cmd[2:-1]))
    log = log_file("lmstudio")
    log.write_text("", encoding="utf-8")
    before = {(m[0].lower(), m[1]) for m in servers.loaded_models(base, kind="lmstudio") or []}
    proc = spawn(cmd, log=log)
    hit = lms_wait(base, model, proc, log, say, before)
    if not hit:
        return _fail("LM Studio did not load the model: " + (" | ".join(tail(log, 3)) or "no output"))
    remember("lmstudio", model=model, ctx=ctx, way="builtin", opts=opts, applied={**(load_state().get("applied") or {}), "lmstudio": opts})
    return {**_done("lmstudio", base, hit[0]), "serve": serve}


LMS_DONE = re.compile(r"loaded successfully|\b100(?:\.0+)?\s*%", re.I)
LMS_LAG = 10        # seconds LM Studio's model list may lag behind an `lms load` that has ended


def lms_wait(base, model, proc, log, say, before=()):
    """Waits for `lms load` -> the loaded (id, window), or None.

    Done when the model is in LM Studio's list as loaded and the load is over: `lms load` ended, or its output
    says it finished (the command could stay open after "Model loaded successfully", and the window showed
    "loading" for minutes over a model that was ready). A model that was already loaded before (the user's own
    copy) does not count until the command ended. After the command ended the list gets LMS_LAG seconds."""
    def find(fresh_only):
        on = [m for m in servers.loaded_models(base, kind="lmstudio") or []
              if servers.same_model(m[0], model) or m[0].startswith(model)]
        fresh = [m for m in on if (m[0].lower(), m[1]) not in before]
        return (fresh or ([] if fresh_only else on) or [None])[0]
    start, ended = time.monotonic(), None
    while time.monotonic() - start < WAIT["lmstudio"]:
        code = proc.poll()
        text = " ".join(tail(log, 3))
        if code is not None and ended is None:
            ended = time.monotonic()
        if code is not None or LMS_DONE.search(text):
            hit = find(fresh_only=code is None)
            if hit:
                return hit
            if code not in (None, 0) or (ended and time.monotonic() - ended > LMS_LAG):
                return None
        pct = re.findall(r"(\d+(?:\.\d+)?)\s*%", text)
        say(f"loading {model}… {int(time.monotonic() - start)} s", float(pct[-1]) if pct else None)
        time.sleep(1)
    return None


def lms_stop(say):
    lms = lms_cli()
    base = servers.default_base("lmstudio")
    if not lms:
        return False
    say("lms unload --all")
    run([lms, "unload", "--all"], 120)
    say("lms server stop")
    run([lms, "server", "stop"], 60)
    return wait_down(base, 20)


# ---------- Ollama ----------

def ollama_cli():
    if saved_path("ollama"):
        return saved_path("ollama")
    if os.name == "nt":
        return shutil.which("ollama") or next((str(p) for p in (
            Path(os.environ.get("LOCALAPPDATA", "-"), "Programs", "Ollama", "ollama.exe"),) if p.is_file()), None)
    return servers._which("ollama", "/Applications/Ollama.app/Contents/Resources/ollama")


def ollama_dir():
    return Path(servers.env_value("OLLAMA_MODELS") or Path.home() / ".ollama" / "models")


def ollama_models():
    base = servers.default_base("ollama")
    r = servers.root(base)
    models = []
    if not servers.port_down(base):
        d = servers.get(r + "/api/tags", timeout=5)
        for m in (d or {}).get("models") or [] if isinstance(d, dict) else []:
            if isinstance(m, dict) and m.get("name"):
                det = m.get("details") or {}
                models.append({"id": m["name"], "name": m["name"], "size": m.get("size"),
                               "params": det.get("parameter_size"), "quant": det.get("quantization_level"),
                               "max_ctx": None})

        def show(m):
            with suppress(Exception):
                info = servers.http(r + "/api/show", body={"model": m["id"]}, timeout=5) or {}
                for k, v in (info.get("model_info") or {}).items():
                    if k.endswith(".context_length") and isinstance(v, int):
                        m["max_ctx"] = v
                m["vision"] = "vision" in (info.get("capabilities") or [])
                if "embedding" in (info.get("capabilities") or []) and "completion" not in (info.get("capabilities") or []):
                    m["embed"] = True
        with cf.ThreadPoolExecutor(8) as pool:
            list(pool.map(show, models[:40]))
        return [m for m in models if not m.get("embed")]
    # the server is off: the downloaded models from the manifests (registry/<namespace>/<name>/<tag>)
    lib = ollama_dir() / "manifests"
    with suppress(OSError):
        for tag in lib.glob("*/*/*/*"):
            if tag.is_file():
                ns, name = tag.parent.parent.name, tag.parent.name
                full = f"{name}:{tag.name}" if ns == "library" else f"{ns}/{name}:{tag.name}"
                models.append({"id": full, "name": full, "size": None, "max_ctx": None})
    return models


def ollama_kill():
    if os.name == "nt":
        for name in ("ollama app.exe", "ollama.exe"):
            run(["taskkill", "/IM", name, "/T", "/F"], 30)
    elif sys.platform == "darwin":
        run(["osascript", "-e", 'quit app "Ollama"'], 10)
        run(["pkill", "-f", "ollama serve"], 10)
    else:
        if run(["systemctl", "--user", "stop", "ollama"], 20)[0] != 0:
            run(["pkill", "-f", "ollama serve"], 10)
        pid = listener(11434)
        if pid:
            kill(pid)


def ollama_start(model, ctx, say, opts=None):
    base = servers.default_base("ollama")
    r = servers.root(base)
    exe = ollama_cli()
    if not model:
        return _fail("pick a model (none downloaded? in a terminal: ollama pull qwen3:8b)")
    env = {"OLLAMA_CONTEXT_LENGTH": str(ctx), **opt_env("ollama", opts)}
    keep = env.get("OLLAMA_KEEP_ALIVE") or "30m"
    # only for the `ollama serve` started here: the user's own environment is never written, so Ollama started
    # by hand (the tray, a terminal) runs with the user's own settings, as before
    say(" ".join(f"{k}={v}" for k, v in env.items()))
    applied = (load_state().get("applied") or {}).get("ollama")
    prev = mine("ollama", base)
    started = {"serve": bool(prev and prev.get("serve"))}

    def serve():
        if not exe:
            return False
        started["serve"] = True
        say("ollama serve")
        spawn([exe, "serve"], log=log_file("ollama"), env={**os.environ, **env})
        remember("ollama", applied={**(load_state().get("applied") or {}), "ollama": env})
        return wait_up(base, 60, say, what="Ollama", check=lambda: isinstance(servers.get(r + "/api/version"), dict))

    def load():
        if prev and prev.get("model") and not servers.same_model(prev["model"], model):
            with suppress(Exception):           # the one loaded from here before; the user's own stay
                servers.http(r + "/api/generate", body={"model": prev["model"], "keep_alive": 0}, timeout=60)
        say(f"loading {model}…")
        try:
            servers.http(r + "/api/generate", body={"model": model, "keep_alive": keep}, timeout=WAIT["ollama"])
        except urllib.error.HTTPError as e:
            return str(e.read().decode("utf-8", "replace") or e)[:300]
        except Exception as e:
            # the answer can time out on a slow disk while Ollama goes on loading: it counts once it is loaded
            return "" if loaded() else str(e)[:300]
        return ""

    def loaded():
        return next((m for m in servers.loaded_models(base, kind="ollama") or [] if servers.same_model(m[0], model)), None)

    def window():
        hit = loaded()
        return hit[1] if hit else None

    if servers.port_down(base) and not serve():
        return _fail("Ollama did not start: start it from the Start menu (or: ollama serve)")
    err = load()
    # the window is read back; the other variables only take effect at a start of the server
    other = {k: v for k, v in env.items() if k != "OLLAMA_CONTEXT_LENGTH"}
    stale = applied is not None and {k: v for k, v in applied.items() if k != "OLLAMA_CONTEXT_LENGTH"} != other
    if not err and ((window() or 0) != int(ctx) or stale) and not started["serve"]:
        # an Ollama started by hand keeps its own settings
        say(f"Ollama was started outside maxedcode: it keeps its own settings (context {window() or '?'})")
    elif not err and ((window() or 0) != int(ctx) or stale):
        say("Ollama runs with other settings: restarting it with these")
        ollama_kill()
        wait_down(base, 20)
        if not serve():
            return _fail("Ollama did not start again: start it from the Start menu")
        err = load()
    if err:
        return _fail(f"Ollama did not load {model}: {err}")
    remember("ollama", model=model, ctx=ctx, way="builtin", opts=opts or {})
    return {**_done("ollama", base, model), "serve": started["serve"]}


def ollama_stop(say):
    base = servers.default_base("ollama")
    r = servers.root(base)
    for m, _ in servers.loaded_models(base, kind="ollama") or []:
        say(f"unloading {m}")
        with suppress(Exception):
            servers.http(r + "/api/generate", body={"model": m, "keep_alive": 0}, timeout=60)
    say("stopping Ollama")
    ollama_kill()
    return wait_down(base, 20)


# ---------- llama.cpp ----------

def llama_cli():
    saved = saved_path("llamacpp") or load_state().get("llama_server")
    if saved and Path(saved).is_file():
        return saved
    return shutil.which("llama-server") or (servers._which("llama-server") if os.name != "nt" else None)


def gguf_dirs():
    h = Path.home()
    dirs = [h / ".lmstudio" / "models", h / ".cache" / "lm-studio" / "models", h / ".cache" / "llama.cpp",
            h / ".cache" / "huggingface" / "hub", h / "models", h / "Models"]
    with suppress(Exception):
        lms = json.loads((h / ".lmstudio" / "settings.json").read_text(encoding="utf-8"))
        if lms.get("downloadsFolder"):
            dirs.insert(0, Path(lms["downloadsFolder"]))
    dirs += [Path(d) for d in load_state().get("gguf_dirs", [])]
    seen, out = set(), []
    for d in dirs:
        key = str(d).lower()
        if key not in seen and d.is_dir():
            seen.add(key)
            out.append(d)
    return out


SHARD = re.compile(r"-(\d{5})-of-\d{5}\.gguf$", re.I)


def gguf_files(limit=200, roots=None):
    """GGUF models on this PC: no vision projectors (mmproj), only the first part of a split model."""
    found = []
    for root in gguf_dirs() if roots is None else roots:
        for dirpath, dirnames, files in os.walk(root):
            if len(Path(dirpath).relative_to(root).parts) >= 5:
                dirnames[:] = []
            for f in files:
                if not f.lower().endswith(".gguf") or "mmproj" in f.lower():
                    continue
                m = SHARD.search(f)
                if m and m.group(1) != "00001":
                    continue
                p = Path(dirpath, f)
                with suppress(OSError):
                    size = p.stat().st_size
                    if size > 50_000_000 or m:
                        found.append((p, size))
            if len(found) >= limit:
                break
    return found


def gguf_ctx(path):
    """The training context of a GGUF model (<arch>.context_length in its header), or None."""
    def read_str(f):
        n, = struct.unpack("<Q", f.read(8))
        return f.read(n).decode("utf-8", "replace")
    sizes = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
    fmt = {4: "<I", 5: "<i", 10: "<Q", 11: "<q"}

    def skip(f, t):
        if t == 8:
            read_str(f)
        elif t == 9:
            sub, n = struct.unpack("<IQ", f.read(12))
            if sub in sizes:
                f.seek(sizes[sub] * n, 1)
            else:
                for _ in range(n):
                    skip(f, sub)
        else:
            f.seek(sizes[t], 1)
    with suppress(Exception), open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            return None
        f.read(4 + 8)
        count, = struct.unpack("<Q", f.read(8))
        for _ in range(min(count, 64)):
            key = read_str(f)
            t, = struct.unpack("<I", f.read(4))
            if key.endswith(".context_length") and t in fmt:
                return struct.unpack(fmt[t], f.read(struct.calcsize(fmt[t])))[0]
            skip(f, t)
    return None


def strata_files():
    """The .gguf files Strata's own run configs use (<strata>/strata-*.json): Strata's packed quantizations,
    which a plain llama-server does not load - not offered as llama.cpp models."""
    src = servers.strata_source() or (Path(saved_path("strata")) if saved_path("strata") else None)
    out = set()
    if not src:
        return out

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str) and x.lower().endswith(".gguf"):
            out.add(str(Path(x)).lower())
    with suppress(OSError):
        for conf in src.glob("strata-*.json"):
            with suppress(Exception):
                walk(json.loads(conf.read_text(encoding="utf-8-sig")))
    return out


def gguf_source(path):
    """Where a model file comes from, said shortly: LM Studio's folder, the Hugging Face / llama.cpp cache, or
    the folder itself."""
    h = Path.home()
    names = [(lms_folder(), "LM Studio"), (h / ".lmstudio" / "models", "LM Studio"),
             (h / ".cache" / "lm-studio" / "models", "LM Studio"),
             (h / ".cache" / "huggingface" / "hub", "Hugging Face"), (h / ".cache" / "llama.cpp", "llama.cpp cache")]
    for root, name in names:
        with suppress(ValueError, TypeError):
            Path(path).relative_to(root)
            return name
    return str(Path(path).parent)


def llama_models():
    """The .gguf models llama-server can run: every model file of the usual folders and the user's own, but
    Strata's own (strata_files)."""
    skip, seen = strata_files(), set()
    out = []
    for p, size in gguf_files():
        if str(p).lower() in skip or (p.name.lower(), size) in seen:     # the same file in two folders: once
            continue
        seen.add((p.name.lower(), size))
        out.append({"id": str(p), "name": SHARD.sub(".gguf", p.name)[:-5], "size": size, "max_ctx": gguf_ctx(p),
                    "vision": any("mmproj" in x.name.lower() for x in p.parent.glob("*.gguf")),
                    "path": str(p.parent), "where": gguf_source(p)})
    return out


def add_gguf_dir(path):
    """An own folder of .gguf models for llama.cpp (Settings -> Start models -> llama.cpp -> + Models folder)."""
    d = Path(str(path or "").strip().strip('"'))
    if not d.is_dir():
        return False
    st = load_state()
    dirs = [x for x in st.get("gguf_dirs", []) if str(x).lower() != str(d).lower()]
    st["gguf_dirs"] = [str(d)] + dirs
    save_state(st)
    return True


def llama_start(model, ctx, say, opts=None):
    base = servers.default_base("llamacpp")
    exe = llama_cli()
    if not exe:
        return _fail("llama-server is not found: download llama.cpp (llama-server) and put it on PATH, "
                     "or add your own way to start it")
    if not model or not Path(model).is_file():
        return _fail("pick a .gguf model file")
    if not free_port("llamacpp", say):
        return _fail("port 8080 is still taken")
    busy = make_room(base, "llama-server", say)
    if busy:
        return busy
    opts = opts or {}
    cmd = [exe, "-m", model, "-c", str(ctx), "--host", "127.0.0.1", "--port", "8080", "--jinja"]
    flags = opt_flags("llamacpp", opts)
    code, helptext = run([exe, "--help"], 20)
    if "-ngl" not in flags and "--fit" not in helptext:     # older builds put nothing on the GPU unless asked
        cmd += ["-ngl", "999"]
    kv = opt(opts, "llamacpp", "kv")
    if kv:
        cmd += ["-ctk", kv, "-ctv", kv]
    proj = sorted(Path(model).parent.glob("*mmproj*.gguf"))
    if proj and opt(opts, "llamacpp", "vision") != "off":
        cmd += ["--mmproj", str(proj[0])]
    cmd += flags
    log = log_file("llamacpp")
    log.write_text("", encoding="utf-8")
    say(" ".join(Path(x).name if i in (0, 2) else x for i, x in enumerate(cmd)))
    proc = spawn(cmd, log=log, env={**os.environ, **opt_env("llamacpp", opts)})
    remember("llamacpp", model=model, ctx=ctx, way="builtin", opts=opts, pid=proc.pid)
    if not wait_up(base, WAIT["llamacpp"], say, proc=proc, log=log, what="llama-server to load the model",
                   check=lambda: (servers.get(servers.root(base) + "/health") or {}).get("status") == "ok"):
        return _fail("llama-server did not start: " + " | ".join(tail(log, 3)))
    return _done("llamacpp", base, None)


def llama_stop(say):
    base = servers.default_base("llamacpp")
    pid = (load_state().get("pid") or {}).get("llamacpp")
    if pid and alive(pid):
        say(f"stopping llama-server (PID {pid})")
        kill(pid)
    if not servers.port_down(base) and servers.kind_of(base, timeout=2) == "llamacpp":
        kill(listener(8080))
    return wait_down(base, 20)


# ---------- all of them ----------

MODELS = {"strata": strata_models, "lmstudio": lms_models, "ollama": ollama_models, "llamacpp": llama_models}
START = {"strata": strata_start, "lmstudio": lms_start, "ollama": ollama_start, "llamacpp": llama_start}
STOP = {"strata": strata_stop, "lmstudio": lms_stop, "ollama": ollama_stop, "llamacpp": llama_stop}
CLI = {"strata": lambda: strata_cli()[0], "lmstudio": lms_cli, "ollama": ollama_cli, "llamacpp": llama_cli}
OTHER_KINDS = [k for k in dict.fromkeys(servers.PORTS.values()) if k not in KINDS]    # vLLM, KoboldCpp, Jan …


def status():
    """What runs on the usual ports now: {kind: describe() + "base"} (8080 is Strata or llama.cpp: asked which).
    The other servers (vLLM, KoboldCpp, Jan, text-generation-webui) under their own kinds."""
    out = {}

    def one(port):
        base = f"http://127.0.0.1:{port}/v1"
        if servers.port_down(base):
            return None
        kind = servers.PORTS[port]
        info = servers.describe(base, kind="" if port == 8080 else kind, timeout=3)
        return {**info, "base": base} if info.get("online") else None
    others = [p for p in servers.listening_ports() if p not in servers.PORTS]
    with cf.ThreadPoolExecutor(max(1, min(32, len(servers.PORTS) + len(others)))) as pool:
        usual = list(pool.map(one, servers.PORTS))
        extra = list(pool.map(lambda port: servers._probe_other(port, "", 0.3), others))     # see servers.running
        for port, info in zip(servers.PORTS, usual):
            if info:
                out[info["kind"] or servers.PORTS[port]] = info
        for x in extra:
            if x and (x["kind"] or "other-" + x["base"]) not in out:
                info = servers.describe(x["base"], kind=x["kind"], timeout=3)
                out[x["kind"] or "other-" + x["base"]] = {**info, "base": x["base"]}
    return out


def follow_target(base):
    """base is on this PC and does not answer, and exactly one other model server runs here (started from its
    own window, a script, the tray): that one {"kind", "base", "model", "label"}, else None. The CLI and the
    window switch the agent to it."""
    host = servers.host_of(base)
    if host not in ("127.0.0.1", "localhost", "::1"):
        return None
    if not servers.port_down(base) and servers.model_ids(base, timeout=2) is not None:
        return None
    on = {k: v for k, v in status().items() if v["base"].rstrip("/") != str(base).rstrip("/")}
    if len(on) != 1:
        return None
    kind, info = next(iter(on.items()))
    model = info.get("model") if kind in servers.JIT_KINDS else None
    if kind in servers.JIT_KINDS and not model:      # LM Studio / Ollama with nothing loaded: nothing to follow
        return None
    return {"kind": kind, "base": info["base"], "model": model, "label": servers.LABELS.get(kind, kind)}


def ways(kind, st=None, cli=None, presets=None, scripts=None):
    """The ways to start a program, in the order they are offered: the builtin start, the program's own presets,
    start scripts found in its folder, the user's own ways."""
    st = load_state() if st is None else st
    out = []
    if kind in KINDS and cli:
        out.append({"id": "builtin", "type": "builtin", "source": "builtin"})
    for name in presets or []:
        out.append({"id": "preset:" + name, "type": "preset", "name": name, "source": "program"})
    for path in scripts or []:
        out.append({"id": "script:" + path, "type": "script", "name": Path(path).stem, "command": path,
                    "base": servers.default_base(kind), "source": "found"})
    if kind == OTHER:
        for path in st.get("kobold") or []:
            if Path(path).is_file():
                out.append({"id": "script:" + path, "type": "script", "name": Path(path).stem, "command": f'"{path}"',
                            "base": "http://127.0.0.1:5001/v1", "source": "found"})
    for w in own_ways(st):
        if w["kind"] == kind:
            out.append({**w, "source": "own"})
    return out


def way_by_id(kind, way_id, st=None):
    scripted = str(way_id).startswith("script:") and kind != OTHER
    return next((w for w in ways(kind, st, cli=True, presets=[way_id[7:]] if str(way_id).startswith("preset:") else [],
                                 scripts=[way_id[7:]] if scripted else [])
                 if w["id"] == way_id), None)


def catalog():
    """Every program this window can start: installed or not, running or not, its ways, models and settings."""
    st = load_state()
    found = {x["kind"]: x for x in servers.installed()}
    with cf.ThreadPoolExecutor(8) as pool:
        running = pool.submit(status)
        lists = {k: pool.submit(_safe, MODELS[k]) for k in KINDS}
        clis = {k: pool.submit(_safe, CLI[k]) for k in KINDS}
        presets = pool.submit(_safe, strata_presets)
        scripts = pool.submit(_safe, strata_scripts)
        running = running.result()
        result = []
        for kind in KINDS:
            info = running.get(kind)
            models = lists[kind].result() or []
            cli = clis[kind].result()
            loaded = {}
            if info:
                on = servers.loaded_models(info["base"], kind=kind) if kind in servers.JIT_KINDS else None
                name = on[0][0] if on else info.get("model")
                loaded = {"model": name, "ctx": (on[0][1] if on else None) or info.get("ctx")}
                if kind == "strata":
                    h = strata_health(info["base"]) or {}
                    loaded["model"] = name or h.get("model") or (servers.model_ids(info["base"]) or [None])[0]
                    loaded["asleep"] = h.get("loaded") is False     # the engine is not running now
            for m in models:
                m["ctxs"] = ctx_options(m.get("max_ctx"), m.get("ctx"), (st.get("ctx") or {}).get(f"{kind}|{m['id']}"))
                m["pick_ctx"] = default_ctx(kind, m, st)
                m["loaded"] = bool(loaded.get("model")) and (servers.same_model(loaded["model"], m["id"])
                                                             or str(loaded["model"]).startswith(m["id"])
                                                             or m["id"].lower() in str(loaded["model"]).lower())
                # Strata's API answers while its engine is not in memory (asleep, or loading it again): not "loaded"
                m["asleep"] = m["loaded"] and bool(loaded.get("asleep"))
            w = ways(kind, st, cli, (presets.result() or []) if kind == "strata" else [],
                     (scripts.result() or []) if kind == "strata" else [])
            entry = {"kind": kind, "label": servers.LABELS[kind], "about": ABOUT[kind],
                     "installed": bool(kind in found or cli), "running": bool(info),
                     "base": (info or {}).get("base") or servers.default_base(kind), "can_start": bool(cli) or len(w) > 0,
                     "path": saved_path(kind) or (str(cli[-1]) if isinstance(cli, list) else cli) or "",
                     "loaded": loaded, "models": models, "download": DOWNLOAD[kind],
                     "how": (found.get(kind) or {}).get("how", ""), "ways": w,
                     "last_way": (st.get("way") or {}).get(kind) or "",
                     "last_model": (st.get("model") or {}).get(kind),
                     "options": options(kind), "opts": (st.get("opts") or {}).get(kind) or {}}
            if kind == "llamacpp":
                entry["dirs"] = [str(d) for d in gguf_dirs()]
            if kind == "ollama" and not models:
                entry["hint"] = "ollama pull qwen3:8b"
            result.append(entry)
        # any other server: the user's own ways; what runs on the other usual ports
        others = [dict(v, kind=k) for k, v in running.items() if k not in KINDS]
        w = ways(OTHER, st)
        result.append({"kind": OTHER, "label": "", "about": ABOUT[OTHER], "installed": True,
                       "running": bool(others), "base": others[0]["base"] if others else "",
                       "can_start": bool(w), "loaded": {"model": others[0].get("model"), "ctx": others[0].get("ctx"),
                                                        "label": servers.LABELS.get(others[0]["kind"], "")} if others else {},
                       "running_list": [{"kind": o["kind"], "label": servers.LABELS.get(o["kind"], o["kind"]),
                                         "base": o["base"], "model": o.get("model")} for o in others],
                       "models": [], "ways": w, "last_way": (st.get("way") or {}).get(OTHER) or "",
                       "options": [], "opts": {}, "download": ""})
    o = mine()
    for e in result:
        if e["running"]:
            e["by_app"] = bool(o and (e["kind"] == o.get("kind") or str(e.get("base")).rstrip("/") == str(o["base"]).rstrip("/")))
    return {"servers": result, "last": st.get("kind") or "",
            "ours": {"kind": o.get("kind"), "label": servers.LABELS.get(o.get("server"), o.get("server") or ""),
                     "base": o["base"], "model": o.get("model")} if o else None}


def _safe(fn):
    with suppress(Exception):
        return fn()
    return None


def _fail(error):
    return {"done": True, "ok": False, "error": error}


def _done(kind, base, model):
    return {"done": True, "ok": True, "kind": kind, "base": base, "model": model}


def use(result):
    """A started server becomes the agent's: the address, the kind, and the model a request names (LM Studio /
    Ollama load whatever is named; Strata and llama.cpp serve one model: auto)."""
    with suppress(Exception):
        from . import appsettings
        appsettings.apply({"base_url": result["base"], "server": result["kind"] or None,
                           "model": result.get("model") if result["kind"] in servers.JIT_KINDS else None})
        return True
    return False


def start(kind, model=None, ctx=None, script=None, say=None, way=None, opts=None):
    """Starts a program one of its ways (way: an id from ways(); none: the builtin start, or script)."""
    say = say or (lambda text, pct=None: None)
    if kind not in START and kind != OTHER:
        return _fail(f"{kind or 'this server'} is not started from here")
    if servers.offline():
        return _fail("offline (MAXEDCODE_OFFLINE)")
    ctx = int(ctx or DEFAULT_CTX)
    w = {"id": "", "type": "script", "command": script, "name": Path(str(script)).stem} if script else \
        way_by_id(kind, way) if way and way != "builtin" else None
    if way and way != "builtin" and not w:
        return _fail("this way to start is gone: pick another one")
    target = (w.get("base") if w else None) or (servers.default_base(kind) if kind in KINDS else "")
    try:
        prev = mine()
        if prev and str(prev["base"]).rstrip("/").lower() != str(target).rstrip("/").lower():
            release(prev, say)          # another program started from here: it makes room for this one
        if w and w["type"] == "preset":
            result = strata_start(None, ctx, say, preset=w["name"])
        elif w:
            if script:
                remember(kind, script=script)
            result = script_start(kind, w, say)
        elif kind == OTHER:
            result = _fail("add your own way to start this server: its command and address")
        else:
            result = START[kind](model, ctx, say, opts=opts or {})
    except Exception as e:            # a broken install must end the job with words, not a traceback
        result = _fail(f"{type(e).__name__}: {e}")
    if result.get("ok"):
        keep_ours({"kind": kind, "server": result["kind"], "base": result["base"],
                   "model": result.get("model") if result["kind"] in servers.JIT_KINDS else None,
                   "serve": result.pop("serve", kind not in servers.JIT_KINDS)})
        live = servers.describe(result["base"], kind=result["kind"] if result["kind"] != OTHER else "",
                                model=result.get("model"))
        result["kind"] = live.get("kind") or result["kind"]
        result["ctx"] = live.get("loaded_ctx") or live.get("ctx") or (ctx if not w else None)
        result["model"] = result.get("model") or live.get("model") or live.get("use")
        result["saved"] = use(result)
    return result


def stop(kind, say=None, base=None):
    """Stops a program (its own way: `strata launch --stop`, `lms server stop` …); a server of another kind at
    an address: the process listening there."""
    say = say or (lambda text, pct=None: None)
    try:
        if kind in STOP:
            ok = STOP[kind](say)
        elif base:
            kind_now = servers.kind_of(base, timeout=2)
            if kind_now in STOP:
                ok = STOP[kind_now](say)
            else:
                port = _port(base)
                pid = listener(port) if port else None
                if pid:
                    say(f"stopping the server on port {port} (PID {pid})")
                    kill(pid)
                ok = wait_down(base, 20)
        else:
            return {"ok": False, "text": f"{kind} is not stopped from here"}
    except Exception as e:
        return {"ok": False, "text": f"{type(e).__name__}: {e}"}
    if ok:
        forget_ours(None if base else kind, base)
    return {"ok": bool(ok), "text": "stopped" if ok else "it still answers"}


def main(argv):
    command = argv[1] if len(argv) > 1 else "get"
    body = {}
    if command in ("start", "stop", "set"):
        with suppress(ValueError):
            body = json.loads(sys.stdin.read() or "{}")

    def say(text, pct=None):
        print(json.dumps({"text": text, **({"pct": pct} if pct is not None else {})}, ensure_ascii=False), flush=True)
    if command == "start":
        print(json.dumps(start(body.get("kind"), body.get("model"), body.get("ctx"), body.get("script"), say,
                               way=body.get("way"), opts=body.get("opts")), ensure_ascii=False), flush=True)
    elif command == "stop":
        print(json.dumps({"done": True, **stop(body.get("kind"), say, body.get("base"))}, ensure_ascii=False), flush=True)
    else:
        if command == "set" and body.get("do") == "way-save" and body.get("command"):
            save_way(body.get("kind") or OTHER, body.get("name"), body["command"], body.get("base") or "", body.get("id"))
        elif command == "set" and body.get("do") == "way-remove" and body.get("id"):
            remove_way(body["id"])
        elif command == "set" and body.get("do") == "gguf-dir":
            add_gguf_dir(body.get("path"))
        elif command == "set" and body.get("do") == "path":
            if not set_path(body.get("kind"), body.get("path")):
                print(json.dumps({**catalog(), "error": "this is not the program's file"}, ensure_ascii=False))
                return 0
        elif command == "find":
            found, kobold = find_programs()
            print(json.dumps({**catalog(), "found": found, "kobold": kobold}, ensure_ascii=False))
            return 0
        print(json.dumps(catalog(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
