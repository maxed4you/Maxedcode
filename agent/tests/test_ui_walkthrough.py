"""What a walk through the app window by hand found (2026-11-11), kept from coming back.

* A refused step (plan mode, a result that starts with "[Arguments normalized: ...]") was shown as a success:
  "Changed files: hello.txt +2" and the "+N -M" button over a write that never happened.
* The words pointed at "Settings -> maxedcode", a section the window no longer has (Connection, Browser).
* A server that runs, but whose program is not found here, was offered for download."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from maxedcode import config

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "maxedcode" / "webapp" / "static" / "app.js"
REFUSED = "[Arguments normalized: path: 'hello.txt' -> 'C:\\\\x...']\nError: plan mode is read-only; describe it"
CASES = [(REFUSED, True), ("Wrote 2 lines to hello.txt", False), ("[note]\n[x]\nError: y", True),
         ("[Arguments normalized: a]\nWrote 3 lines", False), ("[1, 2, 3]", False), ("Error: no", True),
         ("Tool error: x", True), ("exit code 1", True), ("", False)]


class RefusedSteps(unittest.TestCase):
    def test_the_agent_tells_a_refusal_after_a_note(self):
        for text, failed in CASES:
            self.assertEqual(config.step_failed(text), failed, text)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_window_tells_it_the_same_way(self):
        js = APP_JS.read_text(encoding="utf-8")
        lines = [x for x in js.splitlines() if x.strip().startswith(("var STEP_ERROR =", "function stepFailed("))]
        self.assertEqual(len(lines), 2, "STEP_ERROR and stepFailed() are one line each")
        script = "\n".join(lines) + f"\nconsole.log(JSON.stringify({json.dumps([c[0] for c in CASES])}.map(stepFailed)))"
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [c[1] for c in CASES])

    def test_every_check_of_a_step_result_goes_through_step_failed(self):
        js = APP_JS.read_text(encoding="utf-8")
        uses = [x.strip() for x in js.splitlines() if "STEP_ERROR" in x and not x.strip().startswith("//")]
        self.assertTrue(all(u.startswith(("var STEP_ERROR =", "function stepFailed(")) for u in uses), uses)


class Words(unittest.TestCase):
    """Every place that tells the user where a setting is names a section the window has."""
    OLD = re.compile(r"(Settings|Настройки)\s*(→|->|-|–)\s*maxedcode\b")
    FILES = ["maxedcode/webapp/static/app.js", "maxedcode/chrome/extension/_locales/en/messages.json",
             "maxedcode/chrome/extension/_locales/ru/messages.json", "maxedcode/chrome/guide/index.html",
             "maxedcode/tools/browser.py", "install.py", "../README.md"]

    def test_no_word_points_at_the_old_section(self):
        for rel in self.FILES:
            path = ROOT / rel
            if not path.exists():
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if self.OLD.search(line) and not line.strip().startswith(("//", "#", "/*")):
                    self.fail(f"{rel}:{n}: {line.strip()[:120]}")

    def test_the_sections_named_exist(self):
        js = APP_JS.read_text(encoding="utf-8")
        for name in ("connTab: ['Подключение', 'Connection']", "brHead: ['Браузер', 'Browser']"):
            self.assertIn(name, js)


class RunningWithoutItsProgram(unittest.TestCase):
    def test_no_download_offer_over_a_server_that_runs(self):
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn("s.running ? t('runNoProgram')", js)
        self.assertIn("if (!s.running) acts.appendChild(smallBtn(t('runDownload')", js)
        self.assertIn("runNoProgram: [", js)


if __name__ == "__main__":
    unittest.main()
