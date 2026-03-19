import unittest

from bridges.plan_creator import create_plan, extract_lambda_name, autotest_step


class PlanCreatorTests(unittest.TestCase):

    def test_extract_lambda_name_from_named_goal(self):
        self.assertEqual(
            extract_lambda_name("Create Lambda named test-lambda-integration Test Lambda for integration"),
            "test-lambda-integration",
        )

    def test_create_lambda_named_goal_includes_function_name(self):
        plan = create_plan("Create Lambda named test-lambda-integration Test Lambda for integration")
        commands = [step["cmd"] for step in plan if step.get("type") == "command"]

        self.assertTrue(any("test-lambda-integration" in cmd for cmd in commands))
        self.assertTrue(any("aws lambda invoke" in cmd for cmd in commands))
        self.assertEqual(commands[-1], 'python -m unittest discover -s tests -p "test_*.py"')

    def test_create_iam_role_goal_appends_autotest(self):
        plan = create_plan("Create IAM role for lambda")
        commands = [step["cmd"] for step in plan if step.get("type") == "command"]

        self.assertEqual(commands[-1], 'python -m unittest discover -s tests -p "test_*.py"')

    def test_autotest_goal_returns_unittest_command(self):
        plan = create_plan("run autotest")
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["type"], "command")
        self.assertIn("python -m unittest discover", plan[0]["cmd"])

    def test_feature_autotest_uses_specific_pattern(self):
        step = autotest_step("run tests for cloudwatch")
        self.assertEqual(step["cmd"], 'python -m unittest discover -s tests -p "test_runner_cloudwatch.py"')

    def test_feature_test_goal_returns_specific_test_file(self):
        plan = create_plan("run tests for lambda")
        self.assertEqual(plan[0]["cmd"], 'python -m unittest discover -s tests -p "test_plan_creator.py"')


if __name__ == "__main__":
    unittest.main()
