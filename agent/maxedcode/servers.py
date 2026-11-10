"""Model servers on this PC: which ones are installed, which ones run, what each one really serves.

One place for what differs between servers, used by the installer, the first start of the CLI and the window,
and by every run (config.resolve_server):

* kind      - strata / llamacpp / lmstudio / ollama / vllm / koboldcpp / jan / tgwui, or a cloud API (CLOUD)
* context   - the window the server really allocated (each server reports it its own way); a wrong number
              means the agent compacts too late and the server cuts the prompt itself, which breaks the cache
* model     - the loaded model; LM Studio and Ollama list every downloaded model, and a request for another
              id makes them load it (the cache is gone) or fail
* body      - the cache fields this server understands (cache_fields); the prompt bytes are the same everywhere

Standard library only and no maxedcode imports: install.py runs it before the dependencies are installed.
"""
import concurrent.futures as cf
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from pathlib import Path

LABELS = {"strata": "Strata", "llamacpp": "llama.cpp", "lmstudio": "LM Studio", "ollama": "Ollama", "vllm": "vLLM",
          "koboldcpp": "KoboldCpp", "jan": "Jan", "tgwui": "text-generation-webui", "openai": "OpenAI",
          "openrouter": "OpenRouter", "": "OpenAI-compatible server"}
# Cloud APIs. Every big provider has an OpenAI-compatible endpoint, so none needs a protocol of its own:
# label -> (kind, base URL). A cloud kind skips the probes of local servers and gets no local cache fields.
CLOUD = {
    "OpenAI": ("openai", "https://api.openai.com/v1"),
    "Anthropic (Claude)": ("anthropic", "https://api.anthropic.com/v1"),
    "Google Gemini": ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
    "OpenRouter": ("openrouter", "https://openrouter.ai/api/v1"),
    "DeepSeek": ("deepseek", "https://api.deepseek.com/v1"),
    "Groq": ("groq", "https://api.groq.com/openai/v1"),
    "Mistral": ("mistral", "https://api.mistral.ai/v1"),
    "xAI (Grok)": ("xai", "https://api.x.ai/v1"),
    "Together AI": ("together", "https://api.together.xyz/v1"),
    "Cerebras": ("cerebras", "https://api.cerebras.ai/v1"),
}
LABELS.update({kind: label for label, (kind, _) in CLOUD.items()})
# local ports in the order they are shown; 8080 is Strata or llama.cpp, told apart by /health
PORTS = {8080: "strata", 1234: "lmstudio", 11434: "ollama", 8000: "vllm", 5001: "koboldcpp", 1337: "jan", 5000: "tgwui"}
CLOUD_HOSTS = {urllib.parse.urlsplit(url).hostname: kind for kind, url in CLOUD.values()}
CLOUD_KINDS = set(CLOUD_HOSTS.values())
LLAMA_FAMILY = {"strata", "llamacpp", "koboldcpp"}      # llama.cpp servers: cache_prompt is theirs
UA = {"User-Agent": "maxedcode"}
NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# What Ollama allocates when nothing sets the context: the prompt is cut from the front without an error
OLLAMA_DEFAULT_CTX = 4096


# ---------- HTTP ----------

def is_local(host):
    """Own network (localhost, 192.168.x.x, 10.x, a bare PC name): reached without the system proxy."""
    if host in ("localhost", ""):
        return True
    with suppress(ValueError):
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    return "." not in host


def port_down(base, timeout=0.5):
    """True when a server on this PC / own network does not accept a TCP connection at all.

    On Windows a connect to a closed port takes ~2 s (the stack retries the refused SYN), and describe() asks up
    to 7 endpoints: with the model server off, maxed showed nothing for 14 s. A listening server accepts at once
    (the kernel does it, however busy the model is), so a short connect tells "off" in 0.5 s. Cloud and other
    remote hosts are not checked: False."""
    if offline():
        return False
    u = urllib.parse.urlsplit(str(base))
    host = u.hostname or ""
    if not is_local(host):
        return False
    try:
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout if host in ("127.0.0.1", "localhost", "::1")
                                      else max(timeout, 1.5)):
            return False
    except (OSError, ValueError):
        return True


def root(base):
    """http://host:port/v1 -> http://host:port (the native endpoints live there)."""
    base = str(base or "").rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def offline():
    """MAXEDCODE_OFFLINE=1 (set by run_all_tests.py): no server is asked and nothing is started, so tests do not
    depend on a running model and never wait on closed ports."""
    return os.environ.get("MAXEDCODE_OFFLINE", "").strip().lower() in ("1", "true", "yes")


def http(url, key="", body=None, timeout=3):
    """GET (or POST body) JSON; raises on any failure. Local addresses bypass the proxy."""
    if offline():
        raise OSError("offline (MAXEDCODE_OFFLINE)")
    host = urllib.parse.urlsplit(url).hostname or ""
    head = {**UA, **auth_headers(url, key), **({"Content-Type": "application/json"} if body is not None else {})}
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=head)
    with (NOPROXY.open if is_local(host) else urllib.request.urlopen)(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def auth_headers(url, key):
    """The key as the server wants it: a bearer token, or x-api-key for Anthropic's own endpoints (the model
    list; its chat endpoint for OpenAI clients takes the bearer token)."""
    if not key or key == "local":
        return {}
    if cloud_kind(url) == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {key}"}


def split_addr(addr):
    """'192.168.0.10:1234', 'http://host/v1/chat/completions' -> (scheme, host, port|None, path) or None."""
    a = str(addr or "").strip().strip("\"'").replace("\\", "/")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", a, re.I):
        a = "http://" + a
    try:
        u = urllib.parse.urlsplit(a)
        port = u.port
    except ValueError:
        return None
    if u.scheme.lower() not in ("http", "https") or not u.hostname:
        return None
    path = u.path.rstrip("/")
    for tail in ("/chat/completions", "/completions", "/models"):
        if path.endswith(tail):
            path = path[: -len(tail)]
    return u.scheme.lower(), u.hostname, port, path


def candidates(addr, ports=None, known=None):
    """URLs to try for a typed address: no port -> the known ports (and 80), no path -> /v1 and the root."""
    p = split_addr(addr)
    if not p:
        return []
    scheme, host, port, path = p
    h = f"[{host}]" if ":" in host else host
    known = list(known or PORTS)
    if port:
        hosts = [f"{h}:{port}"]
    elif ports:
        hosts = [f"{h}:{x}" for x in ports]
    elif scheme == "https":
        hosts = [h]
    elif path:
        hosts = [h] + [f"{h}:{x}" for x in known]   # http://host/api is most likely port 80
    else:
        hosts = [f"{h}:{x}" for x in known] + [h]
    paths = [path, path + "/v1"] if path and not path.endswith("/v1") else [path or "/v1"] + ([] if path else [""])
    return [f"{scheme}://{x}{y}" for x in hosts for y in paths]


def find_base(addr, key="", timeout=2):
    """A typed address in any form ("192.168.0.10", "pc:1234", ".../chat/completions") -> the API URL that
    answers (a 401 counts: the server is there), or None. One candidate is returned without a request."""
    urls = candidates(addr)
    if len(urls) <= 1 or cloud_kind(addr):       # a known cloud API: its address is used as it is
        return urls[0] if urls else None

    def answers(url):
        try:
            http(url + "/models", key, timeout=timeout)
            return True
        except urllib.error.HTTPError as e:
            return e.code in (401, 403)
        except Exception:
            return False
    with cf.ThreadPoolExecutor(min(16, len(urls))) as pool:
        ok = list(pool.map(answers, urls))
    return next((u for u, good in zip(urls, ok) if good), None)


def get(url, key="", timeout=3):
    """http() that answers None instead of raising."""
    with suppress(Exception):
        return http(url, key, timeout=timeout)
    return None


def model_ids(base, key="", timeout=3):
    """GET /v1/models -> [ids] | None (no answer or not an OpenAI API). Raises HTTPError 401/403 (key needed)."""
    try:
        data = http(str(base).rstrip("/") + "/models", key, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise
        return None
    except Exception:
        return None
    items = data.get("data", data.get("models")) if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    return [str(m.get("id") or m.get("name")) for m in items if isinstance(m, dict) and (m.get("id") or m.get("name"))]


# ---------- what the server is ----------

def cloud_kind(base):
    """The cloud API kind of an address ("anthropic", "gemini" ...) or "" for any other server."""
    host = (urllib.parse.urlsplit(str(base)).hostname or "").lower()
    for h, kind in CLOUD_HOSTS.items():
        if host == h or host.endswith("." + h):
            return kind
    return ""


def kind_of(base, key="", timeout=2, health=None):
    """Which server answers at base. health: an already read /health answer (saves a request)."""
    if cloud_kind(base):
        return cloud_kind(base)
    r = root(base)
    health = get(r + "/health", key, timeout) if health is None else health
    if isinstance(health, dict) and health.get("max_context"):
        return "strata"
    ver = get(r + "/api/version", key, timeout)
    if isinstance(ver, dict) and ver.get("version"):
        return "ollama"
    lms = get(r + "/api/v0/models", key, timeout)
    if isinstance(lms, dict) and isinstance(lms.get("data"), list) and \
            any(isinstance(m, dict) and "state" in m for m in lms["data"]):
        return "lmstudio"
    if isinstance(get(r + "/api/extra/version", key, timeout), dict):
        return "koboldcpp"
    props = get(r + "/props", key, timeout)
    if isinstance(props, dict) and ("default_generation_settings" in props or "build_info" in props):
        return "llamacpp"
    if isinstance(get(r + "/version", key, timeout), dict):
        return "vllm"
    port = urllib.parse.urlsplit(str(base)).port
    return {1234: "lmstudio", 11434: "ollama", 1337: "jan", 5000: "tgwui"}.get(port, "")


def _lms_models(base, key, timeout):
    d = get(root(base) + "/api/v0/models", key, timeout)
    return [m for m in (d.get("data") or []) if isinstance(m, dict)] if isinstance(d, dict) else []


def _ollama_loaded(base, key, timeout):
    d = get(root(base) + "/api/ps", key, timeout)
    return [m for m in (d.get("models") or []) if isinstance(m, dict)] if isinstance(d, dict) else []


def env_value(name):
    """An environment variable as a newly started program sees it: this process, then the user's registry."""
    if os.environ.get(name):
        return os.environ[name]
    if os.name != "nt":
        return _posix_env().get(name, "")
    with suppress(Exception):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, name)[0])
    return ""


def loaded_model(base, key="", kind="", timeout=3):
    """The model the server has loaded now, or None when it cannot tell (several, or none loaded)."""
    if kind == "lmstudio":
        on = [m.get("id") for m in _lms_models(base, key, timeout)
              if m.get("state") == "loaded" and m.get("type") in (None, "llm", "vlm")]
        return on[0] if len(on) == 1 else None
    if kind == "ollama":
        on = [m.get("model") or m.get("name") for m in _ollama_loaded(base, key, timeout)]
        if len(on) == 1:
            return on[0]
        if on:
            return None
    with suppress(Exception):
        ids = model_ids(base, key, timeout)
        if ids and len(ids) == 1:
            return ids[0]
    return None


def context(base, key="", kind="", model=None, timeout=3, health=None, props=None):
    """The context window the server really allocated (per request slot), or None when it does not say."""
    r = root(base)
    if kind in ("strata", ""):
        health = get(r + "/health", key, timeout) if health is None else health
        if isinstance(health, dict) and health.get("max_context"):
            return int(health["max_context"])
    if kind in ("strata", "llamacpp", ""):
        props = get(r + "/props", key, timeout) if props is None else props
        if isinstance(props, dict):
            n = (props.get("default_generation_settings") or {}).get("n_ctx") or props.get("n_ctx")
            if n:
                return int(n)
    if kind == "lmstudio":
        ms = _lms_models(base, key, timeout)
        on = [m for m in ms if m.get("state") == "loaded" and (not model or m.get("id") == model)] or \
            [m for m in ms if m.get("id") == model]
        for m in on:
            n = m.get("loaded_context_length") or (m.get("max_context_length") if m.get("state") == "loaded" else None)
            if n:
                return int(n)
        return None
    if kind == "ollama":
        on = [m for m in _ollama_loaded(base, key, timeout)
              if not model or model in (m.get("model"), m.get("name"))]
        for m in on:
            if m.get("context_length"):
                return int(m["context_length"])
        if model:
            show = None
            with suppress(Exception):
                show = http(r + "/api/show", key, {"model": model}, timeout)
            m = re.search(r"num_ctx\s+(\d+)", str((show or {}).get("parameters") or "")) if isinstance(show, dict) else None
            if m:
                return int(m.group(1))
        n = env_value("OLLAMA_CONTEXT_LENGTH")
        return int(n) if n.isdigit() else None
    if kind == "koboldcpp":
        d = get(r + "/api/extra/true_max_context_length", key, timeout)
        if isinstance(d, dict) and d.get("value"):
            return int(d["value"])
    # vLLM (max_model_len), OpenRouter (context_length) and others put it into the model list
    d = get(str(base).rstrip("/") + "/models", key, timeout)
    for m in (d.get("data") or []) if isinstance(d, dict) else []:
        if isinstance(m, dict) and (not model or m.get("id") == model):
            n = m.get("max_model_len") or m.get("context_length") or m.get("loaded_context_length")
            if n:
                return int(n)
    return None


def describe(base, key="", kind="", model=None, timeout=3):
    """Everything a run needs to know, in as few requests as each server allows.

    -> {"kind", "ctx", "model" (loaded, or None), "props" (llama.cpp /props or None), "online"}"""
    r = root(base)
    cloud = kind in CLOUD_KINDS or bool(cloud_kind(base))
    if not cloud and port_down(base):       # nothing listens: no 7 slow refused connects (see port_down)
        return {"kind": kind, "ctx": None, "model": None, "props": None, "online": False}
    health = None if cloud else get(r + "/health", key, timeout)
    if not kind:
        kind = kind_of(base, key, min(timeout, 2), health)
        cloud = kind in CLOUD_KINDS
    props = get(r + "/props", key, timeout) if kind in ("strata", "llamacpp", "") else None
    loaded = None if cloud else loaded_model(base, key, kind, timeout)
    ctx = context(base, key, kind, model or loaded, timeout, health, props)
    online = health is not None or props is not None or loaded is not None or ctx is not None
    if cloud and not online:            # most cloud model lists carry no context size: a list is an answer
        with suppress(Exception):
            online = model_ids(base, key, timeout) is not None
    return {"kind": kind, "ctx": ctx, "model": loaded, "props": props, "online": online}


def cache_fields(kind):
    """Which optional cache fields a request to this server carries (cachekey.extra_body).

    Strata: the cache key and the system-turn pin; llama.cpp servers: cache_prompt; OpenAI: its own
    prompt_cache_key. LM Studio, Ollama and vLLM reuse the prefix by themselves and get nothing extra.
    An unknown local server keeps the full set: local servers ignore fields they do not know."""
    if kind == "strata" or kind == "":
        return {"cache_prompt", "promptCacheKey", "strata_prefix"}
    if kind in LLAMA_FAMILY:
        return {"cache_prompt"}
    if kind == "openai":
        return {"prompt_cache_key"}
    return set()


# ---------- this PC ----------

def _exists(*parts):
    p = Path(*[str(x) for x in parts])
    return p if p.exists() else None


def _local(*parts):
    return _exists(os.environ.get("LOCALAPPDATA", "-"), *parts)


def strata_source():
    """A Strata source checkout / terminal install (strata.py + bin\\strata.cmd): STRATA_DIR, the folder of the
    strata command on PATH, then the usual places (<drive>:\\AI\\Strata, <drive>:\\Strata, ~\\Strata)."""
    places = [env_value("STRATA_DIR")]
    on_path = shutil.which("strata")
    if on_path:
        places.append(str(Path(on_path).resolve().parent.parent))
    if os.name == "nt":
        drives = [f"{d}:\\" for d in "CDEFG" if Path(f"{d}:\\").exists()]
        places += [str(Path(d, *sub)) for d in drives for sub in (("AI", "Strata"), ("Strata",))]
    places += [str(Path.home() / "Strata"), str(Path.home() / ".strata" / "app")]
    if os.name != "nt":
        places += [str(Path.home() / "AI" / "Strata"), str(Path.home() / ".local" / "share" / "strata")]
    launcher = ("strata.cmd",) if os.name == "nt" else ("strata", "strata.sh")
    for p in places:
        if p and (Path(p) / "strata.py").is_file() and any((Path(p) / "bin" / x).is_file() for x in launcher):
            return Path(p)
    return None


def installed():
    """Model servers installed on this PC: [{"kind", "label", "path", "start": [cmd] | None, "how": text}]."""
    if os.name != "nt":
        return _installed_posix()
    home, out = Path.home(), []
    strata = shutil.which("strata")
    sp = strata or _local("Programs", "Strata") or _exists(home, ".strata")
    # Strata (github.com/Niko1221/Strata) starts from its sources / terminal install with `strata launch`
    # (bin\strata.cmd -> strata.py), which asks for the model - no flag picks the last one, `--preset NAME
    # --no-tui` needs a saved preset - so it opens in its own console. The desktop app is never started from here.
    src = strata_source()
    cli = [str(src / "bin" / "strata.cmd")] if src else [strata] if strata else None
    if sp or src:
        out.append({"kind": "strata", "path": str(src or sp), "console": True, "wait": 180,
                    "start": [*cli, "launch"] if cli else None,
                    "how": "strata launch (in the Strata folder: bin\\strata.cmd launch; "
                           "no questions: strata launch --preset \"<name>\" --no-tui)"})
    lms = shutil.which("lms") or _exists(home, ".lmstudio", "bin", "lms.exe") or \
        _exists(home, ".cache", "lm-studio", "bin", "lms.exe")
    lp = _local("Programs", "LM Studio") or _local("Programs", "lm-studio") or lms or _exists(home, ".lmstudio")
    if lp:
        out.append({"kind": "lmstudio", "path": str(lp), "start": [str(lms), "server", "start"] if lms else None,
                    "how": "LM Studio: Developer -> Start Server (or: lms server start)"})
    oll = shutil.which("ollama") or _local("Programs", "Ollama", "ollama.exe")
    if oll:
        tray = _local("Programs", "Ollama", "ollama app.exe")
        out.append({"kind": "ollama", "path": str(oll), "start": [str(tray)] if tray else [str(oll), "serve"],
                    "how": "start Ollama from the Start menu (or: ollama serve)"})
    llama = shutil.which("llama-server")
    if llama:
        out.append({"kind": "llamacpp", "path": llama, "start": None,
                    "how": "llama-server -m model.gguf --port 8080 -c 32768"})
    jan = _local("Programs", "Jan") or _local("Programs", "jan")
    if jan:
        out.append({"kind": "jan", "path": str(jan), "start": None, "how": "Jan: Settings -> Local API Server -> Start"})
    for x in out:
        x["label"] = LABELS[x["kind"]]
    return out


def _probe_local(port, key, timeout):
    """One local port -> a running server's description or None."""
    base = f"http://127.0.0.1:{port}/v1"
    try:
        ids = model_ids(base, key, timeout)
    except urllib.error.HTTPError:
        return {"base": base, "kind": PORTS.get(port, ""), "models": None, "need_key": True}
    if ids is None:
        return None
    d = describe(base, key, timeout=timeout)
    return {"base": base, "kind": d["kind"], "models": ids, "loaded": d["model"], "ctx": d["ctx"], "need_key": False}


def running(key="", timeout=1.5):
    """OpenAI-compatible servers that answer on this PC's usual ports, in PORTS order."""
    with cf.ThreadPoolExecutor(len(PORTS)) as ex:
        found = [x for x in ex.map(lambda p: _probe_local(p, key, timeout), PORTS) if x]
    for x in found:
        x["label"] = LABELS.get(x["kind"], LABELS[""])
        x["running"] = True
    return found


def scan(key=""):
    """Running servers first, then installed ones that are not running: what a first start offers."""
    up = running(key)
    kinds = {x["kind"] for x in up}
    rest = [{**x, "running": False, "base": default_base(x["kind"])} for x in installed() if x["kind"] not in kinds]
    for x in up:
        same = next((i for i in installed() if i["kind"] == x["kind"]), None) if x["kind"] else None
        x["start"], x["how"], x["path"] = None, (same or {}).get("how", ""), (same or {}).get("path", "")
    return up + rest


def default_base(kind):
    """The local address a server of this kind listens on by default."""
    port = next((p for p, k in PORTS.items() if k == kind), 8080)
    return f"http://127.0.0.1:{port}/v1"


def start(kind, wait=None):
    """Starts an installed server in the background and waits until it answers -> its base URL or None.
    One that asks questions (strata launch) gets its own console window; the user picks the model there."""
    entry = next((x for x in installed() if x["kind"] == kind), None)
    if not entry or not entry.get("start") or offline():
        return None
    wait = wait or entry.get("wait") or 30
    if os.name != "nt":
        flags = 0         # a session of its own (below): it outlives the window or terminal that started it
    elif entry.get("console"):
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    else:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | \
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with suppress(OSError):
        subprocess.Popen(entry["start"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True,
                         **({} if os.name == "nt" else {"start_new_session": True}))
    base = default_base(kind)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        with suppress(Exception):
            if model_ids(base, timeout=2) is not None:
                return base
        time.sleep(1)
    return None


def advice(kind, ctx, want=32768):
    """What to change on the server so the cache holds, or "" when nothing: [lines]."""
    tips = []
    if kind == "ollama":
        if not ctx or ctx < want:
            tips.append(f"Ollama serves {ctx or OLLAMA_DEFAULT_CTX} tokens and cuts a longer prompt from the front "
                        f"without an error (the cache breaks): set OLLAMA_CONTEXT_LENGTH={want}")
        if not env_value("OLLAMA_KEEP_ALIVE"):
            tips.append("Ollama unloads the model after 5 idle minutes (the cache is lost): set OLLAMA_KEEP_ALIVE=30m")
    elif kind == "lmstudio" and ctx and ctx < want:
        tips.append(f"LM Studio loaded the model with {ctx} tokens: reload it with a larger context "
                    f"(My Models -> model -> Context Length, or: lms load <model> --context-length {want})")
    elif kind == "llamacpp" and ctx and ctx < 16384:
        tips.append(f"llama-server gives each slot {ctx} tokens (-c is split between -np slots): use -c {want * 2} "
                    "-np 2 or -np 1")
    return tips


def set_user_env(values):
    """Writes user environment variables (HKCU\\Environment) and tells Windows; programs started later see them.
    macOS: launchctl setenv (apps started from now on, Ollama.app too); Linux: ~/.config/environment.d (the
    systemd user session, from the next login) and this process."""
    if os.name != "nt":
        return _set_posix_env(values)
    import ctypes
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
        for name, value in values.items():
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, str(value))
    with suppress(Exception):
        ctypes.windll.user32.SendMessageTimeoutW(ctypes.c_void_p(0xFFFF), 0x1A, ctypes.c_void_p(0),
                                                 ctypes.c_wchar_p("Environment"), 2, 5000,
                                                 ctypes.byref(ctypes.c_size_t()))


# ---------- macOS / Linux ----------

POSIX_BIN = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", str(Path.home() / ".local" / "bin"))
ENV_D = Path.home() / ".config" / "environment.d" / "90-maxedcode.conf"     # Linux: set_user_env writes here
POSIX_ENV = ("OLLAMA_CONTEXT_LENGTH", "OLLAMA_KEEP_ALIVE", "STRATA_DIR")      # what launchctl getenv is asked


def _which(name, *more):
    """A program on PATH or in the usual bin folders (an app started from the dock does not get the shell's PATH)."""
    found = shutil.which(name)
    if found:
        return found
    for d in POSIX_BIN:
        p = Path(d, name)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return next((str(p) for p in more if p and Path(p).is_file()), None)


def _app(name):
    """A macOS application bundle in /Applications or ~/Applications."""
    return _exists("/Applications", name) or _exists(Path.home(), "Applications", name)


def _posix_env():
    """Variables set_user_env wrote before: launchctl on macOS, the environment.d file on Linux."""
    out = {}
    if sys.platform == "darwin":
        for name in POSIX_ENV:
            with suppress(Exception):
                v = subprocess.run(["launchctl", "getenv", name], capture_output=True, text=True, timeout=3).stdout
                if v.strip():
                    out[name] = v.strip()
        return out
    with suppress(OSError):
        for line in ENV_D.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() and not k.strip().startswith("#"):
                out[k.strip()] = v.strip().strip('"')
    return out


def _set_posix_env(values):
    values = {str(k): str(v) for k, v in values.items()}
    os.environ.update(values)
    if sys.platform == "darwin":
        for name, value in values.items():
            with suppress(Exception):
                subprocess.run(["launchctl", "setenv", name, value], capture_output=True, timeout=5)
        return
    current = _posix_env()
    current.update(values)
    ENV_D.parent.mkdir(parents=True, exist_ok=True)
    ENV_D.write_text("# written by maxedcode (model server settings)\n" +
                     "".join(f"{k}={v}\n" for k, v in current.items()), encoding="utf-8")


def _installed_posix():
    """installed() on macOS and Linux: the same servers in their own places (Homebrew, ~/.lmstudio, .app bundles)."""
    home, out, mac = Path.home(), [], sys.platform == "darwin"
    strata = _which("strata")
    src = strata_source()
    sp = strata or _exists(home, ".strata")
    if sp or src:
        # `strata launch` asks for the model in its own terminal: there is no console to open for it from here
        out.append({"kind": "strata", "path": str(src or sp), "start": None,
                    "how": "strata launch (in a terminal; no questions: strata launch --preset \"<name>\" --no-tui)"})
    lms = _which("lms", home / ".lmstudio" / "bin" / "lms", home / ".cache" / "lm-studio" / "bin" / "lms")
    lp = (_app("LM Studio.app") if mac else None) or lms or _exists(home, ".lmstudio") or \
        _exists(home, ".cache", "lm-studio")
    if lp:
        out.append({"kind": "lmstudio", "path": str(lp), "start": [str(lms), "server", "start"] if lms else None,
                    "how": "LM Studio: Developer -> Start Server (or: lms server start)"})
    oll = _which("ollama", "/Applications/Ollama.app/Contents/Resources/ollama")
    oapp = _app("Ollama.app") if mac else None
    if oll or oapp:
        out.append({"kind": "ollama", "path": str(oapp or oll),
                    "start": ["open", "-a", str(oapp)] if oapp else [str(oll), "serve"],
                    "how": "start the Ollama app (or: ollama serve)" if mac else
                           "systemctl start ollama (or: ollama serve)"})
    llama = _which("llama-server")
    if llama:
        out.append({"kind": "llamacpp", "path": llama, "start": None,
                    "how": "llama-server -m model.gguf --port 8080 -c 32768"})
    vllm = _which("vllm")
    if vllm:
        out.append({"kind": "vllm", "path": vllm, "start": None, "how": "vllm serve <model> --port 8000"})
    jan = (_app("Jan.app") if mac else None) or _exists(home, ".config", "Jan") or _exists(home, "jan")
    if jan:
        out.append({"kind": "jan", "path": str(jan), "start": None, "how": "Jan: Settings -> Local API Server -> Start"})
    for x in out:
        x["label"] = LABELS[x["kind"]]
    return out


def main(argv):
    """python -m maxedcode.servers [scan|start <kind>] -> one JSON line (the window uses it)."""
    if len(argv) > 2 and argv[1] == "start":
        print(json.dumps({"base": start(argv[2])}))
    else:
        print(json.dumps({"servers": scan()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
