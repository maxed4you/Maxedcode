import unittest

from maxedcode.tools import BUILTIN
from maxedcode.tools.todo import TaskProgressGuard, copy_tasks, is_verification_tool, tool_todo, validate


class DynamicTaskValidationTests(unittest.TestCase):
    def test_accepts_model_generated_english_steps(self):
        todos = [
            {"content": "Inspect the relevant files", "status": "in_progress"},
            {"content": "Implement the requested change", "status": "pending"},
            {"content": "Run focused regression tests", "status": "pending"},
        ]
        self.assertIsNone(validate(todos))
        self.assertIn("0/3 done", tool_todo(todos))

    def test_accepts_a_single_language_list_and_rejects_a_mixed_one(self):
        # The tool asks for the language of the user's request, so a fully Russian checklist is valid;
        # what is rejected is a checklist that mixes languages between its items.
        self.assertIsNone(validate([{"content": "Проверить файлы проекта", "status": "in_progress"}]))
        mixed = [{"content": "Проверить файлы проекта", "status": "in_progress"},
                 {"content": "Run tests", "status": "pending"}]
        self.assertIn("another language", validate(mixed))
        # A code span is not counted as language, so it can stay untranslated.
        self.assertIsNone(validate([{"content": "Проверить `config.py`", "status": "in_progress"},
                                    {"content": "Запустить тесты", "status": "pending"}]))

    def test_allows_non_english_characters_inside_a_backticked_path(self):
        todos = [{"content": "Update config at `C:\Проекты\app.py`", "status": "in_progress"}]
        self.assertIsNone(validate(todos))

    def test_accepts_long_checklists_without_a_fixed_cap(self):
        todos = [{"content": f"Step {i}", "status": "pending"} for i in range(12)]
        self.assertIsNone(validate(todos))

    def test_only_one_step_can_be_active(self):
        todos = [
            {"content": "Inspect code", "status": "in_progress"},
            {"content": "Run tests", "status": "in_progress"},
        ]
        self.assertIn("at most one", validate(todos))

    def test_model_has_a_dynamic_task_tool_in_the_users_language(self):
        tool = next(t for t in BUILTIN if t["function"]["name"] == "todo_write")
        function = tool["function"]
        self.assertIn("CURRENT user request", function["description"])
        # The prompt and validate() ask for the user's language; the schema must not contradict them.
        self.assertIn("USER'S LANGUAGE", function["description"])
        self.assertNotIn("IN ENGLISH", function["description"])
        self.assertNotIn("maxItems", function["parameters"]["properties"]["todos"])

    def test_snapshot_copy_is_independent_and_preserves_statuses(self):
        todos = [
            {"content": "Inspect code", "status": "done"},
            {"content": "Run tests", "status": "in_progress"},
        ]
        snapshot = copy_tasks(todos)
        snapshot[0]["status"] = "pending"
        self.assertEqual(todos[0]["status"], "done")
        self.assertEqual(snapshot[1]["status"], "in_progress")


class TodoStatusSpellingTests(unittest.TestCase):
    def test_completed_spelling_of_other_agents_is_accepted(self):
        # Claude Code / DeepSeek Harness say "completed"; a failed todo_write costs a whole model step.
        todos = [{"content": "Read the file", "status": "completed"}, {"content": "Fix it", "status": "In-Progress"}]
        self.assertIsNone(validate(todos))
        self.assertTrue(tool_todo(todos).startswith("1/2 done"))
        self.assertEqual([t["status"] for t in copy_tasks(todos)], ["done", "in_progress"])
        self.assertIn("status", validate([{"content": "x", "status": "maybe"}]))
        status = next(t for t in BUILTIN if t["function"]["name"] == "todo_write")["function"]["parameters"][
            "properties"]["todos"]["items"]["properties"]["status"]
        self.assertEqual(status["enum"], ["pending", "in_progress", "done"])


class TaskProgressGuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = TaskProgressGuard()
        self.plan = [
            {"content": "Inspect relevant files", "status": "in_progress"},
            {"content": "Implement the requested change", "status": "pending"},
            {"content": "Run focused regression tests", "status": "pending"},
        ]
        self.guard.plan_updated([], self.plan)

    def test_research_then_edit_requires_status_transition(self):
        self.guard.observe_tool("read_file", {"path": "app.py"}, True)
        error = self.guard.blocked_reason("edit_file", {"path": "app.py"}, self.plan)
        self.assertIn("research/inspection step marked done", error)

        updated = [
            {"content": "Inspect relevant files", "status": "done"},
            {"content": "Implement the requested change", "status": "in_progress"},
            {"content": "Run focused regression tests", "status": "pending"},
        ]
        self.assertTrue(self.guard.plan_updated(self.plan, updated))
        self.assertIsNone(self.guard.blocked_reason("edit_file", {"path": "app.py"}, updated))

    def test_unchanged_plan_does_not_clear_checkpoint(self):
        self.guard.observe_tool("grep", {"pattern": "foo"}, True)
        self.assertFalse(self.guard.plan_updated(self.plan, list(self.plan)))
        self.assertIsNotNone(self.guard.blocked_reason("write_file", {"path": "x.py"}, self.plan))

    def test_code_changes_then_test_requires_transition(self):
        self.guard.observe_tool("edit_file", {"path": "app.py"}, True)
        cmd = {"command": "python -m unittest discover -s tests -v"}
        self.assertTrue(is_verification_tool("powershell", cmd))
        self.assertIn("verification", " ".join(TaskProgressGuard.__doc__.split()))
        error = self.guard.blocked_reason("powershell", cmd, self.plan)
        self.assertIn("completed implementation", error)

        updated = [
            {"content": "Inspect relevant files", "status": "done"},
            {"content": "Implement the requested change", "status": "done"},
            {"content": "Run focused regression tests", "status": "in_progress"},
        ]
        self.guard.plan_updated(self.plan, updated)
        self.assertIsNone(self.guard.blocked_reason("powershell", cmd, updated))

    def test_only_successful_research_or_edits_create_checkpoints(self):
        self.guard.observe_tool("read_file", {"path": "missing.py"}, False)
        self.assertIsNone(self.guard.blocked_reason("edit_file", {"path": "app.py"}, self.plan))


if __name__ == "__main__":
    unittest.main(verbosity=2)
