import unittest

from executor.scripts.discover_aws_environment import build_dependency_graph, _discover_git_repositories, _list_ec2_instances, _list_ecs, _list_s3_buckets, sanitize_snapshot_value
from executor.scripts.scan_environment_risks import resolve_inventory_dir
from executor.scripts.scan_environment_risks import analyze_snapshot


class DiscoverAwsEnvironmentTests(unittest.TestCase):

    def test_resolve_inventory_dir_prefers_regional_inventory_with_signals(self):
        direct = "state/aws_inventory/full-account-scan"
        regional = "state/aws_inventory/full-account-scan-us-east-1"
        resolved = resolve_inventory_dir("full-account-scan", "us-east-1")
        self.assertIn(str(resolved).replace("\\", "/"), {direct, regional})

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
            "codebuild_projects": [],
            "s3_buckets": [],
            "vpcs": [],
            "subnets": [],
            "security_groups": [],
            "rds": {"instances": []},
            "git_repositories": [],
            "ecs": {"clusters": [], "services": [], "task_definitions": []},
            "ecs_scheduled_tasks": [],
            "secrets": [],
            "dynamodb_tables": [
                {
                    "Table": {
                        "TableName": "legacy-table",
                        "TableArn": "arn:aws:dynamodb:us-east-1:123456789012:table/legacy-table",
                    }
                }
            ],
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
                            "QUEUE_ARN": "arn:aws:sqs:us-east-1:123456789012:legacy-events",
                            "TABLE_ARN": "arn:aws:dynamodb:us-east-1:123456789012:table/legacy-table",
                        }
                    },
                }
            ],
        }

        graph = build_dependency_graph(snapshot)

        self.assertTrue(any(edge["relationship"] == "assumes-role" for edge in graph["edges"]))
        self.assertTrue(any(edge["relationship"] == "event-source-mapping" for edge in graph["edges"]))
        self.assertTrue(any(node["type"] == "lambda-event-source-mapping" for node in graph["nodes"]))
        self.assertTrue(any(edge["relationship"] == "invokes-function" for edge in graph["edges"]))
        self.assertTrue(any(edge["relationship"] == "feeds-mapping" for edge in graph["edges"]))
        self.assertTrue(any(edge["relationship"] == "env:QUEUE_ARN" for edge in graph["edges"]))
        self.assertTrue(any(edge["relationship"] == "env:TABLE_ARN" for edge in graph["edges"]))

    def test_build_dependency_graph_includes_secret_references(self):
        snapshot = {
            "sqs_queues": [],
            "iam_roles": [],
            "sns_topics": [],
            "s3_buckets": [],
            "ec2_instances": [],
            "vpcs": [],
            "subnets": [],
            "security_groups": [],
            "rds": {"instances": []},
            "git_repositories": [],
            "ecs": {"clusters": [], "services": [], "task_definitions": []},
            "ecs_scheduled_tasks": [],
            "codebuild_projects": [
                {
                    "name": "legacy-build",
                    "arn": "arn:aws:codebuild:us-east-1:123456789012:project/legacy-build",
                }
            ],
            "secrets": [
                {
                    "ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:legacy-db",
                    "Name": "legacy-db",
                }
            ],
            "dynamodb_tables": [],
            "api_gateways": [],
            "lambda_event_source_mappings": [],
            "lambda_permissions": [],
            "lambda_functions": [
                {
                    "FunctionName": "legacy-worker",
                    "FunctionArn": "arn:aws:lambda:us-east-1:123456789012:function:legacy-worker",
                    "Environment": {
                        "Variables": {
                            "SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:legacy-db"
                        }
                    },
                }
            ],
        }

        graph = build_dependency_graph(snapshot)

        self.assertTrue(any(node["type"] == "secret" for node in graph["nodes"]))
        self.assertTrue(any(node["type"] == "codebuild-project" for node in graph["nodes"]))
        self.assertTrue(any(edge["relationship"] == "env:SECRET_ARN" for edge in graph["edges"]))

    def test_build_dependency_graph_includes_ecs_service_and_task_definition_links(self):
        snapshot = {
            "sqs_queues": [],
            "iam_roles": [
                {"RoleName": "task-role", "Arn": "arn:aws:iam::123456789012:role/task-role"},
                {"RoleName": "execution-role", "Arn": "arn:aws:iam::123456789012:role/execution-role"},
            ],
            "sns_topics": [],
            "api_gateways": [],
            "codebuild_projects": [],
            "s3_buckets": [],
            "ec2_instances": [],
            "vpcs": [],
            "subnets": [{"SubnetId": "subnet-a", "VpcId": "vpc-a"}],
            "security_groups": [{"GroupId": "sg-a", "GroupName": "app-sg", "VpcId": "vpc-a"}],
            "rds": {"instances": []},
            "git_repositories": [],
            "secrets": [{"ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:db", "Name": "db"}],
            "dynamodb_tables": [
                {"Table": {"TableName": "orders", "TableArn": "arn:aws:dynamodb:us-east-1:123456789012:table/orders"}}
            ],
            "lambda_event_source_mappings": [],
            "lambda_permissions": [],
            "lambda_functions": [],
            "ecs_scheduled_tasks": [],
            "ecs": {
                "clusters": [{"clusterArn": "arn:aws:ecs:us-east-1:123456789012:cluster/main", "clusterName": "main"}],
                "services": [
                    {
                        "serviceArn": "arn:aws:ecs:us-east-1:123456789012:service/main/api",
                        "serviceName": "api",
                        "clusterArn": "arn:aws:ecs:us-east-1:123456789012:cluster/main",
                        "taskDefinition": "arn:aws:ecs:us-east-1:123456789012:task-definition/api:1",
                        "networkConfiguration": {
                            "awsvpcConfiguration": {
                                "subnets": ["subnet-a"],
                                "securityGroups": ["sg-a"],
                            }
                        },
                    }
                ],
                "task_definitions": [
                    {
                        "taskDefinitionArn": "arn:aws:ecs:us-east-1:123456789012:task-definition/api:1",
                        "family": "api",
                        "taskRoleArn": "arn:aws:iam::123456789012:role/task-role",
                        "executionRoleArn": "arn:aws:iam::123456789012:role/execution-role",
                        "containerDefinitions": [
                            {
                                "name": "api",
                                "environment": [
                                    {"name": "TABLE_ARN", "value": "arn:aws:dynamodb:us-east-1:123456789012:table/orders"}
                                ],
                                "secrets": [
                                    {"name": "DB_SECRET", "valueFrom": "arn:aws:secretsmanager:us-east-1:123456789012:secret:db"}
                                ],
                            }
                        ],
                    }
                ],
            },
        }

        graph = build_dependency_graph(snapshot)

        self.assertTrue(any(node["type"] == "ecs-service" for node in graph["nodes"]))
        self.assertTrue(any(node["type"] == "ecs-task-definition" for node in graph["nodes"]))
        self.assertTrue(any(edge["relationship"] == "runs-in-cluster" for edge in graph["edges"]))
        self.assertTrue(any(edge["relationship"] == "uses-task-definition" for edge in graph["edges"]))
        self.assertTrue(any(edge["relationship"] == "env:TABLE_ARN" for edge in graph["edges"]))
        self.assertTrue(any(edge["relationship"] == "secret:api" for edge in graph["edges"]))

    def test_discover_git_repositories_finds_lambda_and_codebuild_urls(self):
        snapshot = {
            "lambda_functions": [
                {
                    "FunctionName": "legacy-worker",
                    "Environment": {
                        "Variables": {
                            "REPO_URL": "https://github.com/example/legacy-service.git",
                        }
                    },
                }
            ],
            "codebuild_projects": [
                {
                    "name": "legacy-build",
                    "source": {"location": "https://github.com/example/platform-infra.git"},
                    "environment": {"environmentVariables": []},
                }
            ],
        }

        repositories = _discover_git_repositories(snapshot)

        self.assertEqual(len(repositories), 2)
        self.assertTrue(any(repo["name"] == "legacy-service" for repo in repositories))
        self.assertTrue(any(repo["name"] == "platform-infra" for repo in repositories))

    def test_analyze_snapshot_flags_public_rds_and_security_group(self):
        snapshot = {
            "source_env": "legacy",
            "account_id": "123456789012",
            "region": "us-east-1",
            "lambda_functions": [],
            "iam_roles": [],
            "security_groups": [
                {
                    "GroupId": "sg-123",
                    "GroupName": "public-sg",
                    "IpPermissions": [
                        {"FromPort": 5432, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
                    ],
                }
            ],
            "route_tables": [],
            "rds": {
                "instances": [
                    {
                        "DBInstanceIdentifier": "legacy-db",
                        "PubliclyAccessible": True,
                        "StorageEncrypted": False,
                        "BackupRetentionPeriod": 0,
                    }
                ],
                "clusters": [],
            },
            "cloudformation_stacks": [],
            "s3_buckets": [],
            "git_repositories": [],
        }

        report = analyze_snapshot(snapshot)

        self.assertGreaterEqual(report["summary"]["high"], 2)
        self.assertTrue(any(item["category"] == "network-exposure" for item in report["findings"]))
        self.assertTrue(any(item["category"] == "rds-exposure" for item in report["findings"]))

    def test_sanitize_snapshot_value_redacts_sensitive_fields(self):
        snapshot = {
            "AccessKeyId": "AKIAEXAMPLE",
            "nested": {
                "CloudTrailEvent": "{\"event\":\"value\"}",
                "safe": "ok",
            },
            "items": [
                {"SessionToken": "token"},
                {"value": "ok"},
            ],
        }

        sanitized = sanitize_snapshot_value(snapshot)

        self.assertEqual(sanitized["AccessKeyId"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["CloudTrailEvent"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["safe"], "ok")
        self.assertEqual(sanitized["items"][0]["SessionToken"], "[REDACTED]")
        self.assertEqual(sanitized["items"][1]["value"], "ok")

    def test_list_ecs_includes_services_for_matched_cluster(self):
        class FakeEcs:
            def list_clusters(self):
                return {"clusterArns": ["arn:aws:ecs:us-east-1:123:cluster/full-account-scan-a"]}

            def describe_clusters(self, clusters):
                return {"clusters": [{"clusterArn": clusters[0], "clusterName": "full-account-scan-a"}]}

            def list_services(self, cluster):
                return {"serviceArns": ["arn:aws:ecs:us-east-1:123:service/full-account-scan-a/service-prod"]}

            def describe_services(self, cluster, services):
                return {"services": [{"serviceName": "service-prod", "taskDefinition": "td-1"}]}

            def list_tasks(self, cluster, serviceName):
                return {"taskArns": []}

            def describe_task_definition(self, taskDefinition):
                return {"taskDefinition": {"taskDefinitionArn": "td-1"}}

        ecs = _list_ecs(FakeEcs(), "full-account-scan", {})

        self.assertEqual(len(ecs["clusters"]), 1)
        self.assertEqual(len(ecs["services"]), 1)
        self.assertEqual(len(ecs["task_definitions"]), 1)

    def test_list_s3_buckets_includes_bucket_configuration(self):
        class FakeS3:
            def list_buckets(self):
                return {"Buckets": [{"Name": "legacy-bucket"}]}

            def get_bucket_location(self, Bucket):
                return {"LocationConstraint": "us-east-1"}

            def get_bucket_tagging(self, Bucket):
                return {"TagSet": [{"Key": "team", "Value": "platform"}]}

            def get_bucket_versioning(self, Bucket):
                return {"Status": "Enabled"}

            def get_bucket_encryption(self, Bucket):
                return {"ServerSideEncryptionConfiguration": {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}}

            def get_bucket_lifecycle_configuration(self, Bucket):
                return {"Rules": [{"ID": "expire-old", "Status": "Enabled"}]}

            def get_bucket_cors(self, Bucket):
                return {"CORSRules": [{"AllowedMethods": ["GET"], "AllowedOrigins": ["*"]}]}

            def get_bucket_policy(self, Bucket):
                return {"Policy": "{\"Version\":\"2012-10-17\"}"}

            def get_bucket_notification_configuration(self, Bucket):
                return {
                    "QueueConfigurations": [
                        {
                            "Id": "queue-events",
                            "QueueArn": "arn:aws:sqs:us-east-1:123:legacy-events",
                            "Events": ["s3:ObjectCreated:*"],
                        }
                    ]
                }

        buckets = _list_s3_buckets(FakeS3(), "us-east-1", "legacy", {})

        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0]["Versioning"]["Status"], "Enabled")
        self.assertTrue(buckets[0]["BucketEncryption"]["Rules"])
        self.assertTrue(buckets[0]["LifecycleRules"])
        self.assertTrue(buckets[0]["CorsRules"])
        self.assertIn("Version", buckets[0]["Policy"])
        self.assertEqual(buckets[0]["NotificationConfiguration"]["QueueConfigurations"][0]["Id"], "queue-events")

    def test_list_ec2_instances_matches_name_or_id(self):
        class FakeEc2:
            def describe_instances(self):
                return {
                    "Reservations": [
                        {
                            "Instances": [
                                {"InstanceId": "i-123", "Tags": [{"Key": "Name", "Value": "legacy-app"}]},
                                {"InstanceId": "i-999", "Tags": [{"Key": "Name", "Value": "other-app"}]},
                            ]
                        }
                    ]
                }

        instances = _list_ec2_instances(FakeEc2(), "legacy", {})

        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]["InstanceId"], "i-123")


if __name__ == "__main__":
    unittest.main()
