import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maxedcode import repo_map
from maxedcode.util import tok


class RepoMapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="maxedcode-repomap-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.patcher = patch.object(repo_map, "HOME", self.home)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_map_uses_python_signatures_and_token_budget(self):
        (self.root / "entry.py").write_text("from core import run\ndef main(x: int):\n    return run(x)\n", encoding="utf-8")
        (self.root / "core.py").write_text("class Runner:\n    def run(self, value):\n        return value\n", encoding="utf-8")
        text = repo_map.build_repo_map(self.root, budget_tokens=128)
        self.assertIn("def main(x: int)", text)
        self.assertIn("class Runner", text)
        self.assertLessEqual(tok(text), 128)

    def test_unchanged_file_is_read_from_tag_cache(self):
        (self.root / "core.py").write_text("def stable():\n    return 1\n", encoding="utf-8")
        repo_map.build_repo_map(self.root, 200)
        with patch.object(repo_map, "_parse_symbols", side_effect=AssertionError("cache miss")):
            second = repo_map.build_repo_map(self.root, 200)
        self.assertIn("stable", second)

    def test_deleted_files_do_not_leak_from_cache(self):
        path = self.root / "gone.py"
        path.write_text("def gone():\n    return 1\n", encoding="utf-8")
        repo_map.build_repo_map(self.root, 200)
        path.unlink()
        result = repo_map.build_repo_map(self.root, 200)
        self.assertNotIn("gone.py", result)
        self.assertNotIn("def gone", result)

    def test_recently_mentioned_file_ranks_higher(self):
        (self.root / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
        (self.root / "zeta.py").write_text("def zeta():\n    return 2\n", encoding="utf-8")
        result = repo_map.build_repo_map(self.root, 300, messages=[{"role": "user", "content": "inspect zeta.py"}])
        self.assertLess(result.index("zeta.py:"), result.index("alpha.py:"))

    def test_protected_file_never_appears_even_after_cached(self):
        (self.root / "secret.py").write_text("def secret():\n    return 1\n", encoding="utf-8")
        (self.root / "public.py").write_text("def public():\n    return 2\n", encoding="utf-8")
        with patch.object(repo_map, "protected", side_effect=lambda name: "secret" if name.endswith("secret.py") else None):
            result = repo_map.build_repo_map(self.root, 300)
        self.assertNotIn("secret.py", result)
        self.assertIn("public.py", result)


if __name__ == "__main__":
    unittest.main()
