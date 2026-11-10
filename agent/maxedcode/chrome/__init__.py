"""The user's own browser for the agent: the extension (extension/), its native messaging host (host.py) and the
switch that turns both on.

    python -m maxedcode.chrome on       register the host for Chrome, Edge, Brave, Chromium, Yandex, Vivaldi (HKCU,
                                        no admin; macOS / Linux: the manifest in each browser's user folder) and
                                        turn the browser tool on (settings.json "browser_tool")
    python -m maxedcode.chrome off      remove both
    python -m maxedcode.chrome status   JSON: on, registered, connected browsers, the extension folder
    python -m maxedcode.chrome guide    open the step-by-step guide (guide/index.html) in the default browser
    python -m maxedcode.chrome reveal   show the extension folder in Explorer (to drag it onto chrome://extensions)

The extension itself is loaded once by the user (chrome://extensions → Developer mode → Load unpacked → the
extension folder; guide/ shows it with screenshots): Chrome installs nothing on its own from outside the web store.
"""
import json
import os
import socket
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

if os.name == "nt":
    import winreg

NAME = "com.maxedcode.browser"
EXTENSION_ID = "dakikiblpdhjjkapncflecjjjfecifpo"      # fixed by the "key" in extension/manifest.json
HERE = Path(__file__).resolve().parent
EXTENSION = HERE / "extension"
HOST = HERE / "host.py"
DIR = Path.home() / ".maxedcode-app" / "browser"


def live_dir():
    """Where running hosts announce themselves (host.py LIVE): a test browser uses its own folder."""
    return Path(os.environ.get("MAXEDCODE_BROWSER_LIVE") or DIR)
SETTINGS = Path.home() / ".maxedcode" / "settings.json"
SETTING = "browser_tool"
# registry roots where each browser looks for native messaging hosts (HKCU\<root>\NativeMessagingHosts\<name>)
BROWSERS = {
    "Chrome": r"Software\Google\Chrome",
    "Edge": r"Software\Microsoft\Edge",
    "Brave": r"Software\BraveSoftware\Brave-Browser",
    "Chromium": r"Software\Chromium",
    "Yandex": r"Software\Yandex\YandexBrowser",
    "Vivaldi": r"Software\Vivaldi",
}
GUIDE = HERE / "guide" / "index.html"      # how to load the extension, with screenshots (English)
# macOS / Linux: no registry; each browser reads <its user folder>/NativeMessagingHosts/<NAME>.json
# (folders relative to ~/Library/Application Support on macOS, to ~/.config on Linux; "~/" = the home folder)
POSIX_BROWSERS = {
    "darwin": {
        "Chrome": "Google/Chrome",
        "Edge": "Microsoft Edge",
        "Brave": "BraveSoftware/Brave-Browser",
        "Chromium": "Chromium",
        "Yandex": "Yandex/YandexBrowser",
        "Vivaldi": "Vivaldi",
    },
    "linux": {
        "Chrome": "google-chrome",
        "Edge": "microsoft-edge",
        "Brave": "BraveSoftware/Brave-Browser",
        "Chromium": "chromium",
        "Chromium (snap)": "~/snap/chromium/common/chromium",
        "Yandex": "yandex-browser",
        "Vivaldi": "vivaldi",
    },
}


def posix_browsers():
    """{browser: its user folder} for this OS (macOS or Linux)."""
    mac = sys.platform == "darwin"
    home = Path.home()
    base = home / "Library" / "Application Support" if mac else home / ".config"
    return {name: home / rel[2:] if rel.startswith("~/") else base / rel
            for name, rel in POSIX_BROWSERS["darwin" if mac else "linux"].items()}


def posix_targets():
    """The NativeMessagingHosts folders to write: every installed browser's (its user folder exists); with none
    found yet, Chrome's and Chromium's (a browser installed later finds the manifest there)."""
    folders = posix_browsers()
    have = [d for d in folders.values() if d.is_dir()]
    if not have:
        have = [folders["Chrome"], folders["Chromium"]]
    return [d / "NativeMessagingHosts" for d in have]


def launcher_file():
    """The script the browser starts: host.bat on Windows, an executable shell script elsewhere."""
    return DIR / ("host.bat" if os.name == "nt" else "host.sh")


def python_exe():
    """The console python next to the running one (pythonw has no stdin/stdout for the host)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        exe = exe.with_name("python.exe")
    return exe


def manifest_file():
    return DIR / f"{NAME}.json"


def register():
    """Writes the host's launcher and manifest and points every browser's registry key at it."""
    if os.name != "nt":
        return _register_posix()
    DIR.mkdir(parents=True, exist_ok=True)
    launcher = DIR / "host.bat"
    # The paths lie under the user profile: a non-ASCII user name needs the UTF-8 code page (cmd reads a .bat in
    # the console's one, 866 on a Russian Windows); >nul keeps the native messaging stdout clean.
    launcher.write_text(f'@echo off\r\nchcp 65001 >nul 2>nul\r\n"{python_exe()}" -I "{HOST}" %*\r\n',
                        encoding="utf-8", newline="")
    manifest = {"name": NAME, "description": "maxedcode browser bridge", "path": str(launcher), "type": "stdio",
                "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"]}
    manifest_file().write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for root in BROWSERS.values():
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{root}\NativeMessagingHosts\{NAME}") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest_file()))


def unregister():
    if os.name != "nt":
        return _unregister_posix()
    for root in BROWSERS.values():
        with suppress(OSError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"{root}\NativeMessagingHosts\{NAME}")
    for name in ("host.bat", f"{NAME}.json"):
        with suppress(OSError):
            (DIR / name).unlink()


def registered():
    """The Chrome key points at our manifest, and that manifest launches this copy's host.py."""
    if os.name != "nt":
        return _registered_posix()
    with suppress(OSError, ValueError):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{BROWSERS['Chrome']}\NativeMessagingHosts\{NAME}") as key:
            if Path(winreg.QueryValue(key, None)) != manifest_file():
                return False
        return str(HOST) in (DIR / "host.bat").read_text(encoding="utf-8")
    return False


def _manifest(launcher):
    return {"name": NAME, "description": "maxedcode browser bridge", "path": str(launcher), "type": "stdio",
            "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"]}


def _register_posix():
    """macOS / Linux: an executable launcher (the right python on host.py) and the manifest in each browser's
    NativeMessagingHosts folder; a copy stays in DIR for status()."""
    import shlex
    DIR.mkdir(parents=True, exist_ok=True)
    launcher = launcher_file()
    launcher.write_text(f"#!/bin/sh\nexec {shlex.quote(str(python_exe()))} -I {shlex.quote(str(HOST))} \"$@\"\n",
                        encoding="utf-8", newline="\n")
    os.chmod(launcher, 0o755)
    text = json.dumps(_manifest(launcher), indent=2)
    manifest_file().write_text(text, encoding="utf-8")
    for folder in posix_targets():
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{NAME}.json").write_text(text, encoding="utf-8")


def _unregister_posix():
    for d in posix_browsers().values():
        with suppress(OSError):
            (d / "NativeMessagingHosts" / f"{NAME}.json").unlink()
    for name in ("host.sh", f"{NAME}.json"):
        with suppress(OSError):
            (DIR / name).unlink()


def _registered_posix():
    """Some browser's manifest launches our launcher, and the launcher runs this copy's host.py."""
    launcher = launcher_file()
    with suppress(OSError):
        if str(HOST) not in launcher.read_text(encoding="utf-8"):
            return False
        for d in posix_browsers().values():
            with suppress(OSError, ValueError):
                data = json.loads((d / "NativeMessagingHosts" / f"{NAME}.json").read_text(encoding="utf-8"))
                if Path(data.get("path") or "") == launcher:
                    return True
    return False


def hosts():
    """Hosts that are running now (one per connected browser), the last used first: the panel's own browser for its
    turns (AGENT_BROWSER_HOST), then the one the user focused last. Dead ones are cleaned up."""
    found = []
    for path in live_dir().glob("*.json"):
        if path.name == manifest_file().name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            with socket.create_connection(("127.0.0.1", int(data["port"])), timeout=1):
                pass
        except (OSError, ValueError, KeyError, TypeError):
            with suppress(OSError):
                path.unlink()
            continue
        found.append(data)
    # The one the user was in last: with Chrome and Edge both connected the newest start was the wrong one
    # (a second browser opened for a minute took over every task).
    preferred = os.environ.get("AGENT_BROWSER_HOST") or ""
    return sorted(found, key=lambda h: (str(h.get("pid")) != preferred,
                                        -max(float(h.get("focused") or 0), float(h.get("started") or 0))))


def _settings():
    with suppress(OSError, ValueError):
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    return {}


def tool_on():
    return str(os.environ.get("AGENT_BROWSER") or _settings().get(SETTING) or "").lower() in ("1", "true", "yes", "on")


def set_tool(on):
    data = _settings()
    if on:
        data[SETTING] = True
    else:
        data.pop(SETTING, None)
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, SETTINGS)


def status():
    return {"on": tool_on(), "registered": registered(), "extension": str(EXTENSION), "id": EXTENSION_ID,
            "connected": [h.get("browser") or "browser" for h in hosts()], "guide": str(GUIDE)}


def open_guide():
    """The step-by-step guide in the default browser. Chrome refuses chrome://extensions from a program (it opened a
    new tab instead), so the guide walks the user there; opened from disk it shows this computer's folder path."""
    if os.name != "nt":
        from .. import plat
        return plat.open_path(GUIDE)
    os.startfile(GUIDE)
    return True


def reveal():
    """Explorer with the extension folder selected: dragged onto chrome://extensions (Developer mode on) it loads
    without typing a path. A path pasted into the folder dialog's search box instead of its address bar only said
    "not found"."""
    if os.name != "nt":
        from .. import plat
        return plat.reveal(EXTENSION)
    subprocess.Popen(["explorer.exe", "/select,", str(EXTENSION)])
    return True


def turn(on):
    if on:
        register()
    else:
        unregister()
    set_tool(on)
    return status()


def main(argv):
    command = argv[1] if len(argv) > 1 else "status"
    if command in ("on", "off"):
        result = turn(command == "on")
    elif command == "guide":
        result = {"ok": open_guide()}
    elif command == "reveal":
        result = {"ok": reveal()}
    else:
        result = status()
    print(json.dumps(result, ensure_ascii=False))
    return 0

