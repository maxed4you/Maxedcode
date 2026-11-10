"""The system prompt for Linux/macOS: system_prompt.md with its Windows/PowerShell lines swapped for bash ones.

system_prompt.md stays the one source (and byte-for-byte the Windows prompt). On POSIX config.py applies these
replacements once at import, so the prefix is stable for the process like on Windows. Every `old` must occur
in system_prompt.md: test_posix_shell.py checks that, so an edit of the prompt cannot silently leave a
PowerShell rule in the bash prompt.
"""

REPLACEMENTS = (
    ("on the user's Windows computer.", "on the user's Linux/macOS computer."),
    ("At most one more local check (Get-Command, the package manager)",
     "At most one more local check (command -v, the package manager)"),
    ("the whole profile or the Recycle Bin", "the whole home folder or the Trash"),
    ("put it in `$env:TEMP`, never in the user's project", "put it in `/tmp`, never in the user's project"),
    ("Desktop, Downloads, drive roots, or Program Files", "Desktop, Downloads, the filesystem root, /usr or /opt"),
    ("read_file decodes UTF-8 and UTF-16 (PowerShell-written logs) itself", "read_file decodes UTF-8 and UTF-16 itself"),
    ("\npowershell, read_file, edit_file,", "\nbash, read_file, edit_file,"),
    ("- powershell is Windows PowerShell 5.1 (`;`, not `&&`); quote paths with spaces.",
     "- bash runs in one persistent /bin/bash session: cd and variables carry over to the next call. On macOS it is "
     "bash 3.2, so no bash 4+ features (associative arrays, `${x,,}`, `mapfile`). Quote paths with spaces."),
    ("wait for it with bg_output(id, wait=600), never with Start-Sleep loops", "wait for it with bg_output(id, wait=600), never with sleep loops"),
    ("Multi-line scripts are fine. Use only cmdlets and parameters you know exist (no bash: grep/head/tail/ls -la/rm -rf/export); "
     "`$env:NAME`, not `$env.NAME`; `\"${name}:\"` inside strings. The tool pre-checks every command: on \"command NOT run — "
     "pre-check failed\" nothing ran, so fix exactly the named cmdlet/parameter/syntax and run once more.",
     "Multi-line scripts and heredocs are fine. stdin is empty (/dev/null): a program that reads input gets EOF, so pipe "
     "the input in (`printf '1 2\\n' | python3 sol.py`). Non-interactive only: no sudo, editors, pagers or password "
     "prompts (use flags such as `-y`, `--no-pager`, `git --no-pager log`). Use `python3` when `python` is missing. The "
     "tool pre-checks the syntax of every command: on \"command NOT run — pre-check failed\" nothing ran, so fix exactly "
     "the reported syntax error and run once more."),
    ("- A `[error origin] file:line: code` line in a powershell result is where the script actually failed (\"At line:1 "
     "char:N\" is only the caller). Read that line and its context first. A dot-sourced file runs in the caller's scope: "
     "its variables overwrite the caller's typed parameters.",
     "- A failed command ends with `Error: bash command exited with code N`; the error lines above it (`file: line N: "
     "...`) say where it failed. Read that line and its context first. A `source`d file runs in the caller's shell: its "
     "variables and `cd` stay in the session."),
    ("never by rewriting the whole file with write_file or Set-Content for a partial change.",
     "never by rewriting the whole file with write_file or a shell redirect (`>`, `cat <<EOF`, `sed -i`) for a partial change."),
    ("is written once to a file in %TEMP% and re-run by name", "is written once to a file in /tmp and re-run by name"),
    ("(registry, user PATH or variables, shortcuts, services, scheduled tasks)",
     "(shell profile files such as ~/.bashrc or ~/.zshrc, user PATH or variables, desktop entries, services, cron jobs, launch agents)"),
    ("(git reset/rebase/commit amend, refs, migrations, registry)", "(git reset/rebase/commit amend, refs, migrations, system configuration)"),
    ("reading/searching and read-only powershell/py_run are allowed.", "reading/searching and read-only bash/py_run are allowed."),
)


def posix_prompt(text):
    """system_prompt.md text for Linux/macOS (replacements that do not match are skipped)."""
    text = text.replace("\r\n", "\n")
    for old, new in REPLACEMENTS:
        text = text.replace(old, new)
    return text


def missing(text):
    """Replacements whose `old` text is not in the prompt (for the tests)."""
    text = text.replace("\r\n", "\n")
    return [old for old, _new in REPLACEMENTS if old not in text]
