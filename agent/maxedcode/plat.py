"""What differs between Windows, macOS and Linux outside the shell tool: opening files and links, the clipboard,
the file manager, the trash, desktop notifications and the native file dialogs.

Standard library only and no maxedcode imports (servers.py, the installer and chrome/host.py use it too).
On Windows the callers keep their own long-tested code; these helpers are what the POSIX branches call. Every
helper that needs an outside program looks it up first and gives up quietly (False / None) when it is missing.
"""
import os
import shutil
import subprocess
import sys

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def _detached(cmd, cwd=None):
    """Starts a program that outlives nothing of ours: its own session, no stdin/stdout tied to this process."""
    try:
        subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True,
                         **({} if IS_WIN else {"start_new_session": True}))
        return True
    except (OSError, ValueError):
        return False


def open_path(target):
    """A file, folder or URL in its default app (a double click in the file manager). False when nothing opened it."""
    target = str(target)
    if IS_WIN:
        try:
            os.startfile(target)          # noqa: the Windows-only API
            return True
        except OSError:
            return False
    if IS_MAC:
        return _detached(["open", target])
    opener = shutil.which("xdg-open") or shutil.which("gio")
    if not opener:
        return False
    return _detached([opener, "open", target] if opener.endswith("gio") else [opener, target])


def reveal(path):
    """The file manager with this file or folder selected (or its folder opened when selecting is not possible)."""
    path = os.path.abspath(str(path))
    if IS_WIN:
        return _detached(["explorer.exe", "/select,", path])
    if IS_MAC:
        return _detached(["open", "-R", path])
    gdbus = shutil.which("gdbus")
    if gdbus:      # the freedesktop file manager interface: Nautilus, Dolphin, Nemo, Thunar ... select the item
        from urllib.parse import quote
        uri = "file://" + quote(path)
        try:
            r = subprocess.run([gdbus, "call", "--session", "--dest", "org.freedesktop.FileManager1",
                                "--object-path", "/org/freedesktop/FileManager1",
                                "--method", "org.freedesktop.FileManager1.ShowItems", f"['{uri}']", ""],
                               capture_output=True, timeout=5)
            if r.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    return open_path(path if os.path.isdir(path) else os.path.dirname(path))


def clipboard_commands():
    """(copy command, paste command) for this desktop, either None when no clipboard program is installed.
    Wayland first when the session is Wayland (wl-clipboard), then X11 (xclip, xsel)."""
    if IS_WIN:
        return ["clip"], None
    if IS_MAC:
        return ["pbcopy"], ["pbpaste"]
    found = []
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        found.append((["wl-copy"], ["wl-paste", "--no-newline"]))
    if shutil.which("xclip"):
        found.append((["xclip", "-selection", "clipboard"], ["xclip", "-selection", "clipboard", "-o"]))
    if shutil.which("xsel"):
        found.append((["xsel", "--clipboard", "--input"], ["xsel", "--clipboard", "--output"]))
    if not found and shutil.which("wl-copy"):
        found.append((["wl-copy"], ["wl-paste", "--no-newline"]))
    return found[0] if found else (None, None)


def copy(text):
    """Text to the system clipboard. False when there is no clipboard program (the TUI also sends OSC 52)."""
    cmd = clipboard_commands()[0]
    if not text or not cmd:
        return False
    data = text.encode("utf-16-le") if IS_WIN else text.encode("utf-8")
    try:
        # wl-copy and xclip stay in the background to serve the selection: do not wait for them to exit
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True)
        p.communicate(data, timeout=5)
        return True
    except subprocess.TimeoutExpired:
        return True
    except OSError:
        return False


def paste():
    """The clipboard's text, or None when it cannot be read."""
    cmd = clipboard_commands()[1]
    if not cmd:
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def trash(path):
    """Moves a file or folder to the trash (the user can restore it), never deletes it for good. Raises OSError
    when no trash is reachable."""
    path = os.path.abspath(str(path))
    try:
        from send2trash import send2trash
        send2trash(path)
        return
    except ImportError:
        pass
    if IS_MAC:
        script = 'tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)'
        cmds = [["osascript", "-e", "on run argv", "-e", script, "-e", "end run", path]]
    else:
        cmds = [["gio", "trash", path], ["trash-put", path], ["kioclient5", "move", path, "trash:/"],
                ["kioclient", "move", path, "trash:/"]]
    for cmd in cmds:
        if shutil.which(cmd[0]):
            try:
                if subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0:
                    return
            except (OSError, subprocess.SubprocessError):
                continue
    raise OSError("could not move it to the trash (install gio, trash-cli or send2trash)")


def notify(title, text=""):
    """A desktop notification; False when the desktop has no way to show one."""
    title, text = str(title or "maxedcode")[:200], str(text or "")[:500]
    if IS_MAC:
        script = "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run"
        return _detached(["osascript", "-e", script, title, text])
    if shutil.which("notify-send"):
        return _detached(["notify-send", "--app-name=maxedcode", title, text])
    return False


def _dialog(cmd):
    """Runs a dialog program: its stdout lines, [] when cancelled, None when the program is missing."""
    if not shutil.which(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError:
        return None
    return [x for x in r.stdout.splitlines() if x.strip()] if r.returncode == 0 else []


def pick_folder(start="", title=""):
    """The desktop's own "choose a folder" dialog (macOS Finder, zenity, kdialog). -> a path or None."""
    start, title = str(start or os.path.expanduser("~")), str(title or "Choose a folder")
    if IS_MAC:
        script = ("on run argv\nset f to choose folder with prompt (item 2 of argv) default location "
                  "(POSIX file (item 1 of argv) as alias)\nreturn POSIX path of f\nend run")
        out = _dialog(["osascript", "-e", script, start, title])
    else:
        out = _dialog(["zenity", "--file-selection", "--directory", "--title", title,
                       "--filename", start.rstrip("/") + "/"])
        if out is None:
            out = _dialog(["kdialog", "--getexistingdirectory", start, "--title", title])
    return out[0].rstrip("/") or "/" if out else None


def pick_files(start=""):
    """The desktop's own "open files" dialog with several files allowed. -> [paths] ([] when cancelled)."""
    start = str(start or os.path.expanduser("~"))
    if IS_MAC:
        script = ("on run argv\nset fs to choose file default location (POSIX file (item 1 of argv) as alias) "
                  "with multiple selections allowed\nset out to \"\"\nrepeat with f in fs\n"
                  "set out to out & POSIX path of f & linefeed\nend repeat\nreturn out\nend run")
        return _dialog(["osascript", "-e", script, start]) or []
    out = _dialog(["zenity", "--file-selection", "--multiple", "--separator", "\n",
                   "--filename", start.rstrip("/") + "/"])
    if out is None:
        out = _dialog(["kdialog", "--getopenfilename", start, "--multiple", "--separate-output"])
    return out or []


def user_shell():
    """The user's login shell for a terminal ($SHELL, else bash, else sh)."""
    sh = os.environ.get("SHELL") or ""
    if sh and os.path.isfile(sh) and os.access(sh, os.X_OK):
        return sh
    return shutil.which("bash") or shutil.which("zsh") or "/bin/sh"
