"""Regression tests for schema-driven tool-call argument repair."""
import json
import ntpath
import os
import unittest

from maxedcode.tool_validation import normalize_tool_arguments


def spec(name, properties, required=()):
    return {"type": "function", "function": {"name": name, "parameters": {
        "type": "object", "properties": properties, "required": list(required), "additionalProperties": False}}}


class ToolInputRepairTests(unittest.TestCase):
    def setUp(self):
        self.schemas = [
            spec("read_file", {"path": {"type": "string"}, "start_line": {"type": "integer"},
                               "limit": {"type": "integer"}}, ["path"]),
            spec("powershell", {"command": {"type": "string"}, "run_in_background": {"type": "boolean"}}, ["command"]),
            spec("grep", {"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern", "path"]),
            spec("patch", {"path": {"type": "string"}, "blocks": {"type": "array", "items": {
                "type": "object", "properties": {"old_text": {"type": "string"}, "new_text": {"type": "string"}},
                "required": ["old_text", "new_text"]}}}, ["path", "blocks"]),
        ]

    def normalize(self, name, value):
        return normalize_tool_arguments(name, json.dumps(value), self.schemas)

    def test_unrepaired_arguments_keep_the_models_bytes(self):
        # The server saw these exact tokens; a compact re-dump would make it re-read the whole reply.
        path = os.path.abspath("calc.py")
        raw = '{"path": ' + json.dumps(path) + ', "blocks": [{"old_text": "a", "new_text": "b"}]}'
        _, canonical, error = normalize_tool_arguments("patch", raw, self.schemas)
        self.assertIsNone(error)
        self.assertEqual(canonical, raw)
        # A real repair (string -> integer) still sends the repaired, re-serialized value.
        _, canonical, _ = normalize_tool_arguments("read_file", '{"path": ' + json.dumps(path) + ', "start_line": "3"}',
                                                   self.schemas)
        self.assertEqual(json.loads(canonical)["start_line"], 3)
        self.assertNotIn(" ", canonical.replace(path, ""))

    def test_aliases_unknown_fields_and_integer_strings(self):
        parsed, canonical, error = self.normalize("read_file", {"file_path": "src/main.py", "start_line": "12", "unused": 5})
        self.assertIsNone(error)
        self.assertTrue(os.path.isabs(parsed["path"]) or ntpath.isabs(parsed["path"]))
        self.assertTrue(parsed["path"].replace("\\", "/").endswith("src/main.py"))
        self.assertEqual(parsed["start_line"], 12)
        self.assertEqual(json.loads(canonical), parsed)

    def test_command_alias_and_boolean_string(self):
        parsed, _, error = self.normalize("powershell", {"cmd": "Get-Location", "run_in_background": "false", "junk": True})
        self.assertIsNone(error)
        self.assertEqual(parsed, {"command": "Get-Location", "run_in_background": False})

    def test_regex_alias(self):
        parsed, _, error = self.normalize("grep", {"regex": "TODO", "path": "."})
        self.assertIsNone(error)
        self.assertEqual(parsed["pattern"], "TODO")
        self.assertTrue(os.path.isabs(parsed["path"]) or ntpath.isabs(parsed["path"]))

    def test_nested_object_array_values_are_coerced_and_unknown_keys_removed(self):
        parsed, _, error = self.normalize("patch", {"path": "a.txt", "blocks": [{"old_text": 42, "new_text": "done", "extra": 1}]})
        self.assertIsNone(error)
        self.assertTrue(os.path.isabs(parsed["path"]) or ntpath.isabs(parsed["path"]))
        self.assertTrue(parsed["path"].replace("\\", "/").endswith("a.txt"))
        self.assertEqual(parsed["blocks"], [{"old_text": "42", "new_text": "done"}])

    def test_top_level_pairs_can_be_repaired_to_object(self):
        parsed, canonical, error = normalize_tool_arguments("powershell", '[["cmd", "Get-Location"]]', self.schemas)
        self.assertIsNone(error)
        self.assertEqual(parsed["command"], "Get-Location")
        self.assertEqual(json.loads(canonical), parsed)

    def test_nested_json_objects_and_local_refs_are_repaired(self):
        ref_schema = spec("complex", {
            "config": {"$ref": "#/$defs/Config"},
            "counts": {"type": "object", "additionalProperties": {"type": "integer"}},
        }, ["config"])
        ref_schema["function"]["parameters"]["$defs"] = {
            "Config": {"type": "object", "properties": {
                "enabled": {"type": "boolean"}, "limit": {"type": "integer"}},
                "required": ["enabled"], "additionalProperties": False}
        }
        args = json.dumps({"config": json.dumps({"enabled": "false", "limit": "12", "x": 1}),
                           "counts": {"one": "2", "two": "3"}})
        parsed, _, error = normalize_tool_arguments("complex", args, [ref_schema])
        self.assertIsNone(error)
        self.assertEqual(parsed, {"config": {"enabled": False, "limit": 12}, "counts": {"one": 2, "two": 3}})

    def test_union_schemas_are_not_guessed_and_optional_placeholders_are_removed(self):
        schema = spec("union", {
            "maybe_count": {"type": "integer"},
            "value": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
            "object": {"type": "object", "properties": {"id": {"type": "string"}}},
        })
        parsed, _, error = normalize_tool_arguments("union", '{"maybe_count":null,"value":"2","object":{}}', [schema])
        self.assertIsNone(error)
        self.assertEqual(parsed, {"value": "2", "object": {}})

    def test_numeric_boolean_and_array_repair_are_conservative(self):
        schema = spec("typed", {
            "count": {"type": "integer"}, "enabled": {"type": "boolean"},
            "items": {"type": "array", "items": {"type": "integer"}},
            "pair": {"type": "array", "prefixItems": [{"type": "integer"}, {"type": "boolean"}]},
        })
        parsed, _, error = normalize_tool_arguments(
            "typed", '{"count":"2.5","enabled":"off","items":"2","pair":["7","false"]}', [schema]
        )
        self.assertIsNone(error)
        self.assertEqual(parsed, {"count": "2.5", "enabled": "off", "items": [2], "pair": [7, False]})

    def test_remote_refs_are_not_loaded_and_closed_schema_drops_unknown_fields(self):
        schema = spec("local", {"value": {"type": "string"}})
        parsed, _, error = normalize_tool_arguments(
            "local", '{"value":"ok","extra":true,"payload":{"$ref":"https://example.invalid/schema"}}', [schema]
        )
        self.assertIsNone(error)
        self.assertEqual(parsed, {"value": "ok"})

    def test_windows_drive_path_is_normalized_without_tilde_expansion(self):
        from maxedcode.tool_validation import _normalize_path
        self.assertEqual(_normalize_path("C:/work\\src.py"), "C:\\work\\src.py")
        tilde = _normalize_path("~/project/file.py")
        self.assertIn("~", tilde)

    def test_leaked_closing_tag_tail_is_cut_from_a_path(self):
        # Live A1 run: the model ended the path with "\n</"; joining the lines made "...\\sandbox<", an invalid path.
        from maxedcode.tool_validation import _normalize_path
        self.assertEqual(_normalize_path("C:\\work\\sandbox\n</"), "C:\\work\\sandbox")
        self.assertEqual(_normalize_path("C:\\work\\a.py</parameter>"), "C:\\work\\a.py")
        # Real session: the rest of the tool-call markup, with backslashes, ended a write_file path.
        self.assertEqual(_normalize_path("C:\\work\\probe.py<\\parameter><\\<\\function>"), "C:\\work\\probe.py")

    def test_leaked_call_closing_tags_are_cut_from_text_values(self):
        # Real LCB abc396_d run: a correct solution.py ended with the call's own closing tags -> SyntaxError
        # and one more step to rewrite the file.
        schemas = [spec("write_file", {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])]
        code = 'if __name__ == "__main__":\n    main()\n'
        leaked = code + "\n</parameter>\n</\nparameter>\n</invoke>\n</function>\n</tool_call>"
        parsed, _, error = normalize_tool_arguments(
            "write_file", json.dumps({"path": os.path.abspath("s.py"), "content": leaked}), schemas)
        self.assertIsNone(error)
        self.assertEqual(parsed["content"], code)
        # One trailing XML tag is real content (an XML file); it stays.
        for kept in ("<a>\n</function>\n", "<root><invoke/></root>", "x </parameter>"):
            parsed, _, _ = normalize_tool_arguments(
                "write_file", json.dumps({"path": os.path.abspath("s.xml"), "content": kept}), schemas)
            self.assertEqual(parsed["content"], kept)

    def test_arguments_cut_off_inside_a_text_value_are_not_run(self):
        # Real session: a 10.7k-char write_file ended mid-row; repair_json closed the string and half a file was written.
        schemas = [spec("write_file", {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
                   spec("read_file", {"path": {"type": "string"}}, ["path"])]
        path = json.dumps(os.path.abspath("data.csv"))
        cut = '{"path": ' + path + ', "content": "a,b\\n1,2\\n3,'
        parsed, canonical, error = normalize_tool_arguments("write_file", cut, schemas)
        self.assertEqual(parsed, {})
        self.assertIn("cut off", error)
        self.assertEqual(canonical, "{}")
        # Only the closing brace missing: the text is complete, the usual repair still applies.
        parsed, _, error = normalize_tool_arguments("write_file", '{"path": ' + path + ', "content": "x"', schemas)
        self.assertIsNone(error)
        self.assertEqual(parsed["content"], "x")
        # Tools that do not act on the text keep the old repair.
        _, _, error = normalize_tool_arguments("read_file", '{"path": ' + path[:-3], schemas)
        self.assertFalse(error and "cut off" in error)

    def test_reply_cut_by_the_output_limit_does_not_write(self):
        # The server may close the JSON itself; finish_reason "length" is then the only sign.
        schemas = [spec("write_file", {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])]
        raw = json.dumps({"path": os.path.abspath("s.py"), "content": "def f():\n    retu"})
        parsed, canonical, error = normalize_tool_arguments("write_file", raw, schemas, "length")
        self.assertIn("cut off", error)
        self.assertEqual(canonical, raw)          # the model's bytes stay in the history (prefix cache)
        _, _, error = normalize_tool_arguments("write_file", raw, schemas, "stop")
        self.assertIsNone(error)

    def test_ask_user_options_sent_as_one_string_are_unpacked(self):
        # Real session: the person got one garbage option '["["A", "B", ...]' and a question ending in markup.
        schemas = [spec("ask_user", {"question": {"type": "string"},
                                     "options": {"type": "array", "items": {"type": "string"}}}, ["question"])]
        raw = json.dumps({"question": "What to frame?\n</</option>",
                          "options": ['["["Each choice", "Each group", "Other - I will describe it"]']})
        parsed, _, error = normalize_tool_arguments("ask_user", raw, schemas)
        self.assertIsNone(error)
        self.assertEqual(parsed["question"], "What to frame?")
        self.assertEqual(parsed["options"], ["Each choice", "Each group", "Other - I will describe it"])
        clean = json.dumps({"question": "Pick one", "options": ["a [b]", "c"]})
        self.assertEqual(normalize_tool_arguments("ask_user", clean, schemas)[1], clean)   # untouched, same bytes



class TextToolCallTests(unittest.TestCase):
    """A call the server left in the text must run, not become the final answer as raw XML."""

    def test_qwen_xml_call_is_recovered(self):
        from maxedcode.tool_validation import text_tool_calls
        text = ("Sure.\n<tool_call>\n<function=read_file>\n<parameter=path>\nD:/a.py\n</parameter>\n"
                "<parameter=limit>\n20\n</parameter>\n</function>\n</tool_call>")
        calls, rest = text_tool_calls(text)
        self.assertEqual(rest, "Sure.")
        self.assertEqual(calls[0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"path": "D:/a.py", "limit": "20"})

    def test_plain_text_is_left_alone(self):
        from maxedcode.tool_validation import text_tool_calls
        self.assertEqual(text_tool_calls("no calls here"), ([], "no calls here"))

    def test_hermes_json_calls_are_recovered(self):
        from maxedcode.tool_validation import text_tool_calls
        text = ('Let me look.\n<tool_call>\n{"name": "read_file", "arguments": {"path": "D:/a.py"}}\n</tool_call>\n'
                '<tool_call>\n{"function": {"name": "grep", "arguments": "{\\"pattern\\": \\"x\\"}"}}\n</tool_call>')
        calls, rest = text_tool_calls(text)
        self.assertEqual(rest, "Let me look.")
        self.assertEqual([c["function"]["name"] for c in calls], ["read_file", "grep"])
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"path": "D:/a.py"})
        self.assertEqual(json.loads(calls[1]["function"]["arguments"]), {"pattern": "x"})

    def test_hermes_call_cut_off_or_with_trailing_comma(self):
        from maxedcode.tool_validation import text_tool_calls
        calls, _ = text_tool_calls('<tool_call>\n{"name": "glob", "arguments": {"pattern": "*.py",}}')
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"pattern": "*.py"})

    def test_bare_json_call_needs_an_offered_name_and_no_prose(self):
        from maxedcode.tool_validation import text_tool_calls
        specs = [spec("read_file", {"path": {"type": "string"}}, ["path"])]
        bare = '```json\n{"name": "read_file", "arguments": {"path": "D:/a.py"}}\n```'
        calls, rest = text_tool_calls(bare, tool_specs=specs)
        self.assertEqual((calls[0]["function"]["name"], rest), ("read_file", ""))
        self.assertEqual(text_tool_calls(bare)[0], [])                                   # no specs: no guess
        self.assertEqual(text_tool_calls('{"name": "deploy", "arguments": {}}', tool_specs=specs)[0], [])
        prose = 'The config is:\n{"name": "read_file", "arguments": {}}'
        self.assertEqual(text_tool_calls(prose, tool_specs=specs), ([], prose))


class ToolNameAliasTests(unittest.TestCase):
    def test_names_from_other_agents_map_to_ours(self):
        from maxedcode.tool_validation import canonical_tool_name
        specs = [spec("powershell", {"command": {"type": "string"}}, ["command"]),
                 spec("grep", {"pattern": {"type": "string"}}, ["pattern"])]
        self.assertEqual(canonical_tool_name("run_command", specs), "powershell")
        self.assertEqual(canonical_tool_name("Bash", specs), "powershell")
        self.assertEqual(canonical_tool_name("grep_search", specs), "grep")
        self.assertEqual(canonical_tool_name("read", specs), "read")        # target not offered: no guess
        self.assertEqual(canonical_tool_name("weird_tool", specs), "weird_tool")

    def test_unknown_name_maps_only_when_the_arguments_fit_one_tool(self):
        # Live runs: "run"/"match" with {"command": ...} and "powers" were each repeated after "unknown tool".
        from maxedcode.tool_validation import canonical_tool_name
        specs = [spec("powershell", {"command": {"type": "string"}, "timeout": {"type": "integer"}}, ["command"]),
                 spec("py_run", {"code": {"type": "string"}}, ["code"]),
                 spec("grep", {"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern", "path"]),
                 spec("glob", {"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"])]
        self.assertEqual(canonical_tool_name("run", specs, {"command": "python -m unittest"}), "powershell")
        self.assertEqual(canonical_tool_name("match", specs, {"command": "dir", "timeout": 30}), "powershell")
        self.assertEqual(canonical_tool_name("powers", specs, {}), "powershell")          # a cut-off name
        self.assertEqual(canonical_tool_name("match", specs, {"pattern": "x", "path": "C:/p"}), "match")   # two fit
        self.assertEqual(canonical_tool_name("run", specs, {"command": "x", "extra": 1}), "run")   # not its field
        self.assertEqual(canonical_tool_name("run", specs), "run")                                # no arguments
        self.assertEqual(canonical_tool_name("mcp__srv__run", specs, {"command": "x"}), "mcp__srv__run")


if __name__ == "__main__":
    unittest.main()
