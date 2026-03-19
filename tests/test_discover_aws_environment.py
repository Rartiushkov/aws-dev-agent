import unittest

from executor.scripts.discover_aws_environment import build_dependency_graph


class DiscoverAwsEnvironmentTests(unittest.TestCase):

    def test_build_dependency_graph_includes_lambda_role_and_queue_links(self):
        snapshot = {
            "sqs_queues": [
                {
                    "QueueName": "legacy-events",
                    "QueueArn": "arn:aws:sqs:us-east-1:123456789012:legacy-events",
                }
            ],
            "iam_roles": [
                {
                    "RoleName": "legacy-role",
                    "Arn": "arn:aws:iam::123456789012:role/legacy-role",
                }
            ],
            "sns_topics": [],
            "api_gateways": [],
            "lambda_event_source_mappings": [
                {
                    "FunctionArn": "arn:aws:lambda:us-east-1:123456789012:function:legacy-worker",
                    "EventSourceArn": "arn:aws:sqs:us-east-1:123456789012:legacy-events",
                }
            ],
            "lambda_permissions": [],
            "lambda_functions": [
                {
                    "FunctionName": "legacy-worker",
                    "FunctionArn": "arn:aws:lambda:us-east-1:123456789012:function:legacy-worker",
                    "Role": "arn:aws:iam::123456789012:role/legacy-role",
                    "Environment": {
                        "Variables": {
                            "QUEUE_ARN": "arn:aws:sqs:us-east-1:123456789012:legacy-events"
                        }
                    },
                }
            ],
        }

        graph = build_dependency_graph(snapshot)

        self.assertTrue(any(edge["relationship"] == "assumes-role" for edge in graph["edges"]))
        self.assertTrue(any(edge["relationship"] == "event-source-mapping" for edge in graph["edges"]))
        self.assertTrue(any(edge["relationship"] == "env:QUEUE_ARN" for edge in graph["edges"]))


if __name__ == "__main__":
    unittest.main()
