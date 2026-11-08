import unittest
from unittest.mock import patch

from maxedcode import config
from maxedcode.tools.catalog import compact_tool_catalog, session_catalog


class ToolCatalogTests(unittest.TestCase):
    def test_compact_catalog_sorts_names_and_keeps_required_signatures(self):
        specs = [
            {"type": "function", "function": {"name": "z_tool", "description": "Last tool.", "parameters": {"type": "object", "properties": {"arg": {"type": "string"}}, "required": ["arg"]}}},
            {"type": "function", "function": {"name": "a_tool", "description": "First tool.", "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": []}}},
        ]
        result = compact_tool_catalog(specs, excluded=set())
        self.assertLess(result.index("tools.a_tool"), result.index("tools.z_tool"))
        self.assertIn("arg!:string", result)
        self.assertIn("limit:integer", result)

    def test_code_mode_is_off_by_default(self):
        with patch.object(config, "CODE_MODE", False):
            self.assertNotIn("code_mode", [item["function"]["name"] for item in session_catalog()])

    @patch.object(config, "CODE_MODE", True)
    def test_session_catalog_embeds_code_mode_catalog_without_reordering_tools(self):
        specs = session_catalog()
        names = [item["function"]["name"] for item in specs]
        self.assertEqual(names, [item["function"]["name"] for item in session_catalog()])
        code_mode = next(item for item in specs if item["function"]["name"] == "code_mode")
        self.assertIn("Available inner tools and exact arguments", code_mode["function"]["description"])
        self.assertIn("tools.read_file(path!:string,", code_mode["function"]["description"])
        self.assertNotIn("tools.code_mode(", code_mode["function"]["description"])


if __name__ == "__main__":
    unittest.main()
