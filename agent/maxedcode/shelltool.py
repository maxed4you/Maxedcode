"""The model's command tool for this platform: `powershell` on Windows, `bash` on Linux/macOS.

One place decides the name, so every runtime check of "is this the shell tool" reads NAME. On Windows
NAME is "powershell" and nothing about the tool list, the schemas or the system prompt changes; on POSIX
the same tool (command, timeout, run_in_background) is advertised as `bash` and runs in /bin/bash.
Stdlib only: imported by config, permissions and the tools package, so it must not import any of them.
"""
import os
import shutil
import sys

POSIX = os.name != "nt"
MACOS = sys.platform == "darwin"
NAME = "bash" if POSIX else "powershell"
TITLE = "Bash" if POSIX else "PowerShell"
# Both spellings, for places that recognise a shell call from either platform (history, aliases).
NAMES = frozenset({"powershell", "bash"})


# Names written for the other platform's shell (rules, hook matchers, agent tool lists copied from Windows):
# on POSIX they mean bash. On Windows canon() returns the name unchanged.
_OTHER = frozenset({"powershell", "pwsh", "sh", "zsh"})


def canon(name):
    """A tool name from settings or an agent definition, as this platform's tool list spells it."""
    return NAME if POSIX and isinstance(name, str) and name.strip().lower() in _OTHER else name


def is_shell(name):
    """True when name is this platform's command tool."""
    return name == NAME


def bash_path():
    """The POSIX shell binary: /bin/bash (macOS ships bash 3.2 there), else bash on PATH, else /bin/sh."""
    for candidate in ("/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("bash") or "/bin/sh"


def posix_env(base=None):
    """Environment for POSIX shell commands: UTF-8 output when the user has no locale set (a bare
    container or a service account starts with LANG unset and Python/git then print ASCII escapes)."""
    env = dict(os.environ if base is None else base)
    if not (env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG")):
        env["LANG"] = "en_US.UTF-8" if MACOS else "C.UTF-8"
    return env
