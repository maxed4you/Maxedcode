"""py_source — the real source of an object from libraries."""
import subprocess
import sys
from ..util import clip

PY_SRC = r'''
import importlib, inspect, sys
sym, maxl = sys.argv[1], int(sys.argv[2])
parts = sym.split(".")
obj = None
for i in range(len(parts), 0, -1):
    try:
        obj = importlib.import_module(".".join(parts[:i])); rest = parts[i:]; break
    except ImportError:
        continue
if obj is None:
    print("Error: module not found: " + sym); sys.exit(1)
try:
    for p in rest: obj = getattr(obj, p)
except AttributeError as e:
    names = [n for n in dir(obj) if not n.startswith("__")]
    print(f"Error: {e}. Available: {', '.join(names)[:1500]}"); sys.exit(1)
try:
    lines, start = inspect.getsourcelines(obj)
    f = inspect.getsourcefile(obj)
    body = "".join(lines).splitlines()
    more = f"\n...[{len(body)-maxl} more lines: read_file {f} start_line={start+maxl}]" if len(body) > maxl else ""
    print(f"# {f}:{start}\n" + "\n".join(body[:maxl]) + more)
except (TypeError, OSError):
    print(f"{sym}: {type(obj).__name__}\n" + (inspect.getdoc(obj) or "")[:1500])
'''
def tool_pysrc(symbol, max_lines=80):
    r = subprocess.run([sys.executable, "-c", PY_SRC, symbol, str(int(max_lines or 80))],
                       capture_output=True, timeout=30, text=True, encoding="utf-8")
    return clip((r.stdout + r.stderr).strip())
