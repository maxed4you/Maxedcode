"""Run Maxedcode's checks and optionally launch an interactive auto-mode smoke test.

Run with `python run_all_tests.py` or double-click `run_all_tests.bat` on Windows.
Suites run side by side (`-j N` workers, default up to 8), the longest first by the last run's times; a
suite's output is printed only when it fails. `--serial` runs them one by one with live output, as before
(test subprocesses then inherit stdin/stdout). Words after the flags pick suites by file name:
`python run_all_tests.py browser dispatch`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SUITES = [
    ("Tool-call, API-history, and long-paste regression tests", "test_cli_hardening.py"),
    ("Tool output normalization and bounded previews", "test_tool_output_normalization.py"),
    ("Compact tool catalog and stable inner signatures", "test_tool_catalog.py"),
    ("Schema-guided tool-input repair", "test_tool_input_repair.py"),
    ("Restricted Code Mode interpreter", "test_code_mode.py"),
    ("Dynamic task-plan logic tests", "test_dynamic_tasks.py"),
    ("Textual UI, context compaction, paste, and ask_user tests", "test_fixes.py"),
    ("Wasted-call guard, dangerous commands, headless ask_user", "test_loopguard.py"),
    ("Prompt rules, download-then-run guard, headless timings", "test_prompt_timing.py"),
    ("Tool runner exit-code/error propagation", "test_runner_results.py"),
    ("Persistent PowerShell timeout/retry and headless regression tests", "test_shell_runner.py"),
    ("Long commands: per-call timeout, background wait, no orphan children", "test_background_wait.py"),
    ("PowerShell pre-check: multi-line, unknown cmdlets/parameters, auto-fixes", "test_ps_precheck.py"),
    ("Local-model quality: repo map, edit fallback, lint/cache, hooks, model profiles", "test_local_quality.py"),
    ("Runtime speed fixes: bounded reasoning, language-free tool routing", "test_runtime_fixes.py"),
    ("Prompt-cache stability: prefix order, byte-identical arguments, compaction anchor", "test_cache_stability.py"),
    ("Subagent sessions, shared prefix/cache key, report cap, history budget, and parent-controlled sequential batches", "test_dispatch.py"),
    ("Ultra effort: levels, staged run, bounded review/fix rounds", "test_ultra.py"),
    ("Tool argument semantics and unknown-tool hints", "test_arg_semantics.py"),
    ("Undo and checkpoint boundaries", "test_checkpoints.py"),
    ("Normalized lifecycle and tool events", "test_events.py"),
    ("Transcript compaction and summaries", "test_compact.py"),
    ("Long autonomous turns: unbounded compaction, checklist after a summary, child wrap-up", "test_autonomy.py"),
    ("Repository map token budgeting and cache", "test_repo_map.py"),
    ("Reliable edits and dry-run diffs", "test_edit_reliability.py"),
    ("Short tool results: relative paths, no echo, whole-file reads", "test_tool_result_size.py"),
    ("Dangerous command and protected path checks", "test_dangerous_commands.py"),
    ("Execution policy matching and precedence", "test_exec_policy.py"),
    ("Event-derived timing metrics", "test_metrics_events.py"),
    ("Tool registry contracts", "test_tool_registry.py"),
    ("Cache-safe late context (rules, folder docs, background results)", "test_late_context.py"),
    ("Cloud API data-minimization and approval gates", "test_cloud_privacy.py"),
    ("Empty-session persistence and safe session switching", "test_ui_session_switch.py"),
    ("Installer: arrow-key menus, Strata defaults, setup without a running server", "test_installer.py"),
    ("Auto-update: daily look at GitHub, download, install at the next start, no downgrade", "test_update.py"),
    ("Model servers: cloud APIs, addresses in any form, per-server keys", "test_cloud_servers.py"),
    ("Every file change shown as a diff; turn time kept for /history", "test_filewatch.py"),
    ("Clickable chat addresses and the confirmation screen", "test_chat_links.py"),
    ("DeepSeek Harness findings: line endings, encodings, saved output, retries, instructions", "test_dsh_findings.py"),
    ("Cline findings: resumed prefix, cut replies, patch errors, compaction files, MCP names", "test_cline_findings.py"),
    ("Desktop window: webapp, --session, live deltas, tool result ids, --btw, Plan/Auto, settings", "test_desktop_engine.py"),
    ("Own browser: extension id, native host, browser tool and its helper", "test_browser.py"),
]
TIMES = Path(tempfile.gettempdir()) / "maxedcode-test-times.json"   # the last run's seconds per suite


def suite_command(pattern: str) -> list[str]:
    return [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", pattern, "-v"]


def run(label: str, command: list[str], env: dict[str, str] | None = None) -> bool:
    print(f"\n{'=' * 76}\n[RUN] {label}\n$ {' '.join(command)}\n{'=' * 76}", flush=True)
    # Tests never ask a model server or start one: the same result (and speed) whether Strata runs or not.
    env = {**(env or os.environ), "MAXEDCODE_OFFLINE": "1"}
    result = subprocess.run(command, cwd=ROOT, env=env, stdin=None)
    state = "PASS" if result.returncode == 0 else f"FAIL (exit {result.returncode})"
    print(f"[{state}] {label}", flush=True)
    return result.returncode == 0


def run_quiet(command: list[str]) -> tuple[bool, float, str]:
    """One suite with its output captured: (passed, seconds, output)."""
    started = time.monotonic()
    result = subprocess.run(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    return result.returncode == 0, time.monotonic() - started, result.stdout


def run_parallel(suites: list[tuple[str, str]], workers: int) -> list[tuple[str, bool]]:
    try:
        last = json.loads(TIMES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = {}
    # The longest suites start first, so the run does not end waiting on one slow suite.
    order = sorted(suites, key=lambda s: -float(last.get(s[1], 60)))
    done: dict[str, tuple[bool, float]] = {}
    started = time.monotonic()
    print(f"\nRunning {len(order)} suites, {workers} at a time...", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(run_quiet, suite_command(pattern)): (label, pattern) for label, pattern in order}
        for job in as_completed(jobs):
            label, pattern = jobs[job]
            ok, seconds, output = job.result()
            done[pattern] = (ok, seconds)
            print(f"[{'PASS' if ok else 'FAIL'}] {seconds:6.1f} s  {label}", flush=True)
            if not ok:
                print(f"{'-' * 76}\n{output.rstrip()}\n{'-' * 76}", flush=True)
    last.update({pattern: round(seconds, 1) for pattern, (_ok, seconds) in done.items()})
    try:
        TIMES.write_text(json.dumps(last, indent=1), encoding="utf-8")
    except OSError:
        pass
    print(f"\nAll suites took {time.monotonic() - started:.0f} s.", flush=True)
    return [(label, done[pattern][0]) for label, pattern in suites]


def options(argv: list[str]) -> tuple[bool, int, list[str]]:
    serial, workers, names = False, min(8, os.cpu_count() or 4), []
    args = iter(argv)
    for arg in args:
        if arg == "--serial":
            serial = True
        elif arg in ("-j", "--jobs"):
            workers = max(1, int(next(args, workers)))
        else:
            names.append(arg.lower())
    return serial, workers, names


def main() -> int:
    serial, workers, names = options(sys.argv[1:])
    results: list[tuple[str, bool]] = []
    # Compile source in memory so the test runner does not create pycache files in the project.
    compile_cmd = [sys.executable, "-c", (
        "from pathlib import Path; import sys; "
        "files=[p for p in Path('.').rglob('*.py') if '__pycache__' not in p.parts and '.venv' not in p.parts and 'venv' not in p.parts]; "
        "[(compile(p.read_text(encoding='utf-8'), str(p), 'exec')) for p in files]; "
        "print(f'Compiled {len(files)} Python files successfully')"
    )]
    results.append(("Python syntax compilation", run("Python syntax compilation", compile_cmd)))

    skipped: list[str] = []
    present: list[tuple[str, str]] = []
    for label, pattern in SUITES:
        if names and not any(n in pattern.lower() for n in names):
            continue
        if not (ROOT / "tests" / pattern).is_file():
            print(f"\n[SKIP] {label}: tests/{pattern} is not included in this source archive.", flush=True)
            skipped.append(label)
            continue
        present.append((label, pattern))
    if serial or workers == 1:
        for label, pattern in present:
            results.append((label, run(label, suite_command(pattern))))
    else:
        results += run_parallel(present, workers)

    print("\nTEST SUMMARY", flush=True)
    for label, ok in results:
        print(f"{'PASS' if ok else 'FAIL':4}  {label}", flush=True)
    all_ok = all(ok for _, ok in results)
    if not all_ok:
        print("\nSome available automated suites failed. Fix those before relying on the CLI.", flush=True)
    elif skipped:
        print(f"\nAll {len(results) - 1} available test suites passed, but {len(skipped)} suites were skipped because their files are absent.", flush=True)
        print("Skipped: " + "; ".join(skipped), flush=True)
    else:
        print("\nAll automated suites passed.", flush=True)

    if all_ok and sys.stdin.isatty() and not names:
        try:
            answer = input("\nLaunch Maxedcode for a live interactive smoke test in AUTO mode? (y/N): ").strip().lower()
        except EOFError:
            answer = "n"
        if answer in {"y", "yes"}:
            env = os.environ.copy()
            env["MAXEDCODE_START_MODE"] = "auto"
            print("\nAuto mode is enabled for this smoke-test process only. `ask_user` questions still appear.", flush=True)
            subprocess.run([sys.executable, "local_agent.py"], cwd=ROOT, env=env, stdin=None)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
