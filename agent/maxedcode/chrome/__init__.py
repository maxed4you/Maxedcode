"""The user's own browser for the agent: the extension (extension/), its native messaging host (host.py) and the
switch that turns both on.

    python -m maxedcode.chrome on       register the host for Chrome, Edge, Brave, Chromium, Yandex, Vivaldi (HKCU,
                                        no admin) and turn the browser tool on (settings.json "browser_tool")
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
import winreg
from contextlib import suppress
from pathlib import Path

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
    for root in BROWSERS.values():
        with suppress(OSError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"{root}\NativeMessagingHosts\{NAME}")
    for name in ("host.bat", f"{NAME}.json"):
        with suppress(OSError):
            (DIR / name).unlink()


def registered():
    """The Chrome key points at our manifest, and that manifest launches this copy's host.py."""
    with suppress(OSError, ValueError):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{BROWSERS['Chrome']}\NativeMessagingHosts\{NAME}") as key:
            if Path(winreg.QueryValue(key, None)) != manifest_file():
                return False
        return str(HOST) in (DIR / "host.bat").read_text(encoding="utf-8")
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
    os.startfile(GUIDE)
    return True


def reveal():
    """Explorer with the extension folder selected: dragged onto chrome://extensions (Developer mode on) it loads
    without typing a path. A path pasted into the folder dialog's search box instead of its address bar only said
    "not found"."""
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

