"""Cache safety: side-channel context must not become a user/system message in the middle of a turn."""
import unittest

from maxedcode.util import attach_late


class LateContextTests(unittest.TestCase):
    def test_goes_into_the_freshest_tool_result_and_adds_no_message(self):
        msgs = [{"role": "user", "content": "task"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function",
                                                                       "function": {"name": "read_file", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "1", "content": "file body"}]
        self.assertTrue(attach_late(msgs, ["<rule x>use tabs</rule>"]))
        self.assertEqual(len(msgs), 3)
        self.assertTrue(msgs[-1]["content"].startswith("file body"))
        self.assertIn("<rule x>use tabs</rule>", msgs[-1]["content"])

    def test_without_pending_tool_result_it_is_an_ordinary_user_message(self):
        msgs = [{"role": "assistant", "content": "done"}]
        attach_late(msgs, ["bg task finished"])
        self.assertEqual(msgs[-1]["role"], "user")
        self.assertIn("bg task finished", msgs[-1]["content"])

    def test_empty_items_change_nothing(self):
        msgs = [{"role": "tool", "tool_call_id": "1", "content": "x"}]
        self.assertFalse(attach_late(msgs, ["", "  "]))
        self.assertEqual(msgs[-1]["content"], "x")


if __name__ == "__main__":
    unittest.main()
