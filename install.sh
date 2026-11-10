#!/bin/sh
# maxedcode one-line install for Linux and macOS:
#
#   wget -qO- https://raw.githubusercontent.com/maxed4you/Maxedcode/main/install.sh | sh      (Linux: Ubuntu / Debian ship wget, not curl)
#   curl -fsSL https://raw.githubusercontent.com/maxed4you/Maxedcode/main/install.sh | sh     (macOS)
#
# It finds Python 3.10+ that can make a venv (installer/bootstrap.sh: when there is none, uv and its Python 3.12
# go to ~/.maxedcode/uv, no sudo), downloads maxedcode from GitHub and runs its installer with no questions: the program in
# ~/.local/share/maxedcode with its own Python environment (program/.venv), the CLI (maxed, linked in ~/.local/bin),
# the app (maxed app), the app menu entries (Linux) or ~/Applications/maxedcode.app (macOS), the model server found
# on this computer. Your chats and settings (~/.maxedcode) are kept; run it again to update.
# Nothing is installed with sudo. In a terminal the setup wizard asks the folder and the server first.
#
# Options (environment variables, set them before sh):
#   MAXEDCODE_ARGS="--base-url 192.168.0.10:1234 --model qwen3"   extra installer options (install.py --help)
#   MAXEDCODE_YES=1        no questions: the defaults (also when there is no terminal)
#   MAXEDCODE_ASK=1        the wizard even without a terminal (the setup window)
#   MAXEDCODE_REF=main     branch or tag to install
#   GITHUB_TOKEN=...       only to install from a private fork
#   curl -fsSL .../install.sh | MAXEDCODE_ARGS="--base-url 192.168.0.10:1234" sh

# everything is in functions, called on the last line: a download cut short runs nothing
say() { printf '  %s\n' "$*"; }
die() { printf '  [ERROR] %s\n' "$*" >&2; exit 1; }

# fetch <url> <file> [token [accept]]
fetch() {
    if command -v curl >/dev/null 2>&1; then
        if [ -n "${3:-}" ]; then
            curl -fsSL -H "Authorization: Bearer $3" -H "User-Agent: maxedcode-setup" \
                -H "Accept: ${4:-application/vnd.github+json}" -o "$2" "$1"
        else
            curl -fsSL -o "$2" "$1"
        fi
    elif command -v wget >/dev/null 2>&1; then
        if [ -n "${3:-}" ]; then
            wget -q --header="Authorization: Bearer $3" --header="User-Agent: maxedcode-setup" \
                --header="Accept: ${4:-application/vnd.github+json}" -O "$2" "$1"
        else
            wget -q -O "$2" "$1"
        fi
    else
        die "curl or wget is needed to download maxedcode"
    fi
}

# unzip with Python (no unzip needed); entries named with backslashes (a zip made on Windows) become folders,
# and no entry may leave the folder
UNZIP='import os, sys, zipfile
root = os.path.realpath(sys.argv[2])
with zipfile.ZipFile(sys.argv[1]) as z:
    for m in z.infolist():
        name = m.filename.replace("\\", "/")
        out = os.path.realpath(os.path.join(root, name))
        if out != root and not out.startswith(root + os.sep):
            sys.exit("bad path in the download: " + name)
        if name.endswith("/"):
            os.makedirs(out, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with z.open(m) as src, open(out, "wb") as dst:
            dst.write(src.read())
        if name.endswith(".sh"):
            os.chmod(out, 0o755)'

main() {
    set -eu
    repo="maxed4you/Maxedcode"
    ref="${MAXEDCODE_REF:-main}"

    printf '\n  maxedcode setup\n  ---------------\n'

    # The program. Like install.ps1: the built package of the latest release, the program alone (maxedcode.zip:
    # install.py, maxedcode/, desktop/ - no tests, no docs). No release yet / a branch asked for / private
    # repository: the source of $ref (only the program is installed from it, the rest stays in the temp folder).
    work=$(mktemp -d "${TMPDIR:-/tmp}/maxedcode-setup-XXXXXXXX")
    trap 'rm -rf "$work"' EXIT
    trap 'exit 130' INT TERM
    zip="$work/maxedcode.zip"

    # Python 3.10+ with a working venv: installer/bootstrap.sh (what npm runs too), from the same branch
    boot="$work/bootstrap.sh"
    if [ -n "${MAXEDCODE_SRC:-}" ]; then
        cp "$MAXEDCODE_SRC/installer/bootstrap.sh" "$boot"
    elif [ -n "${GITHUB_TOKEN:-}" ]; then
        fetch "https://api.github.com/repos/$repo/contents/installer/bootstrap.sh?ref=$ref" "$boot" "$GITHUB_TOKEN" \
            "application/vnd.github.raw"
    else
        fetch "https://raw.githubusercontent.com/$repo/$ref/installer/bootstrap.sh" "$boot"
    fi || die "the Python finder (installer/bootstrap.sh) could not be downloaded"
    MX_PYFILE="$work/python.txt" sh "$boot" || exit 1
    py=$(cat "$work/python.txt")
    [ -n "$py" ] || die "no Python 3.10+ found"
    say "Python: $py"

    if [ -n "${MAXEDCODE_SRC:-}" ]; then          # a local copy of the repository instead of the download (testing)
        mkdir -p "$work/src"
        cp -R "$MAXEDCODE_SRC/agent" "$MAXEDCODE_SRC/desktop" "$work/src/"
    elif [ -n "${GITHUB_TOKEN:-}" ]; then
        say "downloading maxedcode ($ref) ..."
        fetch "https://api.github.com/repos/$repo/zipball/$ref" "$zip" "$GITHUB_TOKEN"
    else
        got=""
        if [ -z "${MAXEDCODE_REF:-}" ]; then
            say "downloading maxedcode (latest release) ..."
            if fetch "https://github.com/$repo/releases/latest/download/maxedcode.zip" "$zip"; then
                got=1
            else
                say "no release package yet: taking the source of $ref"
            fi
        fi
        if [ -z "$got" ]; then
            say "downloading maxedcode ($ref) ..."
            fetch "https://github.com/$repo/archive/$ref.zip" "$zip"
        fi
    fi
    if [ -f "$zip" ]; then
        "$py" -I -c "$UNZIP" "$zip" "$work/pkg" || die "the download could not be unpacked"
    fi
    # the release package has install.py at its top; the source has it in <repo>-<ref>/agent
    installer=$(find "$work" -maxdepth 4 -name install.py 2>/dev/null | while IFS= read -r f; do
        if [ -f "$(dirname "$f")/maxedcode/config.py" ]; then
            printf '%s\n' "$f"
            break
        fi
    done)
    [ -n "$installer" ] || die "the download has no install.py"

    # Run in a terminal: the setup wizard (the setup window when Tk can open one, else the console wizard with
    # arrow keys) asks the folder and the server. No terminal (a script, CI) or MAXEDCODE_YES=1: the defaults.
    ask=""
    if [ "${MAXEDCODE_YES:-}" != "1" ] && { [ "${MAXEDCODE_ASK:-}" = "1" ] || (: < /dev/tty) 2>/dev/null; }; then
        ask=1
    fi
    set --
    [ -n "$ask" ] || set -- --yes
    set -f                                         # MAXEDCODE_ARGS: split on spaces, no * expansion
    # shellcheck disable=SC2086
    set -- "$@" ${MAXEDCODE_ARGS:-}
    set +f
    rc=0
    if [ -n "$ask" ] && (: < /dev/tty) 2>/dev/null; then
        "$py" "$installer" "$@" < /dev/tty || rc=$?      # curl | sh: the questions are read from the terminal
    else
        "$py" "$installer" "$@" < /dev/null || rc=$?
    fi
    [ "$rc" -eq 0 ] || die "the installer stopped with code $rc"
}

main "$@"
