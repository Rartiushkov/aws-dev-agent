import unittest

from bridges.plan_creator import create_plan, extract_lambda_name, autotest_step, extract_clone_request


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
        self.assertEqual(commands[-1], 'python -m unittest discover -s . -p "test_plan_creator.py"')

    def test_create_iam_role_goal_appends_autotest(self):
        plan = create_plan("Create IAM role for lambda")
        commands = [step["cmd"] for step in plan if step.get("type") == "command"]

        self.assertEqual(commands[-1], 'python -m unittest discover -s . -p "test_plan_creator.py"')

    def test_autotest_goal_returns_unittest_command(self):
        plan = create_plan("run autotest")
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["type"], "command")
        self.assertIn("python -m unittest discover", plan[0]["cmd"])

    def test_feature_autotest_uses_specific_pattern(self):
        step = autotest_step("run tests for cloudwatch")
        self.assertEqual(step["cmd"], 'python -m unittest discover -s . -p "test_runner_cloudwatch.py"')

    def test_feature_test_goal_returns_specific_test_file(self):
        plan = create_plan("run tests for lambda")
        self.assertEqual(plan[0]["cmd"], 'python -m unittest discover -s . -p "test_plan_creator.py"')

    def test_run_autotest_in_aws_uses_codebuild(self):
        plan = create_plan("run autotest in aws")
        self.assertEqual(plan[0]["cmd"], "aws codebuild start-build --project-name aws-dev-agent-tests")

    def test_extract_clone_request(self):
        request = extract_clone_request("Clone env from cluster core-cluster service api-service to new env virgin team payments")
        self.assertEqual(request["source_cluster"], "core-cluster")
        self.assertEqual(request["source_service"], "api-service")
        self.assertEqual(request["target_env"], "virgin")
        self.assertEqual(request["team"], "payments")

    def test_clone_env_goal_returns_snapshot_command(self):
        plan = create_plan("Clone env from cluster core-cluster service api-service to new env virgin team payments")
        self.assertIn("python executor/scripts/clone_ecs_env.py", plan[0]["cmd"])
        self.assertIn("--source-cluster core-cluster", plan[0]["cmd"])
        self.assertIn("--source-service api-service", plan[0]["cmd"])
        self.assertIn("--target-env virgin", plan[0]["cmd"])


if __name__ == "__main__":
    unittest.main()
