import unittest
from unittest.mock import patch

from maxedcode import permissions


class ExecPolicyTests(unittest.TestCase):
    def test_prefix_deny_matches_only_the_complete_token_prefix(self):
        policy = {"rules": [{"prefix": ["git", "push"], "decision": "deny"}]}
        self.assertEqual(permissions.exec_policy_decision("powershell", "git push origin main", policy)[0], "deny")
        self.assertIsNone(permissions.exec_policy_decision("powershell", "git status", policy)[0])

    def test_allow_prefix_is_opt_in(self):
        rule = {"prefix": ["pytest"], "decision": "allow"}
        self.assertIsNone(permissions.exec_policy_decision("powershell", "pytest -q", {"rules": [rule]})[0])
        self.assertEqual(permissions.exec_policy_decision("powershell", "pytest -q", {
            "honor_allow_prefixes": True, "rules": [rule]})[0], "allow")

    def test_regex_rules_apply_to_python_runner_too(self):
        policy = {"rules": [{"pattern": r"subprocess\.Popen", "decision": "ask"}]}
        self.assertEqual(permissions.exec_policy_decision("py_run", "import subprocess; subprocess.Popen(['x'])", policy)[0], "ask")

    def test_deny_and_ask_precede_saved_allow(self):
        with patch.object(permissions, "PROTECTED", []), \
             patch.object(permissions, "SETTINGS", {"exec_policy": {"rules": [
                 {"prefix": ["git", "push"], "decision": "deny"}]}}), \
             patch.object(permissions, "load", return_value={"allow": ["powershell"], "deny": [], "ask": []}):
            self.assertEqual(permissions.check("powershell", "git push main"), "deny")

    def test_requested_amendment_is_proposal_only(self):
        proposal = permissions.requested_exec_policy_amendment("powershell", "pytest -q tests")
        self.assertTrue(proposal["requested_amendment"])
        self.assertEqual(proposal["prefix"], ["pytest", "-q", "tests"])


if __name__ == "__main__":
    unittest.main()
