#!/bin/sh
# Step 1 of the setup on Linux / macOS, the twin of bootstrap.ps1: a Python 3.10+ that can make a virtual
# environment (venv + ensurepip: the program gets its own, program/.venv). Its path goes to the file $MX_PYFILE
# (or to stdout when MX_PYFILE is not set); what it does is said on stderr.
#
# 1. python3, python3.14 ... python3.10, Homebrew's and the system's python3 - the first one whose venv works
#    (Debian / Ubuntu ship python3 without python3-venv: that one does not count)
# 2. none: uv (https://docs.astral.sh/uv, one file, no sudo) into ~/.maxedcode/uv, then its Python 3.12 there
# 3. that fails too (no internet, no curl / wget): the commands to install Python by hand, exit code 1
#
# Used by bin/maxedcode.js (npm) and install.sh; nothing is installed with sudo and no shell start file is changed.

say() { printf '  %s\n' "$*" >&2; }

UV_DIR="${MX_UV_DIR:-$HOME/.maxedcode/uv}"
UV_PY="3.12"

# usable <python>: 3.10+ and `python -m venv` makes a venv with pip in it
usable() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1 || return 1
    t=$(mktemp -d "${TMPDIR:-/tmp}/maxedcode-venv-XXXXXXXX") || return 1
    ok=1
    if "$1" -m venv "$t/v" >/dev/null 2>&1 && "$t/v/bin/python" -m pip --version >/dev/null 2>&1; then
        ok=0
    fi
    rm -rf "$t"
    return $ok
}

# the full path of a python command (python3 -> /usr/bin/python3.12 ...)
real() {
    "$1" -c 'import sys; print(sys.executable)' 2>/dev/null
}

found() {
    if [ -n "${MX_PYFILE:-}" ]; then
        printf '%s' "$1" > "$MX_PYFILE"
    else
        printf '%s\n' "$1"
    fi
    exit 0
}

# fetch <url> <file>
fetch() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$2" "$1"
    else
        return 1
    fi
}

# the uv of an earlier setup, one on PATH, else the official standalone installer into UV_DIR
find_uv() {
    for u in "$UV_DIR/uv" "$UV_DIR/bin/uv"; do
        [ -x "$u" ] && { printf '%s\n' "$u"; return 0; }
    done
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    say "no Python 3.10+ with venv found: getting uv (a Python manager, into $UV_DIR, no sudo) ..."
    mkdir -p "$UV_DIR" || return 1
    script="$UV_DIR/uv-installer.sh"
    fetch "https://astral.sh/uv/install.sh" "$script" || return 1
    UV_INSTALL_DIR="$UV_DIR" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh "$script" >&2 || return 1
    rm -f "$script"
    for u in "$UV_DIR/uv" "$UV_DIR/bin/uv"; do
        [ -x "$u" ] && { printf '%s\n' "$u"; return 0; }
    done
    return 1
}

PYTHONS="python3 python3.14 python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3"

main() {
    # MX_PYTHONS: another list to try (the tests use it)
    for exe in ${MX_PYTHONS:-$PYTHONS}; do
        command -v "$exe" >/dev/null 2>&1 || continue
        py=$(real "$exe") || continue
        [ -n "$py" ] || continue
        if usable "$py"; then
            found "$py"
        fi
    done

    if uv=$(find_uv); then
        # its own Pythons, kept next to it: never a system one without venv
        export UV_PYTHON_INSTALL_DIR="$UV_DIR/python" UV_PYTHON_PREFERENCE=only-managed
        py=$("$uv" python find "$UV_PY" 2>/dev/null) || py=""
        if [ -z "$py" ]; then
            say "installing Python $UV_PY with uv (into $UV_DIR/python) ..."
            "$uv" python install "$UV_PY" >&2 && py=$("$uv" python find "$UV_PY" 2>/dev/null) || py=""
        fi
        if [ -n "$py" ] && usable "$py"; then
            found "$py"
        fi
    fi

    say "Python 3.10 or newer (with venv) was not found and could not be downloaded. Install it, then run setup again:"
    say "  macOS:          brew install python   (or https://www.python.org/downloads/macos/)"
    say "  Debian/Ubuntu:  sudo apt install python3 python3-venv"
    say "  Fedora:         sudo dnf install python3"
    say "  Arch:           sudo pacman -S python"
    exit 1
}

main "$@"
