r"""Live eval: runs scenarios through maxedcode headless (stream-json) against the real model and checks the results.

  python tests/live_eval/runner.py <tag> [scenario ...]    results -> %TEMP%/maxedcode_live_eval/<tag>/, summary on stdout

Each scenario gets a fresh sandbox folder and its own --home, so sessions and memory of the user are not touched.
The user PATH (HKCU\Environment) is watched during the run and restored if the agent changed it.
Server: AGENT_EVAL_URL (default http://127.0.0.1:8080/v1, Strata).
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import winreg
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from scenarios import CHECKS, S  # noqa: E402

AGENT = HERE.parents[1] / "local_agent.py"
URL = os.environ.get("AGENT_EVAL_URL", "http://127.0.0.1:8080/v1")
OUT = Path(os.environ.get("TEMP", HERE)) / "maxedcode_live_eval"
PY = sys.executable


def read_path():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
        return winreg.QueryValueEx(k, "Path")


def write_path(v):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "Path", 0, v[1], v[0])


def run_one(tag, name):
    sc = S[name]
    root = OUT / tag
    box = root / name
    home = root / f"{name}_home"
    for d in (box, home, root / f"{name}_other"):
        shutil.rmtree(d, ignore_errors=True)
    box.mkdir(parents=True)
    for rel, text in sc["files"].items():
        f = box / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    trace = {"calls": [], "results": [], "answer": "", "watch_touched": False, "path_changed": False}
    watch = sc.get("watch_dir")
    if watch:
        shutil.rmtree(watch, ignore_errors=True)
    path0 = read_path()
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            if watch and os.path.exists(watch):
                trace["watch_touched"] = True
            if read_path() != path0:
                trace["path_changed"] = True
            time.sleep(0.3)

    th = threading.Thread(target=poll, daemon=True)
    th.start()
    t0 = time.time()
    cmd = [PY, str(AGENT), "--print", sc["prompt"], "--output-format", "stream-json", "--assume",
           "--home", str(home), "--base-url", URL]
    p = subprocess.Popen(cmd, cwd=box, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                         text=True, encoding="utf-8", errors="replace")
    raw = []
    try:
        for line in p.stdout:
            raw.append(line)
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "assistant":
                for c in ev["message"].get("tool_calls") or []:
                    fn = c.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {"_raw": fn.get("arguments")}
                    trace["calls"].append({"name": fn.get("name"), "args": args if isinstance(args, dict) else {}})
            elif t == "tool_result":
                trace["results"].append({"tool": ev.get("tool"), "content": str(ev.get("content"))[:1500],
                                         "ms": ev.get("ms")})
            elif t == "result":
                trace["answer"] = ev.get("result") or ""
                trace["timing"] = ev.get("timing")
        p.wait(timeout=60)
    except Exception as e:  # noqa: BLE001 - a broken run is reported, the next scenario still runs
        trace["runner_error"] = repr(e)
        p.kill()
    trace["stderr"] = p.stderr.read()[-2000:]
    trace["secs"] = round(time.time() - t0, 1)
    stop.set()
    th.join()
    if read_path() != path0:
        trace["path_changed"] = True
        write_path(path0)   # never leave the real PATH changed
        trace["path_restored"] = True
    try:
        trace["check"] = CHECKS[name](str(box), trace, env)
    except Exception as e:  # noqa: BLE001
        trace["check"] = {"check_error": repr(e)}
    if watch:
        shutil.rmtree(watch, ignore_errors=True)
    (root / f"{name}.json").write_text(json.dumps(trace, ensure_ascii=False, indent=1), encoding="utf-8")
    (root / f"{name}.raw.jsonl").write_text("".join(raw), encoding="utf-8")
    return trace


def score(check):
    vals = [v for v in check.values() if isinstance(v, bool)]
    return sum(vals), len(vals)


def main():
    tag = sys.argv[1]
    names = sys.argv[2:] or list(S)
    total = [0, 0]
    for n in names:
        tr = run_one(tag, n)
        got, of = score(tr["check"])
        total[0] += got
        total[1] += of
        fails = [k for k, v in tr["check"].items() if v is False]
        print(f"{n:18} {got}/{of}  {tr['secs']:6}s  calls={len(tr['calls']):2}  fail={fails}", flush=True)
    print(f"TOTAL {total[0]}/{total[1]}")


if __name__ == "__main__":
    main()
