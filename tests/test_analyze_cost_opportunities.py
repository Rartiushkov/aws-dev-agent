import unittest

from executor.scripts.analyze_cost_opportunities import build_cost_report


class AnalyzeCostOpportunitiesTests(unittest.TestCase):

    def test_build_cost_report_flags_failing_ecs_service(self):
        snapshot = {
            "source_env": "legacy",
            "region": "us-east-1",
            "account_id": "123456789012",
            "lambda_functions": [],
            "s3_buckets": [],
            "dynamodb_tables": [],
            "ecs": {
                "services": [
                    {
                        "serviceName": "test-api",
                        "serviceArn": "arn:aws:ecs:::service/test-api",
                        "desiredCount": 1,
                        "runningCount": 0,
                        "capacityProviderStrategy": [{"capacityProvider": "FARGATE", "weight": 1}],
                        "taskDefinition": "arn:aws:ecs:::task-definition/test-api:1",
                        "events": [
                            {"message": "unable to place a task"},
                            {"message": "CannotPullContainerError: pull access denied"},
                        ],
                    }
                ],
                "task_definitions": [{"taskDefinitionArn": "arn:aws:ecs:::task-definition/test-api:1", "cpu": "1024", "memory": "2048"}],
            },
        }

        report = build_cost_report(snapshot)

        self.assertTrue(any(item["category"] == "ecs-waste" for item in report["opportunities"]))
        self.assertTrue(any(item["automation_ready"] for item in report["opportunities"]))

    def test_build_cost_report_flags_s3_without_lifecycle(self):
        snapshot = {
            "source_env": "legacy",
            "region": "us-east-1",
            "account_id": "123456789012",
            "lambda_functions": [],
            "dynamodb_tables": [],
            "s3_buckets": [{"Name": "logs-bucket", "LifecycleRules": []}],
            "ecs": {"services": [], "task_definitions": []},
        }

        report = build_cost_report(snapshot)

        self.assertTrue(any(item["category"] == "s3-lifecycle" for item in report["opportunities"]))

    def test_build_cost_report_flags_graviton_and_test_cleanup_candidates(self):
        snapshot = {
            "source_env": "legacy",
            "region": "us-east-1",
            "account_id": "123456789012",
            "lambda_functions": [
                {
                    "FunctionName": "hello-world-fn",
                    "FunctionArn": "arn:aws:lambda:::function:hello-world-fn",
                    "Runtime": "python3.12",
                    "Architectures": ["x86_64"],
                }
            ],
            "s3_buckets": [],
            "dynamodb_tables": [],
            "ecs": {"services": [], "task_definitions": []},
        }

        report = build_cost_report(snapshot)
        categories = {item["category"] for item in report["opportunities"]}

        self.assertIn("lambda-graviton", categories)
        self.assertIn("resource-cleanup", categories)

    def test_build_cost_report_records_pay_per_request_as_strength(self):
        snapshot = {
            "source_env": "legacy",
            "region": "us-east-1",
            "account_id": "123456789012",
            "lambda_functions": [],
            "s3_buckets": [],
            "dynamodb_tables": [
                {"Table": {"BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"}}},
                {"Table": {"BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"}}},
            ],
            "ecs": {"services": [], "task_definitions": []},
        }

        report = build_cost_report(snapshot)

        self.assertEqual(report["summary"]["opportunity_count"], 0)
        self.assertTrue(report["strengths"])


if __name__ == "__main__":
    unittest.main()
