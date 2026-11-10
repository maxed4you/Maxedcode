import json
import os
import unittest

from maxedcode.tool_validation import (
    name_did_you_mean, normalize_tool_arguments, normalization_notice,
    semantic_argument_problems, unknown_tool_error, validate_tool_arguments,
)
from maxedcode.tools.schemas import BUILTIN


class ArgumentSemanticsTests(unittest.TestCase):
    def test_reversed_read_range_is_rejected_before_dispatch(self):
        error = validate_tool_arguments("read_file", {"path": "a.py", "start_line": 20, "end_line": 5}, BUILTIN)
        self.assertIn("end_line 5 is before start_line 20", error)
        self.assertIn("No changes were made", error)

    @unittest.skipUnless(os.name == "nt", "the powershell tool exists only on Windows")
    def test_one_letter_typo_runs_the_only_close_tool(self):
        from maxedcode.tool_validation import canonical_tool_name
        for typo, name in (("powersell", "powershell"), ("ppowershell", "powershell"), ("wirte_file", "write_file")):
            self.assertEqual(canonical_tool_name(typo, BUILTIN), name)
        self.assertEqual(canonical_tool_name("grpe", BUILTIN), "grpe")       # short names are never guessed

    def test_unknown_tool_suggests_nearest_advertised_name(self):
        self.assertEqual(name_did_you_mean("read_flie", BUILTIN), "read_file")
        error = unknown_tool_error("read_flie", BUILTIN)
        self.assertIn("did you mean read_file", error)
        self.assertIn("No tool was run", error)

    def test_normalization_preserves_only_schema_supported_aliases(self):
        before = {"file_path": "./a.py", "old_text": "x", "new_text": "y"}
        fixed, canonical, error = normalize_tool_arguments("edit_file", json.dumps(before), BUILTIN)
        self.assertTrue(fixed.get("path", "").replace("\\", "/").endswith("/a.py"))
        self.assertNotIn("file_path", fixed)
        self.assertEqual(json.loads(canonical), fixed)
        self.assertFalse(error)
        notice = normalization_notice(before, fixed)
        self.assertIn("path added", notice)
        self.assertIn("file_path removed", notice)

    def test_clamped_arguments_are_not_semantic_errors(self):
        # The implementation intentionally clamps these values; validation must not reject them.
        self.assertEqual(semantic_argument_problems("read_file", {"path": "x", "limit": 99999}), [])
        self.assertEqual(semantic_argument_problems("glob", {"pattern": "*.py", "limit": 1000}), [])

    def test_empty_search_pattern_is_rejected(self):
        error = validate_tool_arguments("grep", {"pattern": "   ", "path": "."}, BUILTIN)
        self.assertIn("pattern is empty", error)


if __name__ == "__main__":
    unittest.main()
