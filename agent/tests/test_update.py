"""Auto-update (maxedcode/update.py): versions, when it may run, the download, the install at the next start."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from maxedcode import update  # noqa: E402
from maxedcode.version import VERSION  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

# what the release zip's install.py does for --update / --prepare, in a few lines
FAKE_INSTALL = r'''
import sys, pathlib
here = pathlib.Path(__file__).parent
if "--prepare" in sys.argv:
    sys.exit(0)
if "--update" in sys.argv:
    dest = pathlib.Path(sys.argv[sys.argv.index("--dir") + 1]) / "program" / "maxedcode"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "version.py").write_text((here / "maxedcode" / "version.py").read_text())
    sys.exit(int((here / "fail.txt").exists()))
'''


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.data = self.root / "data"
        self.program = self.root / "Inst" / "program"
        (self.program / "maxedcode").mkdir(parents=True)
        (self.program / "install.py").write_text("")
        (self.program / "maxedcode" / "version.py").write_text(f'VERSION = "{VERSION}"')
        d = self.data / "update"
        for name, value in (("PROGRAM", self.program), ("DATA", self.data), ("DIR", d), ("STATE", d / "state.json"),
                            ("RUNNING", d / "running")):
            p = patch.object(update, name, value)
            p.start()
            self.addCleanup(p.stop)
        for var in ("MAXEDCODE_NO_UPDATE", "MAXEDCODE_OFFLINE", "MAXEDCODE_UPDATED"):
            p = patch.dict(os.environ)
            p.start()
            self.addCleanup(p.stop)
            os.environ.pop(var, None)

    def release(self, version, fail=False, nested=False):
        """A release zip with that version -> its file:// URL (ASSET is patched to it)."""
        src = self.root / f"rel-{version}"
        top = src / "maxedcode-x" if nested else src
        (top / "maxedcode").mkdir(parents=True)
        (top / "install.py").write_text(FAKE_INSTALL)
        (top / "maxedcode" / "version.py").write_text(f'VERSION = "{version}"')
        if fail:
            (top / "fail.txt").write_text("1")
        z = self.root / f"maxedcode-{version}.zip"
        with zipfile.ZipFile(z, "w") as f:
            for p in src.rglob("*"):
                f.write(p, p.relative_to(src).as_posix())
        return z.as_uri()

    def test_versions(self):
        self.assertEqual(update.parse("v1.2.10"), (1, 2, 10))
        self.assertTrue(update.newer("1.0.10", "1.0.9"))
        self.assertFalse(update.newer("1.0.0", "1.0.0"))
        self.assertFalse(update.newer("", "1.0.0"))
        self.assertFalse(update.newer("latest", "0"))

    def test_version_py_and_package_json_agree(self):
        pkg = REPO / "package.json"
        if pkg.exists():
            self.assertEqual(json.loads(pkg.read_text(encoding="utf-8"))["version"], VERSION)

    def test_only_an_install_updates_and_it_can_be_turned_off(self):
        self.assertEqual(update.install_root(self.program), self.root / "Inst")
        self.assertIsNone(update.install_root(REPO / "agent"))          # the git working copy
        self.assertTrue(update.enabled())
        (self.data).mkdir(parents=True, exist_ok=True)
        (self.data / "settings.json").write_text('{"auto_update": false}')
        self.assertFalse(update.enabled())
        self.assertTrue(update.enabled(manual=True))                      # maxed --update still works
        os.environ["MAXEDCODE_OFFLINE"] = "1"
        self.assertFalse(update.enabled(manual=True))

    def test_a_zip_cannot_write_outside_its_folder(self):
        z = self.root / "evil.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("../outside.txt", "x")
        with self.assertRaises(ValueError):
            update.unpack(z, self.root / "x")
        self.assertFalse((self.root / "outside.txt").exists())

    def test_check_downloads_a_newer_release_once_a_day(self):
        url = self.release("99.0.0", nested=True)
        with patch.object(update, "latest", return_value="99.0.0") as asked, \
                patch.object(update, "ASSET", url.replace("99.0.0", "{version}")):
            st = update.check()
            self.assertEqual(st["ready"], "99.0.0")
            self.assertTrue((Path(st["dir"]) / "install.py").exists())
            update.check()
            self.assertEqual(asked.call_count, 1)                        # not again within CHECK_EVERY
        self.assertEqual(update.status()["ready"], "99.0.0")

    def test_same_or_older_release_downloads_nothing(self):
        with patch.object(update, "latest", return_value=VERSION), patch.object(update, "stage") as stage:
            st = update.check(force=True)
        stage.assert_not_called()
        self.assertEqual(st["latest"], VERSION)
        self.assertNotIn("ready", st)

    def test_offline_github_is_remembered_not_retried_at_every_start(self):
        with patch.object(update, "latest", side_effect=OSError("no network")):
            st = update.check()
        self.assertIn("no answer from GitHub", st["error"])
        with patch.object(update, "latest") as again:
            update.check()
        again.assert_not_called()

    def test_a_release_with_another_version_inside_is_refused(self):
        url = self.release("98.0.0")
        with patch.object(update, "ASSET", url):          # asked for 97.0.0, the zip says 98.0.0
            with self.assertRaises(ValueError):
                update.stage("97.0.0", prepare=False)

    def stage(self, version, **kw):
        url = self.release(version, **kw)
        with patch.object(update, "ASSET", url.replace(version, "{version}")):
            self.assertTrue(update.stage(version, prepare=True))

    def test_apply_installs_then_tells_once(self):
        self.stage("99.0.0")
        self.assertEqual(update.apply(say=lambda t: None), "99.0.0")
        self.assertEqual(update.version_of(self.program), "99.0.0")
        st = update.state()
        self.assertNotIn("ready", st)
        self.assertEqual(update.take_notice(), "99.0.0")
        self.assertEqual(update.take_notice(), "")

    def test_no_install_while_another_maxedcode_of_this_install_runs(self):
        self.stage("99.0.0")
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(other.kill)
        update.RUNNING.mkdir(parents=True, exist_ok=True)
        (update.RUNNING / str(other.pid)).write_text(str(self.program))
        (update.RUNNING / "999999").write_text(str(self.program))       # a finished one: its file goes
        self.assertIsNone(update.apply(say=lambda t: None))
        self.assertEqual(update.version_of(self.program), VERSION)
        self.assertFalse((update.RUNNING / "999999").exists())
        other.kill()
        other.wait()
        self.assertEqual(update.apply(say=lambda t: None), "99.0.0")

    def test_a_failed_install_is_not_tried_at_every_start(self):
        self.stage("99.0.0", fail=True)
        self.assertIsNone(update.apply(say=lambda t: None))
        self.assertEqual(update.state()["failed"], "99.0.0")
        with patch.object(update, "latest", return_value="99.0.0"), patch.object(update, "stage") as stage:
            update.check(force=True)
        stage.assert_not_called()

    def test_already_updated_by_hand_clears_the_download(self):
        self.stage("99.0.0")
        (self.program / "maxedcode" / "version.py").write_text('VERSION = "99.0.0"')
        with patch.object(update, "VERSION", "99.0.0"):
            self.assertIsNone(update.apply(say=lambda t: None))
        self.assertNotIn("ready", update.state())
        self.assertFalse((update.DIR / "99.0.0").exists())

    def test_turned_off_installs_nothing(self):
        self.stage("99.0.0")
        (self.data / "settings.json").write_text('{"auto_update": false}')
        self.assertIsNone(update.apply(say=lambda t: None))
        self.assertEqual(update.version_of(self.program), VERSION)

    def test_a_zip_made_on_windows_unpacks_into_folders(self):
        """Compress-Archive of an older PowerShell names entries with backslashes: Linux / macOS make folders."""
        z = self.root / "backslash.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("install.py", "")
            f.writestr("maxedcode\\version.py", 'VERSION = "99.0.0"')
        src = update.unpack(z, self.root / "out")
        self.assertEqual(update.version_of(src), "99.0.0")
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("..\\escape.txt", "")
        with self.assertRaises(ValueError):
            update.unpack(z, self.root / "out2")

    def test_the_release_zip_builder_keeps_scripts_runnable(self):
        """installer/zip_program.py (release.yml): forward slashes, LF and 755 for *.sh; unpack reads it back."""
        builder = REPO / "installer" / "zip_program.py"
        if not builder.exists():
            self.skipTest("no installer/zip_program.py in this copy")
        app = self.root / "app"
        (app / "maxedcode").mkdir(parents=True)
        (app / "install.py").write_text("")
        (app / "bootstrap.sh").write_bytes(b"#!/bin/sh\r\necho hi\r\n")
        (app / "maxedcode" / "version.py").write_text('VERSION = "99.0.0"')
        z = self.root / "maxedcode.zip"
        subprocess.run([sys.executable, str(builder), str(app), str(z)], check=True)
        with zipfile.ZipFile(z) as f:
            names = {i.filename: i for i in f.infolist()}
        self.assertEqual(sorted(names), ["bootstrap.sh", "install.py", "maxedcode/version.py"])
        self.assertEqual((names["bootstrap.sh"].external_attr >> 16) & 0o777, 0o755)
        src = update.unpack(z, self.root / "out")
        self.assertEqual(update.version_of(src), "99.0.0")
        self.assertEqual((src / "bootstrap.sh").read_bytes(), b"#!/bin/sh\necho hi\n")
        if os.name != "nt":
            self.assertTrue(os.access(src / "bootstrap.sh", os.X_OK))

    def test_process_check_works_on_this_system(self):
        self.assertTrue(update._alive(os.getpid()))
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.assertFalse(update._alive(p.pid))

    def test_a_stale_lock_does_not_block_forever(self):
        update.DIR.mkdir(parents=True, exist_ok=True)
        lock = update.DIR / "check.lock"
        lock.write_text("")
        old = time.time() - update.STALE_LOCK - 10
        os.utime(lock, (old, old))
        with update._Lock("check.lock") as mine:
            self.assertTrue(mine)


if __name__ == "__main__":
    unittest.main()
