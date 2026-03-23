import unittest

from executor.scripts.analyze_unused_resources import build_unused_resource_report


class AnalyzeUnusedResourcesTests(unittest.TestCase):

    def test_report_flags_disabled_mapping_and_failing_ecs(self):
        snapshot = {
            "source_env": "legacy",
            "region": "us-east-1",
            "account_id": "123",
            "lambda_functions": [],
            "lambda_event_source_mappings": [
                {
                    "EventSourceMappingArn": "arn:mapping:1",
                    "FunctionArn": "arn:lambda:fn",
                    "EventSourceArn": "arn:sqs:q",
                    "State": "Disabled",
                }
            ],
            "sqs_queues": [],
            "s3_buckets": [],
            "dynamodb_tables": [],
            "ecs": {
                "services": [
                    {
                        "serviceName": "test-api",
                        "serviceArn": "arn:ecs:svc",
                        "desiredCount": 1,
                        "runningCount": 0,
                        "events": [{"message": "unable to place a task"}, {"message": "CannotPullContainerError"}],
                    }
                ]
            },
        }

        report = build_unused_resource_report(snapshot)
        categories = {item["category"] for item in report["findings"]}
        self.assertIn("disabled-trigger", categories)
        self.assertIn("failing-ecs-service", categories)

    def test_report_flags_test_lambda_idle_queue_and_bucket_lifecycle(self):
        snapshot = {
            "source_env": "legacy",
            "region": "us-east-1",
            "account_id": "123",
            "lambda_functions": [
                {"FunctionName": "hello-world-fn", "FunctionArn": "arn:lambda:hello"}
            ],
            "lambda_event_source_mappings": [],
            "sqs_queues": [
                {
                    "QueueName": "test-queue",
                    "QueueUrl": "https://example",
                    "Attributes": {
                        "QueueArn": "arn:sqs:test",
                        "ApproximateNumberOfMessages": "0",
                        "ApproximateNumberOfMessagesNotVisible": "0",
                        "ApproximateNumberOfMessagesDelayed": "0",
                    },
                }
            ],
            "s3_buckets": [{"Name": "artifacts", "LifecycleRules": []}],
            "dynamodb_tables": [],
            "ecs": {"services": []},
        }

        report = build_unused_resource_report(snapshot)
        categories = {item["category"] for item in report["findings"]}
        self.assertIn("test-lambda", categories)
        self.assertIn("orphan-lambda", categories)
        self.assertIn("idle-queue", categories)
        self.assertIn("bucket-lifecycle", categories)


if __name__ == "__main__":
    unittest.main()
