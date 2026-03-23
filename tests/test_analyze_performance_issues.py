import unittest

from executor.scripts.analyze_performance_issues import analyze_performance, build_client_performance_markdown, build_why_is_it_slow_markdown, build_why_is_it_slow_report


class AnalyzePerformanceIssuesTests(unittest.TestCase):

    def test_analyze_performance_flags_broken_lambda_and_ecs_service(self):
        snapshot = {
            "source_env": "legacy",
            "region": "us-east-1",
            "account_id": "123",
            "lambda_functions": [{"FunctionArn": "arn:lambda:fn", "FunctionName": "worker", "MemorySize": 128, "Timeout": 3}],
            "lambda_event_source_mappings": [
                {
                    "EventSourceMappingArn": "arn:mapping:1",
                    "FunctionArn": "arn:lambda:fn",
                    "EventSourceArn": "arn:sqs:q",
                    "State": "Enabled",
                    "LastProcessingResult": "PROBLEM: access denied",
                }
            ],
            "sqs_queues": [],
            "dynamodb_tables": [],
            "ecs": {
                "services": [
                    {
                        "serviceName": "api",
                        "serviceArn": "arn:ecs:svc",
                        "desiredCount": 1,
                        "runningCount": 0,
                        "events": [{"message": "CannotPullContainerError"}],
                        "networkConfiguration": {"awsvpcConfiguration": {"assignPublicIp": "ENABLED", "subnets": ["a", "b", "c"]}},
                        "taskDefinition": "td-1",
                    }
                ],
                "task_definitions": [{"taskDefinitionArn": "td-1", "cpu": "1024", "memory": "2048"}],
            },
            "ecs_metrics": {"services": []},
        }

        report = analyze_performance(snapshot)
        categories = {item["category"] for item in report["findings"]}

        self.assertIn("lambda-trigger-failure", categories)
        self.assertIn("ecs-unavailable", categories)
        self.assertIn("lambda-tight-config", categories)

    def test_markdown_renders_findings(self):
        report = {
            "generated_at": "2026-03-21T00:00:00Z",
            "root_cause_summary": "Most likely slowdown drivers: an event-driven Lambda consumer is broken.",
            "summary": {"finding_count": 1},
            "findings": [
                {
                    "severity": "high",
                    "title": "Lambda event source is failing to process records",
                    "resource_id": "arn:mapping:1",
                    "evidence": "PROBLEM: access denied",
                    "probable_cause": "consumer is broken",
                    "recommended_action": "fix IAM",
                }
            ],
        }

        markdown = build_client_performance_markdown(report)

        self.assertIn("AWS Performance Report", markdown)
        self.assertIn("Lambda event source is failing to process records", markdown)
        self.assertIn("Most likely slowdown drivers", markdown)

    def test_why_is_it_slow_report_includes_dependency_chain(self):
        performance_report = {
            "generated_at": "2026-03-21T00:00:00Z",
            "source_env": "legacy",
            "region": "us-east-1",
            "account_id": "123",
            "root_cause_summary": "Most likely slowdown drivers: an event-driven Lambda consumer is broken.",
            "summary": {"finding_count": 1},
            "findings": [
                {
                    "severity": "high",
                    "title": "Lambda event source is failing to process records",
                    "resource_id": "mapping-a",
                    "probable_cause": "consumer is broken",
                    "recommended_action": "fix IAM",
                }
            ],
        }
        dependency_graph = {
            "nodes": [
                {"id": "mapping-a", "name": "mapping-a", "type": "lambda-event-source-mapping"},
                {"id": "lambda-a", "name": "lambda-a", "type": "lambda"},
                {"id": "queue-a", "name": "queue-a", "type": "sqs"},
                {"id": "table-a", "name": "table-a", "type": "dynamodb"},
            ],
            "edges": [
                {"from": "queue-a", "to": "mapping-a", "relationship": "feeds-mapping"},
                {"from": "mapping-a", "to": "lambda-a", "relationship": "invokes-function"},
                {"from": "lambda-a", "to": "table-a", "relationship": "env:TABLE_ARN"},
            ],
        }

        report = build_why_is_it_slow_report(performance_report, dependency_graph=dependency_graph)
        markdown = build_why_is_it_slow_markdown(report)

        self.assertEqual(report["top_incidents"][0]["dependency_chain"][0]["id"], "queue-a")
        self.assertEqual(report["top_incidents"][0]["dependency_chain"][1]["id"], "mapping-a")
        self.assertIn("Business impact", markdown)
        self.assertIn("Dependency chain", markdown)

    def test_why_is_it_slow_report_groups_related_ecs_findings(self):
        performance_report = {
            "generated_at": "2026-03-21T00:00:00Z",
            "source_env": "legacy",
            "region": "us-east-1",
            "account_id": "123",
            "root_cause_summary": "Most likely slowdown drivers: an ECS service cannot start healthy tasks.",
            "summary": {"finding_count": 2},
            "findings": [
                {
                    "severity": "high",
                    "category": "ecs-unavailable",
                    "title": "ECS service has desired tasks but no healthy running capacity",
                    "resource_id": "service-a",
                    "probable_cause": "service-a is unhealthy",
                    "recommended_action": "fix image",
                },
                {
                    "severity": "high",
                    "category": "ecs-unavailable",
                    "title": "ECS service has desired tasks but no healthy running capacity",
                    "resource_id": "service-b",
                    "probable_cause": "service-b is unhealthy",
                    "recommended_action": "fix image",
                },
            ],
        }
        dependency_graph = {
            "nodes": [
                {"id": "service-a", "name": "service-a", "type": "ecs-service"},
                {"id": "service-b", "name": "service-b", "type": "ecs-service"},
                {"id": "task-def-1", "name": "task-def-1", "type": "ecs-task-definition"},
            ],
            "edges": [
                {"from": "service-a", "to": "task-def-1", "relationship": "uses-task-definition"},
                {"from": "service-b", "to": "task-def-1", "relationship": "uses-task-definition"},
            ],
        }

        report = build_why_is_it_slow_report(performance_report, dependency_graph=dependency_graph)
        markdown = build_why_is_it_slow_markdown(report)

        self.assertEqual(len(report["top_incidents"]), 1)
        self.assertEqual(report["incident_count"], 1)
        self.assertEqual(report["top_incidents"][0]["finding_count"], 2)
        self.assertEqual(len(report["top_incidents"][0]["affected_resources"]), 2)
        self.assertIn("2 related findings", markdown)
        self.assertIn("Business impact", markdown)

    def test_why_is_it_slow_report_groups_disabled_lambda_path_findings(self):
        performance_report = {
            "generated_at": "2026-03-21T00:00:00Z",
            "source_env": "legacy",
            "region": "us-east-1",
            "account_id": "123",
            "root_cause_summary": "Most likely slowdown drivers: an event-driven Lambda consumer is broken.",
            "summary": {"finding_count": 2},
            "findings": [
                {
                    "severity": "medium",
                    "category": "disabled-processing-path",
                    "title": "A disabled event source mapping may leave async work unprocessed",
                    "resource_id": "mapping-a",
                    "probable_cause": "Messages may not flow",
                    "recommended_action": "enable mapping",
                },
                {
                    "severity": "medium",
                    "category": "disabled-processing-path",
                    "title": "A disabled event source mapping may leave async work unprocessed",
                    "resource_id": "mapping-b",
                    "probable_cause": "Messages may not flow",
                    "recommended_action": "enable mapping",
                },
            ],
        }
        dependency_graph = {
            "nodes": [
                {"id": "queue-a", "name": "queue-a", "type": "sqs"},
                {"id": "mapping-a", "name": "mapping-a", "type": "lambda-event-source-mapping"},
                {"id": "mapping-b", "name": "mapping-b", "type": "lambda-event-source-mapping"},
                {"id": "lambda-a", "name": "lambda-a", "type": "lambda"},
            ],
            "edges": [
                {"from": "queue-a", "to": "mapping-a", "relationship": "feeds-mapping"},
                {"from": "mapping-a", "to": "lambda-a", "relationship": "invokes-function"},
                {"from": "queue-a", "to": "mapping-b", "relationship": "feeds-mapping"},
                {"from": "mapping-b", "to": "lambda-a", "relationship": "invokes-function"},
            ],
        }

        report = build_why_is_it_slow_report(performance_report, dependency_graph=dependency_graph)

        self.assertEqual(len(report["top_incidents"]), 1)
        self.assertEqual(report["top_incidents"][0]["finding_count"], 2)
        self.assertEqual(len(report["top_incidents"][0]["affected_resources"]), 2)

    def test_analyze_performance_uses_live_metrics_signals(self):
        snapshot = {
            "source_env": "legacy",
            "region": "us-east-1",
            "account_id": "123",
            "lambda_functions": [{"FunctionArn": "arn:lambda:fn", "FunctionName": "worker", "MemorySize": 256, "Timeout": 10}],
            "lambda_event_source_mappings": [],
            "sqs_queues": [{"QueueName": "orders", "QueueUrl": "https://example", "Attributes": {"QueueArn": "arn:sqs:orders"}}],
            "dynamodb_tables": [{"Table": {"TableName": "orders", "TableArn": "arn:dynamodb:orders"}}],
            "rds": {"instances": [{"DBInstanceIdentifier": "orders-db"}]},
            "load_balancers": [{"LoadBalancerName": "orders-alb", "LoadBalancerArn": "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/orders-alb/abc"}],
            "api_gateways": [{"name": "orders-api", "stages": [{"stageName": "prod"}]}],
            "ecs": {
                "services": [
                    {
                        "serviceName": "api",
                        "serviceArn": "arn:ecs:svc",
                        "desiredCount": 2,
                        "runningCount": 2,
                        "events": [],
                        "networkConfiguration": {"awsvpcConfiguration": {"assignPublicIp": "DISABLED", "subnets": ["a"]}},
                        "taskDefinition": "td-1",
                        "clusterArn": "arn:aws:ecs:us-east-1:123:cluster/main",
                    }
                ],
                "task_definitions": [{"taskDefinitionArn": "td-1", "cpu": "1024", "memory": "2048"}],
            },
            "ecs_metrics": {"services": []},
        }
        live_metrics = {
            "lambda": {"worker": {"duration": {"Maximum": 9500}, "errors": {"Sum": 2}, "throttles": {"Sum": 1}, "invocations": {"Sum": 10}}},
            "ecs": {"api": {"cpu": {"Maximum": 92}, "memory": {"Maximum": 87}}},
            "sqs": {"orders": {"age_of_oldest": {"Maximum": 600}}},
            "api_gateway": {"orders-api:prod": {"latency": {"Maximum": 3500}, "integration_latency": {"Maximum": 3200}, "server_errors": {"Sum": 3}}},
            "dynamodb": {"orders": {"throttled_requests": {"Sum": 5}, "successful_request_latency": {"Maximum": 80}}},
            "rds": {"orders-db": {"cpu": {"Maximum": 91}, "connections": {"Maximum": 120}, "read_latency": {"Maximum": 0.06}, "write_latency": {"Maximum": 0.03}}},
            "alb": {"orders-alb": {"target_response_time": {"Maximum": 2.5}, "http_5xx": {"Sum": 1}, "target_5xx": {"Sum": 0}}},
        }

        report = analyze_performance(snapshot, live_metrics=live_metrics)
        categories = {item["category"] for item in report["findings"]}

        self.assertIn("lambda-near-timeout", categories)
        self.assertIn("lambda-throttling", categories)
        self.assertIn("lambda-errors", categories)
        self.assertIn("ecs-saturation", categories)
        self.assertIn("queue-latency", categories)
        self.assertIn("api-gateway-latency", categories)
        self.assertIn("dynamodb-throttling", categories)
        self.assertIn("rds-cpu-saturation", categories)
        self.assertIn("alb-latency", categories)
        self.assertTrue(report["root_cause_summary"])


if __name__ == "__main__":
    unittest.main()
