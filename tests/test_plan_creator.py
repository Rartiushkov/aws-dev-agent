import unittest

from bridges.plan_creator import (
    autotest_step,
    build_create_lambda_plan,
    create_plan,
    extract_clone_request,
    extract_lambda_name,
)


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

    def test_discover_environment_goal_returns_discovery_command(self):
        plan = create_plan("discover environment named legacy team platform")
        self.assertIn("python executor/scripts/discover_aws_environment.py", plan[0]["cmd"])
        self.assertIn("--source-env legacy", plan[0]["cmd"])
        self.assertIn("--team platform", plan[0]["cmd"])

    def test_deploy_discovered_env_goal_returns_deploy_and_validate(self):
        plan = create_plan("deploy discovered env from legacy to new env virgin team payments")
        self.assertIn("python executor/scripts/deploy_discovered_env.py", plan[0]["cmd"])
        self.assertIn("--source-env legacy", plan[0]["cmd"])
        self.assertIn("--target-env virgin", plan[0]["cmd"])
        self.assertIn("python executor/scripts/validate_deployed_env.py --target-env virgin", plan[1]["cmd"])

    def test_destroy_target_env_goal_returns_destroy_command(self):
        plan = create_plan("destroy target env virgin")
        self.assertIn("python executor/scripts/destroy_deployed_env.py --target-env virgin", plan[0]["cmd"])

    def test_risk_scan_goal_returns_risk_scan_command(self):
        plan = create_plan("scan risks for legacy")
        self.assertIn("python executor/scripts/scan_environment_risks.py --source-env legacy", plan[0]["cmd"])

    def test_git_backup_goal_returns_backup_command(self):
        plan = create_plan("backup git for legacy")
        self.assertIn("python executor/scripts/backup_git_repos.py --source-env legacy", plan[0]["cmd"])

    def test_migration_strategy_goal_returns_strategy_command(self):
        plan = create_plan("build migration strategy for legacy")
        self.assertIn("python executor/scripts/build_migration_strategy.py --source-env legacy", plan[0]["cmd"])

    def test_s3_transfer_goal_returns_command(self):
        plan = create_plan("transfer s3 objects for legacy")
        self.assertIn("python executor/scripts/transfer_s3_objects.py --source-env legacy", plan[0]["cmd"])

    def test_network_plan_goal_returns_command(self):
        plan = create_plan("build network plan for legacy")
        self.assertIn("python executor/scripts/build_network_migration_plan.py --source-env legacy", plan[0]["cmd"])

    def test_kms_goal_returns_command(self):
        plan = create_plan("analyze kms for legacy")
        self.assertIn("python executor/scripts/analyze_kms_usage.py --source-env legacy", plan[0]["cmd"])

    def test_iac_goal_returns_command(self):
        plan = create_plan("export iac for legacy")
        self.assertIn("python executor/scripts/export_iac_blueprint.py --source-env legacy", plan[0]["cmd"])

    def test_git_connection_goal_returns_test_command(self):
        plan = create_plan("test git connection using configs/client.json")
        self.assertIn("python executor/scripts/test_git_connection.py --config configs/client.json", plan[0]["cmd"])

    def test_export_backup_goal_returns_export_command(self):
        plan = create_plan("export aws backup to git for legacy using configs/client.json")
        self.assertIn("python executor/scripts/export_aws_backup_to_git.py --source-env legacy --config configs/client.json --init-git --commit", plan[0]["cmd"])

    def test_redeploy_discovered_env_goal_returns_destroy_deploy_validate(self):
        plan = create_plan("redeploy discovered env from legacy to new env virgin team payments")
        self.assertIn("python executor/scripts/destroy_deployed_env.py --target-env virgin", plan[0]["cmd"])
        self.assertIn("python executor/scripts/deploy_discovered_env.py --source-env legacy --target-env virgin --team payments", plan[1]["cmd"])
        self.assertIn("python executor/scripts/validate_deployed_env.py --target-env virgin", plan[2]["cmd"])

    def test_build_create_lambda_plan_supports_runtime_template_and_custom_role(self):
        plan = build_create_lambda_plan(
            "orders-webhook",
            runtime="python3.12",
            template_id="api-handler",
            iam_scope="custom",
            role_arn="arn:aws:iam::123456789012:role/custom-role",
            include_test=True,
        )
        commands = [step["cmd"] for step in plan]

        self.assertTrue(any("--runtime python3.12" in cmd for cmd in commands))
        self.assertTrue(any("arn:aws:iam::123456789012:role/custom-role" in cmd for cmd in commands))
        self.assertTrue(any("lambda invoke --function-name orders-webhook" in cmd for cmd in commands))

    def test_build_create_lambda_plan_supports_sqs_trigger(self):
        plan = build_create_lambda_plan(
            "orders-worker",
            template_id="sqs-consumer",
            trigger_type="sqs",
            trigger_source="arn:aws:sqs:us-east-1:123456789012:orders-queue",
        )
        commands = [step["cmd"] for step in plan]

        self.assertTrue(any("create-event-source-mapping" in cmd for cmd in commands))
        self.assertTrue(any("orders-queue" in cmd for cmd in commands))


if __name__ == "__main__":
    unittest.main()
