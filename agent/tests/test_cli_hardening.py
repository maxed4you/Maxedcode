"""Offline regression tests for malformed tool calls, request history, and long paste."""
import json
import unittest

from maxedcode.tools import BUILTIN
from maxedcode.tool_validation import (
    normalize_paste_text,
    normalize_tool_arguments,
    sanitize_tool_history,
    validate_required_arguments,
)


class ToolArgumentHardeningTests(unittest.TestCase):
    def test_write_file_missing_path_is_rejected_before_execution(self):
        parsed, canonical, error = normalize_tool_arguments(
            "write_file", '{"content":"print(1)"}', BUILTIN
        )
        self.assertEqual(parsed, {"content": "print(1)"})
        self.assertEqual(json.loads(canonical), parsed)
        self.assertIn("'path'", error)
        self.assertIn("No changes were made", error)

    def test_invalid_json_is_replaced_with_valid_json_for_history(self):
        parsed, canonical, error = normalize_tool_arguments("write_file", '{"path":', BUILTIN)
        self.assertEqual(parsed, {})
        self.assertEqual(canonical, "{}")
        self.assertTrue(error)
        json.loads(canonical)  # history payload is always valid JSON

    def test_valid_arguments_are_canonicalized_without_losing_unicode(self):
        parsed, canonical, error = normalize_tool_arguments(
            "write_file", '{ "path" : "C:/проект/a.py", "content": "hello" }', BUILTIN
        )
        self.assertIsNone(error)
        self.assertEqual(json.loads(canonical), parsed)
        self.assertIn("проект", canonical)

    def test_hook_or_direct_execution_arguments_are_validated(self):
        error = validate_required_arguments("write_file", {"content": "x"}, BUILTIN)
        self.assertIn("'path'", error)

    def test_legacy_history_tool_arguments_are_sanitized(self):
        history = [
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "write_file", "arguments": '{"path":'},
            }]},
            {"role": "tool", "tool_call_id": "call_1", "content": "Error: KeyError: 'path'"},
        ]
        cleaned = sanitize_tool_history(history, BUILTIN)
        self.assertNotIn("tool_calls", cleaned[0])
        self.assertIn("malformed historical tool call", cleaned[0]["content"])
        self.assertFalse(any(item.get("role") == "tool" for item in cleaned))
        self.assertEqual(history[0]["tool_calls"][0]["function"]["arguments"], '{"path":')

    def test_legacy_json_valid_but_missing_path_pair_is_removed(self):
        history = [
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_bad", "type": "function",
                "function": {"name": "write_file", "arguments": '{"content":"hello"}'},
            }]},
            {"role": "tool", "tool_call_id": "call_bad", "content": "Error: KeyError: 'path'"},
        ]
        cleaned = sanitize_tool_history(history, BUILTIN)
        self.assertNotIn("tool_calls", cleaned[0])
        self.assertFalse(any(item.get("tool_call_id") == "call_bad" for item in cleaned))


class LongPasteTests(unittest.TestCase):
    def test_multiline_long_paste_is_not_truncated(self):
        source = ("line one\r\n" + "x" * 300 + "\r\n") * 300
        pasted = normalize_paste_text(source)
        self.assertEqual(len(pasted), len(source) - source.count("\r"))
        self.assertNotIn("\r", pasted)
        self.assertEqual(pasted.count("\n"), source.count("\n"))
        self.assertEqual(pasted.splitlines()[1], "x" * 300)

    def test_only_nul_bytes_are_removed(self):
        self.assertEqual(normalize_paste_text("a\x00b\tc\n"), "ab\tc\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
