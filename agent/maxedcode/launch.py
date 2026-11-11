"""Start and stop a model server on this PC with the model and the context the user picks, in one click.

The window (Settings -> Models, the model menu under the input) and the CLI (/run) use it. One adapter per server:

* Strata     - `strata launch --model <size> --context <n> --no-tui --yes` (the server runs on, the command
               returns); `strata launch --stop`. Own start scripts (.bat / .sh) are started as they are, in a
               window of their own: <strata>\\launcher\\bat\\*.bat and any file the user picks.
* LM Studio  - `lms server start`, `lms unload --all`, `lms load <model> --context-length <n> -y`.
* Ollama     - the window is OLLAMA_CONTEXT_LENGTH (a request through /v1 cannot set it: Ollama reloads the
               model with its default window), so it is written for the user and the server is restarted
               when it serves another one; the model is loaded with keep_alive.
* llama.cpp  - `llama-server -m <file.gguf> -c <n>` on port 8080; GGUF files are found in the usual folders
               (LM Studio's model folder, the Hugging Face and llama.cpp caches, ~/models).

    python -m maxedcode.launch get                  -> {"servers": [...], "last": {...}}
    python -m maxedcode.launch start < {"kind", "model", "ctx", "script"}
                                                    -> progress lines {"text", "pct"?}, the last one
                                                       {"done": true, "ok", "base", "model", "ctx", "error"}
    python -m maxedcode.launch stop < {"kind"}      -> {"ok", "text"}

A started server is chosen for the agent at once (appsettings: base_url, server, model). Standard library only,
apart from maxedcode.servers (and appsettings / config when a start is saved).
"""
import concurrent.futures as cf
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
from contextlib import suppress
from pathlib import Path

from . import servers

KINDS = ("strata", "lmstudio", "ollama", "llamacpp")
DOWNLOAD = {"strata": "https://github.com/Niko1221/Strata", "lmstudio": "https://lmstudio.ai/download",
            "ollama": "https://ollama.com/download", "llamacpp": "https://github.com/ggml-org/llama.cpp/releases"}
# what each one is, for someone who has none of them yet
ABOUT = {"strata": "fast engine for big MoE models on one GPU",
         "lmstudio": "an app with a model catalog: the easiest start",
         "ollama": "a small background service: `ollama pull <model>`",
         "llamacpp": "llama-server and a .gguf file"}
CTX_STEPS = (16384, 32768, 65536, 131072, 262144)
DEFAULT_CTX = 32768
WAIT = {"strata": 900, "lmstudio": 900, "ollama": 600, "llamacpp": 600}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | NO_WINDOW


# ---------- own state: the last choice per server, started processes, own scripts ----------

def home():
    with suppress(Exception):
        from . import config
        return Path(config.HOME)
    return Path.home() / ".maxedcode"


def state_file():
    return home() / "launch.json"


def load_state():
    with suppress(Exception):
        data = json.loads(state_file().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    return {}


def save_state(data):
    path = state_file()
    with suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)


def remember(kind, model=None, ctx=None, script=None, pid=None, **extra):
    st = load_state()
    st["kind"] = kind
    if model is not None:
        st.setdefault("model", {})[kind] = model
    if ctx and model:
        st.setdefault("ctx", {})[f"{kind}|{model}"] = int(ctx)
    if script is not None:
        st.setdefault("script", {})[kind] = script
        if script:
            scripts = [s for s in st.get("scripts", []) if s != script]
            st["scripts"] = ([script] + scripts)[:8]
    if pid is not None:
        st.setdefault("pid", {})[kind] = pid
    for k, v in extra.items():
        st[k] = v
    save_state(st)


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
    say(f"stopping {servers.LABELS.get(other, other)} on port {port} first")
    stop(other)
    return wait_down(base, 30)


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
        if proc is not None and proc.poll() not in (None, 0):      # it ended with an error
            return False
        last = (tail(log, 1) or [""])[-1] if log else ""
        text = f"waiting for {what}… {int(time.monotonic() - start)} s" + (f" · {last[:140]}" if last else "")
        if text != shown:
            say(text)
            shown = text
        time.sleep(1.5)
    return False


# ---------- Strata ----------

def strata_cli():
    src = servers.strata_source()
    if src:
        exe = src / "bin" / ("strata.cmd" if os.name == "nt" else "strata")
        if exe.is_file():
            return [str(exe)], src
    on_path = shutil.which("strata")
    return ([on_path], None) if on_path else (None, None)


def strata_models():
    cli, _ = strata_cli()
    if not cli:
        return []
    code, out = run([*cli, "models", "list", "--installed", "--output", "json"], 60)
    data = _json(out)
    models = []
    for m in (data or {}).get("models") or []:
        if isinstance(m, dict) and m.get("id") and m.get("component_status", "present") == "present":
            models.append({"id": str(m["id"]), "name": str(m.get("name") or m["id"]), "ctx": m.get("context_tokens"),
                           "max_ctx": None, "size": None})
    return models


def strata_scripts():
    """The user's own start scripts: <strata>\\launcher\\bat\\*.bat (written by a launcher window) and the files
    picked before."""
    _, src = strata_cli()
    found = []
    if src:
        for d in (src / "launcher" / "bat", src / "launcher" / "sh"):
            with suppress(OSError):
                found += sorted(str(p) for p in d.iterdir() if p.suffix.lower() in (".bat", ".cmd", ".sh", ".ps1"))
    found += [s for s in load_state().get("scripts", []) if Path(s).is_file()]
    seen, out = set(), []
    for s in found:
        if s.lower() not in seen:
            seen.add(s.lower())
            out.append({"path": s, "name": Path(s).stem})
    return out


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


def strata_start(model, ctx, say, script=None):
    base = servers.default_base("strata")
    log = log_file("strata")
    if script:
        path = Path(script)
        if not path.is_file():
            return _fail(f"the start script is not there: {script}")
        if not free_port("strata", say):
            return _fail("port 8080 is still taken")
        say(f"starting {path.name} in its own window")
        if os.name == "nt":
            spawn(["cmd", "/c", "start", f"Strata · {path.stem}", str(path)], cwd=str(path.parent))
        else:
            spawn(["sh", str(path)], log=log, cwd=str(path.parent))
        remember("strata", script=str(path))
        if not wait_up(base, WAIT["strata"], say, what="Strata (its window shows the progress)",
                       log=None if os.name == "nt" else log):
            return _fail("Strata did not answer on 127.0.0.1:8080: see its window")
        return _done("strata", base, None)
    cli, src = strata_cli()
    if not cli:
        return _fail("the strata command is not found: install Strata or pick its start script")
    if not model:
        return _fail("pick a model")
    if not free_port("strata", say):
        return _fail("port 8080 is still taken")
    flags = strata_flags(cli, model, ctx)
    log.write_text("", encoding="utf-8")
    say(f"strata launch {' '.join(flags)} --context {ctx}")
    proc = spawn([*cli, "launch", *flags, "--context", str(ctx), "--no-tui", "--yes"], log=log,
                 cwd=str(src) if src else None,
                 env={**os.environ, "PYTHONUTF8": "1", "STRATA_NO_PROMPT": "1"})
    remember("strata", model=model, ctx=ctx, script="")
    if not wait_up(base, WAIT["strata"], say, proc=proc, log=log, what="Strata to load the model"):
        return _fail("Strata did not start: " + (" | ".join(tail(log, 3)) or "no output"))
    return _done("strata", base, None)


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
    h = Path.home()
    exe = "lms.exe" if os.name == "nt" else "lms"
    found = shutil.which("lms") or next((str(p) for p in (h / ".lmstudio" / "bin" / exe,
                                                          h / ".cache" / "lm-studio" / "bin" / exe) if p.is_file()), None)
    return found


def lms_models():
    base = servers.default_base("lmstudio")
    lms = lms_cli()
    models = []
    if lms:
        code, out = run([lms, "ls", "--json"], 30)
        with suppress(ValueError):
            for m in json.loads(out[out.index("["):]):
                if isinstance(m, dict) and m.get("modelKey") and m.get("type") in ("llm", "vlm", None):
                    models.append({"id": m["modelKey"], "name": m.get("displayName") or m["modelKey"],
                                   "size": m.get("sizeBytes"), "max_ctx": m.get("maxContextLength"),
                                   "vision": bool(m.get("vision")), "params": m.get("paramsString"),
                                   "quant": (m.get("quantization") or {}).get("name")})
    if not models and not servers.port_down(base):
        d = servers.get(servers.root(base) + "/api/v0/models")
        for m in (d or {}).get("data") or [] if isinstance(d, dict) else []:
            if isinstance(m, dict) and m.get("id") and m.get("type") in ("llm", "vlm", None):
                models.append({"id": m["id"], "name": m["id"], "size": None, "max_ctx": m.get("max_context_length"),
                               "vision": m.get("type") == "vlm", "quant": m.get("quantization")})
    return models


def lms_start(model, ctx, say, script=None, force=False):
    base = servers.default_base("lmstudio")
    lms = lms_cli()
    if not lms:
        return _fail("LM Studio's command `lms` is not found: open LM Studio once (it sets lms up), then try again")
    if not model:
        return _fail("pick a model")
    if servers.port_down(base):
        say("lms server start")
        run([lms, "server", "start"], 90)
        if not wait_up(base, 60, say, what="the LM Studio server"):
            return _fail("the LM Studio server did not start: open LM Studio -> Developer -> Start Server")
    on = servers.loaded_models(base, kind="lmstudio") or []
    if len(on) == 1 and servers.same_model(on[0][0], model) and (on[0][1] or 0) == int(ctx) and not force:
        say(f"{model} is already loaded with {ctx} tokens")
        remember("lmstudio", model=model, ctx=ctx)
        return _done("lmstudio", base, on[0][0])
    if on:
        say("lms unload --all")
        run([lms, "unload", "--all"], 120)
    say(f"lms load {model} --context-length {ctx} --parallel 1")
    log = log_file("lmstudio")
    log.write_text("", encoding="utf-8")
    # one slot: the agent sends one request at a time, and LM Studio's default 4 slots took 4x the cache memory
    # (an 8B model at 32K spilled out of a 12 GB card and answered 20 tokens in 45 s)
    cmd = [lms, "load", model, "--context-length", str(ctx), "-y"]
    if "--parallel" in run([lms, "load", "--help"], 20)[1]:
        cmd[3:3] = ["--parallel", "1"]
    proc = spawn(cmd, log=log)
    start = time.monotonic()
    while proc.poll() is None and time.monotonic() - start < WAIT["lmstudio"]:
        last = (tail(log, 1) or [""])[-1]
        pct = re.findall(r"(\d+(?:\.\d+)?)\s*%", last)
        say(f"loading {model}… {int(time.monotonic() - start)} s", float(pct[-1]) if pct else None)
        time.sleep(1)
    on = servers.loaded_models(base, kind="lmstudio") or []
    hit = next((m for m in on if servers.same_model(m[0], model) or m[0].startswith(model)), None)
    if not hit:
        return _fail("LM Studio did not load the model: " + " | ".join(tail(log, 3)))
    remember("lmstudio", model=model, ctx=ctx)
    return _done("lmstudio", base, hit[0])


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


def _ollama_env(ctx):
    keep = servers.env_value("OLLAMA_KEEP_ALIVE") or "30m"
    return {"OLLAMA_CONTEXT_LENGTH": str(ctx), "OLLAMA_KEEP_ALIVE": keep}, keep


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


def ollama_start(model, ctx, say, script=None):
    base = servers.default_base("ollama")
    r = servers.root(base)
    exe = ollama_cli()
    if not model:
        return _fail("pick a model (none downloaded? in a terminal: ollama pull qwen3:8b)")
    env, keep = _ollama_env(ctx)
    say(f"OLLAMA_CONTEXT_LENGTH={ctx} (saved for you, so Ollama started any other way uses it too)")
    with suppress(Exception):
        servers.set_user_env(env)

    def serve():
        if not exe:
            return False
        say("ollama serve")
        spawn([exe, "serve"], log=log_file("ollama"), env={**os.environ, **env})
        return wait_up(base, 60, say, what="Ollama", check=lambda: isinstance(servers.get(r + "/api/version"), dict))

    def load():
        for m, _ in servers.loaded_models(base, kind="ollama") or []:
            if not servers.same_model(m, model):
                with suppress(Exception):
                    servers.http(r + "/api/generate", body={"model": m, "keep_alive": 0}, timeout=60)
        say(f"loading {model}…")
        try:
            servers.http(r + "/api/generate", body={"model": model, "keep_alive": keep}, timeout=WAIT["ollama"])
        except urllib.error.HTTPError as e:
            return str(e.read().decode("utf-8", "replace") or e)[:300]
        except Exception as e:
            return str(e)[:300]
        return ""

    def window():
        hit = next((m for m in servers.loaded_models(base, kind="ollama") or [] if servers.same_model(m[0], model)), None)
        return hit[1] if hit else None

    if servers.port_down(base) and not serve():
        return _fail("Ollama did not start: start it from the Start menu (or: ollama serve)")
    err = load()
    if not err and (window() or 0) != int(ctx):
        say("Ollama serves another context: restarting it with the new one")
        ollama_kill()
        wait_down(base, 20)
        if not serve():
            return _fail("Ollama did not start again: start it from the Start menu")
        err = load()
    if err:
        return _fail(f"Ollama did not load {model}: {err}")
    remember("ollama", model=model, ctx=ctx)
    return _done("ollama", base, model)


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
    saved = load_state().get("llama_server")
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


def gguf_files(limit=200):
    """GGUF models on this PC: no vision projectors (mmproj), only the first part of a split model."""
    found = []
    for root in gguf_dirs():
        for dirpath, dirnames, files in os.walk(root):
            if len(Path(dirpath).relative_to(root).parts) >= 5:
                dirnames[:] = []
            for f in files:
                if not f.lower().endswith(".gguf") or f.lower().startswith("mmproj"):
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


def llama_models():
    out = []
    for p, size in gguf_files():
        out.append({"id": str(p), "name": SHARD.sub(".gguf", p.name)[:-5], "size": size, "max_ctx": gguf_ctx(p),
                    "vision": any(x.name.lower().startswith("mmproj") for x in p.parent.glob("*.gguf")),
                    "path": str(p.parent)})
    return out


def llama_start(model, ctx, say, script=None):
    base = servers.default_base("llamacpp")
    exe = llama_cli()
    if not exe:
        return _fail("llama-server is not found: download llama.cpp (llama-server) and put it on PATH")
    if not model or not Path(model).is_file():
        return _fail("pick a .gguf model file")
    if not free_port("llamacpp", say):
        return _fail("port 8080 is still taken")
    cmd = [exe, "-m", model, "-c", str(ctx), "--host", "127.0.0.1", "--port", "8080", "--jinja"]
    code, helptext = run([exe, "--help"], 20)
    if "--fit" not in helptext:         # older builds put nothing on the GPU unless asked
        cmd += ["-ngl", "999"]
    proj = sorted(Path(model).parent.glob("mmproj*.gguf"))
    if proj:
        cmd += ["--mmproj", str(proj[0])]
    log = log_file("llamacpp")
    log.write_text("", encoding="utf-8")
    say(" ".join(Path(x).name if i in (0, 2) else x for i, x in enumerate(cmd)))
    proc = spawn(cmd, log=log)
    remember("llamacpp", model=model, ctx=ctx, pid=proc.pid)
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


def status():
    """What runs on the usual ports now: {kind: describe()} (8080 is Strata or llama.cpp: asked which)."""
    out = {}

    def one(kind):
        base = servers.default_base(kind)
        if servers.port_down(base):
            return None
        info = servers.describe(base, kind="" if port_of(kind) == 8080 else kind, timeout=3)
        return info if info.get("online") and (info["kind"] == kind or port_of(kind) != 8080) else None
    with cf.ThreadPoolExecutor(4) as pool:
        for kind, info in zip(KINDS, pool.map(one, KINDS)):
            if info:
                out[kind] = info
    return out


def catalog():
    """Every server this window can start: installed or not, running or not, its models and contexts."""
    st = load_state()
    found = {x["kind"]: x for x in servers.installed()}
    with cf.ThreadPoolExecutor(5) as pool:
        running = pool.submit(status)
        lists = {k: pool.submit(_safe, MODELS[k]) for k in KINDS}
        clis = {k: pool.submit(_safe, CLI[k]) for k in KINDS}
        running = running.result()
        result = []
        for kind in KINDS:
            info = running.get(kind)
            models = lists[kind].result() or []
            cli = clis[kind].result()
            loaded = {}
            if info:
                on = servers.loaded_models(servers.default_base(kind), kind=kind) if kind in servers.JIT_KINDS else None
                name = on[0][0] if on else info.get("model")
                loaded = {"model": name, "ctx": (on[0][1] if on else None) or info.get("ctx")}
                if kind == "strata" and not name:
                    loaded["model"] = (servers.model_ids(servers.default_base(kind)) or [None])[0]
            for m in models:
                m["ctxs"] = ctx_options(m.get("max_ctx"), m.get("ctx"), (st.get("ctx") or {}).get(f"{kind}|{m['id']}"))
                m["pick_ctx"] = default_ctx(kind, m, st)
                m["loaded"] = bool(loaded.get("model")) and (servers.same_model(loaded["model"], m["id"])
                                                             or str(loaded["model"]).startswith(m["id"])
                                                             or m["id"].lower() in str(loaded["model"]).lower())
            entry = {"kind": kind, "label": servers.LABELS[kind], "about": ABOUT[kind],
                     "installed": bool(kind in found or cli), "running": bool(info),
                     "base": servers.default_base(kind), "can_start": bool(cli) or kind == "strata" and bool(strata_scripts()),
                     "loaded": loaded, "models": models, "download": DOWNLOAD[kind],
                     "how": (found.get(kind) or {}).get("how", ""),
                     "last_model": (st.get("model") or {}).get(kind), "script": (st.get("script") or {}).get(kind) or ""}
            if kind == "strata":
                entry["scripts"] = strata_scripts()
            if kind == "llamacpp":
                entry["dirs"] = [str(d) for d in gguf_dirs()]
            if kind == "ollama" and not models:
                entry["hint"] = "ollama pull qwen3:8b"
            result.append(entry)
    return {"servers": result, "last": st.get("kind") or ""}


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
        appsettings.apply({"base_url": result["base"], "server": result["kind"],
                           "model": result.get("model") if result["kind"] in servers.JIT_KINDS else None})
        return True
    return False


def start(kind, model=None, ctx=None, script=None, say=None):
    say = say or (lambda text, pct=None: None)
    if kind not in START:
        return _fail(f"{kind or 'this server'} is not started from here")
    if servers.offline():
        return _fail("offline (MAXEDCODE_OFFLINE)")
    ctx = int(ctx or DEFAULT_CTX)
    try:
        result = START[kind](model, ctx, say, script=script)
    except Exception as e:            # a broken install must end the job with words, not a traceback
        result = _fail(f"{type(e).__name__}: {e}")
    if result.get("ok"):
        result["ctx"] = ctx
        live = servers.describe(result["base"], kind=kind, model=result.get("model"))
        result["ctx"] = live.get("loaded_ctx") or live.get("ctx") or ctx
        result["model"] = result.get("model") or live.get("model") or live.get("use")
        result["saved"] = use(result)
    return result


def stop(kind, say=None):
    say = say or (lambda text, pct=None: None)
    if kind not in STOP:
        return {"ok": False, "text": f"{kind} is not stopped from here"}
    try:
        ok = STOP[kind](say)
    except Exception as e:
        return {"ok": False, "text": f"{type(e).__name__}: {e}"}
    return {"ok": bool(ok), "text": "stopped" if ok else "it still answers"}


def main(argv):
    command = argv[1] if len(argv) > 1 else "get"
    body = {}
    if command in ("start", "stop"):
        with suppress(ValueError):
            body = json.loads(sys.stdin.read() or "{}")

    def say(text, pct=None):
        print(json.dumps({"text": text, **({"pct": pct} if pct is not None else {})}, ensure_ascii=False), flush=True)
    if command == "start":
        print(json.dumps(start(body.get("kind"), body.get("model"), body.get("ctx"), body.get("script"), say),
                         ensure_ascii=False), flush=True)
    elif command == "stop":
        print(json.dumps({"done": True, **stop(body.get("kind"), say)}, ensure_ascii=False), flush=True)
    else:
        print(json.dumps(catalog(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
