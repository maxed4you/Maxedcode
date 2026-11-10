"""Auto-update: a newer release is downloaded in the background and installed at the next start.

    check()   at most once a day (CHECK_EVERY): the latest GitHub release. Newer than VERSION: its maxedcode.zip
              (the program alone, as install.ps1 takes it) is unpacked into <data>\\update\\<version> and its Python
              packages are made sure of there and then (install.py --prepare), so the install itself is quick:
              the app window waits only 30 s for its server.
    apply()   local_agent.py, before anything else is loaded: a downloaded newer version goes into this install
              (install.py --update: the program files; config.json, chats, settings, shortcuts and PATH stay) when
              no other maxedcode of this install runs; local_agent.py then starts again on the new code.

Off: "auto_update": false in ~\\.maxedcode\\settings.json (the app: Settings -> General), MAXEDCODE_NO_UPDATE=1,
MAXEDCODE_OFFLINE=1. Never for a git working copy or a copy outside an install (<root>\\program). Only GitHub is
asked, with a generic User-Agent: nothing about the user, the PC or the projects. Stdlib only.
"""
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

from .version import VERSION

REPO = "maxed4you/Maxedcode"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET = "https://github.com/" + REPO + "/releases/download/v{version}/maxedcode.zip"
CHECK_EVERY = 24 * 3600
PROGRAM = Path(__file__).resolve().parent.parent          # <install>\program (agent\ in the repository)
DATA = Path(os.environ.get("AGENT_HOME") or Path.home() / ".maxedcode")
DIR = DATA / "update"
STATE = DIR / "state.json"
RUNNING = DIR / "running"                                 # one file per running maxedcode: <pid> -> its program folder
STALE_LOCK = 3600


def parse(version):
    """"v1.2.10" -> (1, 2, 10); () for anything else."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", str(version or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def newer(a, b):
    """True when version a is a real version above b."""
    return bool(parse(a)) and parse(a) > parse(b)


def install_root(program=None):
    """The install folder (the one holding program\\), or None: a git working copy is never updated."""
    p = Path(program or PROGRAM)
    if p.name != "program" or not (p / "install.py").exists() or (p / ".git").exists() or (p.parent / ".git").exists():
        return None
    return p.parent


def _read(path):
    with suppress(Exception):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    return {}


def enabled(manual=False):
    """Updates may run: an install, and (unless asked by hand: maxed --update) automatic updates are not off."""
    if install_root() is None or os.environ.get("MAXEDCODE_OFFLINE"):
        return False
    return manual or (not os.environ.get("MAXEDCODE_NO_UPDATE") and
                      _read(DATA / "settings.json").get("auto_update", True) is not False)


def state():
    return _read(STATE)


def _save(st):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
    os.replace(tmp, STATE)


def _change(**values):
    st = state()
    st.update(values)
    for k in [k for k, v in st.items() if v is None]:
        del st[k]
    _save(st)
    return st


class _Lock:
    """One process at a time for a step (O_EXCL file); a lock left by a killed process expires after STALE_LOCK."""

    def __init__(self, name):
        self.path = DIR / name
        self.held = False

    def __enter__(self):
        DIR.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            if time.time() - self.path.stat().st_mtime > STALE_LOCK:
                self.path.unlink()
        with suppress(OSError):
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            self.held = True
        return self.held

    def __exit__(self, *exc):
        if self.held:
            with suppress(OSError):
                self.path.unlink()


def _open(url, timeout):
    import urllib.request              # only when it looks: a start without anything to do stays quick
    req = urllib.request.Request(url, headers={"User-Agent": "maxedcode-updater",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def latest(timeout=8):
    """The version of the latest GitHub release ("1.0.1")."""
    with _open(LATEST, timeout) as r:
        tag = json.loads(r.read().decode("utf-8")).get("tag_name", "")
    if not parse(tag):
        raise ValueError(f"unexpected release tag {tag!r}")
    return tag.lstrip("v")


def version_of(program):
    """The VERSION written in <program>\\maxedcode\\version.py ("" when there is none)."""
    with suppress(OSError):
        m = re.search(r'VERSION\s*=\s*"([^"]+)"', (Path(program) / "maxedcode" / "version.py").read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    return ""


def unpack(zip_path, target):
    """The release zip into target (no path may leave it) -> the folder that holds install.py."""
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    root = target.resolve()
    import zipfile
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            out = (target / name).resolve()
            if out != root and root not in out.parents:
                raise ValueError(f"bad path in the update: {name}")
        z.extractall(target)
    if (target / "install.py").exists():
        return target
    inner = [d for d in target.iterdir() if (d / "install.py").exists()]
    if len(inner) == 1:
        return inner[0]
    raise ValueError("the update has no install.py")


def stage(version, prepare=True, timeout=120):
    """Downloads and unpacks release `version`; ready for apply() once its packages import. -> True when ready."""
    DIR.mkdir(parents=True, exist_ok=True)
    part = DIR / f"{version}.zip.part"
    with _open(ASSET.format(version=version), timeout) as r, open(part, "wb") as f:
        shutil.copyfileobj(r, f)
    try:
        src = unpack(part, DIR / version)
    finally:
        with suppress(OSError):
            part.unlink()
    if version_of(src) != version:
        shutil.rmtree(DIR / version, ignore_errors=True)
        raise ValueError(f"the release {version} carries version {version_of(src) or '?'}")
    if prepare:       # pip only when this version's packages do not import yet (they rarely change)
        r = subprocess.run([sys.executable, str(src / "install.py"), "--prepare"], capture_output=True, text=True,
                           timeout=900, stdin=subprocess.DEVNULL)
        if r.returncode:
            _change(error=f"packages for {version}: " + (r.stdout + r.stderr).strip()[-300:])
            return False
    for old in DIR.iterdir():          # older downloads
        if old.is_dir() and old.name not in (version, RUNNING.name):
            shutil.rmtree(old, ignore_errors=True)
    _change(ready=version, dir=str(src), error=None)
    return True


def check(force=False, prepare=True, manual=False):
    """The daily look at GitHub (force: now); a newer release is staged. -> the state."""
    if not enabled(manual):
        return state()
    st = state()
    if not force and time.time() - st.get("checked", 0) < CHECK_EVERY:
        return st
    with _Lock("check.lock") as mine:
        if not mine:
            return st
        try:
            version = latest()
        except Exception as e:
            return _change(checked=time.time(), error=f"no answer from GitHub: {type(e).__name__}")
        st = _change(checked=time.time(), latest=version, error=None)
        if newer(version, VERSION) and st.get("ready") != version and st.get("failed") != version:
            try:
                stage(version, prepare)
            except Exception as e:
                _change(error=f"download of {version}: {type(e).__name__}: {e}"[:300])
    return state()


def check_async(done=None):
    """check() off the caller's thread; done(state) afterwards when a newer version is ready."""
    def work():
        with suppress(Exception):
            st = check()
            if done and newer(st.get("ready"), VERSION):
                done(st)
    threading.Thread(target=work, daemon=True).start()


def _alive(pid):
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
    finally:
        k.CloseHandle(h)


def register():
    """This process is a running maxedcode of PROGRAM (until it exits): no update while it runs."""
    with suppress(OSError):
        RUNNING.mkdir(parents=True, exist_ok=True)
        f = RUNNING / str(os.getpid())
        f.write_text(str(PROGRAM), encoding="utf-8")
        atexit.register(lambda: f.unlink(missing_ok=True))


def others():
    """Other running maxedcode processes of this install (the files of finished ones are removed)."""
    alive = []
    with suppress(OSError):
        for f in RUNNING.iterdir():
            with suppress(ValueError, OSError):
                pid = int(f.name)
                if pid == os.getpid():
                    continue
                if not _alive(pid):
                    f.unlink()
                elif f.read_text(encoding="utf-8").strip() == str(PROGRAM):
                    alive.append(pid)
    return alive


def apply(say=None, manual=False):
    """Installs a staged newer version into this install. -> the version installed, or None."""
    say = say or (lambda text: print("  maxedcode: " + text, file=sys.stderr, flush=True))
    st = state()
    version, src = st.get("ready"), Path(st.get("dir") or ".")
    if not version:
        return None
    if not newer(version, VERSION) or not (src / "install.py").exists():
        if src.name == version:                  # already there (an npm update, a setup run by hand)
            shutil.rmtree(src, ignore_errors=True)
        _change(ready=None, dir=None)
        return None
    root = install_root()
    if not enabled(manual) or root is None or others():
        return None
    with _Lock("apply.lock") as mine:
        if not mine:
            return None
        say(f"updating {VERSION} -> {version} (your server, chats and settings stay) ...")
        try:
            r = subprocess.run([sys.executable, str(src / "install.py"), "--update", "--dir", str(root)],
                               capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
            ok = r.returncode == 0 and version_of(PROGRAM) == version
            out = (r.stdout + r.stderr).strip()
        except (OSError, subprocess.SubprocessError) as e:
            ok, out = False, str(e)
        if not ok:
            _change(ready=None, dir=None, failed=version, error=f"install of {version}: {out[-300:]}")
            say(f"the update to {version} did not install; the log: %TEMP%\\maxedcode-setup.log")
            return None
        shutil.rmtree(src, ignore_errors=True)
        _change(ready=None, dir=None, failed=None, error=None, updated=version, notice=version)
        say(f"updated to {version}")
        return version


def take_notice():
    """The version just installed, once (for "maxedcode was updated to ..."), else ""."""
    st = state()
    if st.get("notice"):
        _change(notice=None)
        return st["notice"]
    return ""


def status():
    """For the app window: this version, the latest one seen, a downloaded one, whether updates are on."""
    st = state()
    return {"version": VERSION, "latest": st.get("latest", ""), "checked": st.get("checked", 0),
            "ready": st.get("ready", "") if newer(st.get("ready"), VERSION) else "", "enabled": enabled(),
            "installed": install_root() is not None, "error": st.get("error", ""),
            "auto": _read(DATA / "settings.json").get("auto_update", True) is not False}


def main(argv):
    """maxed --update: look now, download and install when nothing else of this install runs."""
    if install_root() is None:
        print(f"maxedcode {VERSION}: this copy is not an install (a git working copy?); it is not updated")
        return 0
    print(f"maxedcode {VERSION}: looking for a newer release ...")
    st = check(force=True, manual=True)
    if st.get("error"):
        print("  " + st["error"])
    if not newer(st.get("ready"), VERSION):
        print(f"  up to date (the latest release is {st.get('latest') or '?'})")
        return 0 if not st.get("error") else 1
    if others():
        print(f"  {st['ready']} is downloaded; it installs when every maxedcode window and terminal is closed "
              "(or at the next start)")
        return 0
    return 0 if apply(say=lambda t: print("  " + t), manual=True) else 1
