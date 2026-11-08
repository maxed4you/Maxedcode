import unittest

from maxedcode.tool import ToolRegistry
from maxedcode.tools.schemas import BUILTIN


class ToolRegistryTests(unittest.TestCase):
    def test_registry_sorts_schema_bytes_and_names(self):
        reversed_schemas = list(reversed(BUILTIN))
        a, b = ToolRegistry(BUILTIN), ToolRegistry(reversed_schemas)
        self.assertEqual(a.stable_bytes(), b.stable_bytes())
        self.assertEqual([t.name for t in a], sorted(t.name for t in a))

    def test_validation_happens_without_runner_execution(self):
        registry = ToolRegistry(BUILTIN)
        problems = registry.validate("read_file", {"path": "x", "start_line": 10, "end_line": 2})
        self.assertTrue(any(p["field"] == "end_line" for p in problems))

    def test_unknown_registry_tool_is_reported(self):
        problems = ToolRegistry(BUILTIN).validate("does_not_exist", {})
        self.assertEqual(problems[0]["field"], "tool")


if __name__ == "__main__":
    unittest.main()
