"""Optional API-field fallback must not discard prompt-cache affinity on unrelated errors."""
import unittest

from maxedcode.request_compat import unsupported_optional_fields


class RequestCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.fields = {
            "cache_prompt", "promptCacheKey", "strata_prefix", "top_k", "min_p",
            "reasoning_effort", "reasoning_budget_tokens", "chat_template_kwargs",
        }

    def test_named_sampling_parameter_is_removed_alone(self):
        got = unsupported_optional_fields("Unrecognized request argument supplied: top_k", self.fields)
        self.assertEqual(got, {"top_k"})

    def test_named_cache_parameter_is_removed_alone(self):
        got = unsupported_optional_fields("Unexpected field strata_prefix", self.fields)
        self.assertEqual(got, {"strata_prefix"})

    def test_generic_optional_error_preserves_cache_controls(self):
        got = unsupported_optional_fields("unsupported request parameter", self.fields)
        self.assertEqual(got, {"top_k", "min_p", "reasoning_effort", "reasoning_budget_tokens", "chat_template_kwargs"})
        self.assertNotIn("promptCacheKey", got)
        self.assertNotIn("strata_prefix", got)
        self.assertNotIn("cache_prompt", got)

    def test_malformed_tool_history_is_not_misclassified_as_optional_field_error(self):
        got = unsupported_optional_fields("Invalid tool_calls: missing tool_call_id", self.fields)
        self.assertIsNone(got)

    def test_non_optional_server_error_is_not_a_cache_fallback(self):
        got = unsupported_optional_fields("model overloaded; retry later", self.fields)
        self.assertIsNone(got)


if __name__ == "__main__":
    unittest.main()
