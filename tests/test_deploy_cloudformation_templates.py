import json
import tempfile
import unittest
from pathlib import Path

from executor.scripts.deploy_cloudformation_templates import (
    build_parameter_overrides,
    classify_existing_resource_conflict,
    recent_stack_failure_reason,
    target_stack_name,
)


class DeployCloudFormationTemplatesTests(unittest.TestCase):

    def test_target_stack_name_prefixes_env(self):
        self.assertEqual(
            target_stack_name("Infra-ECS-Cluster-carefree-crocodile", "roma-art"),
            "roma-art-Infra-ECS-Cluster-carefree-crocodile",
        )

    def test_build_parameter_overrides_uses_deployment_mappings(self):
        template = {
            "Parameters": {
                "ECSClusterName": {"Type": "String", "Default": "carefree-crocodile-bf3zi5"},
                "VpcId": {"Type": "String", "Default": ""},
                "SubnetIds": {"Type": "CommaDelimitedList", "Default": ""},
                "SecurityGroupIds": {"Type": "CommaDelimitedList", "Default": ""},
                "QueueName": {"Type": "String", "Default": "agent-error-queue"},
                "TableName": {"Type": "String", "Default": "agent_errors"},
                "RoleName": {"Type": "String", "Default": "task-dispatcher-role"},
                "FunctionName": {"Type": "String", "Default": "task-dispatcher"},
                "SecretName": {"Type": "String", "Default": "hippa-signer/github/pat"},
            }
        }
        deployment_manifest = {
            "resource_mappings": {
                "ecs_cluster_names": {"carefree-crocodile-bf3zi5": "roma-art-carefree-crocodile-bf3zi5"},
                "vpc_ids": {"vpc-old": "vpc-new"},
                "subnet_ids": {"subnet-a": "subnet-1", "subnet-b": "subnet-2"},
                "security_group_ids": {"sg-old": "sg-new"},
                "queue_names": {"agent-error-queue": "roma-art-agent-error-queue"},
                "dynamodb_table_names": {"agent_errors": "roma-art-agent_errors"},
                "role_names": {"task-dispatcher-role": "roma-art-task-dispatcher-role"},
                "function_names": {"task-dispatcher": "roma-art-task-dispatcher"},
                "secret_names": {"hippa-signer/github/pat": "roma-art-hippa-signer/github/pat"},
            }
        }

        overrides = build_parameter_overrides(json.dumps(template), deployment_manifest, "roma-art")

        self.assertIn({"ParameterKey": "ECSClusterName", "ParameterValue": "roma-art-carefree-crocodile-bf3zi5"}, overrides)
        self.assertIn({"ParameterKey": "VpcId", "ParameterValue": "vpc-new"}, overrides)
        self.assertIn({"ParameterKey": "SubnetIds", "ParameterValue": "subnet-1,subnet-2"}, overrides)
        self.assertIn({"ParameterKey": "SecurityGroupIds", "ParameterValue": "sg-new"}, overrides)
        self.assertIn({"ParameterKey": "QueueName", "ParameterValue": "roma-art-agent-error-queue"}, overrides)
        self.assertIn({"ParameterKey": "TableName", "ParameterValue": "roma-art-agent_errors"}, overrides)
        self.assertIn({"ParameterKey": "RoleName", "ParameterValue": "roma-art-task-dispatcher-role"}, overrides)
        self.assertIn({"ParameterKey": "FunctionName", "ParameterValue": "roma-art-task-dispatcher"}, overrides)
        self.assertIn({"ParameterKey": "SecretName", "ParameterValue": "roma-art-hippa-signer/github/pat"}, overrides)

    def test_recent_stack_failure_reason_reads_first_reason(self):
        class FakeCf:
            def describe_stack_events(self, StackName):
                return {"StackEvents": [{"ResourceStatusReason": "already exists"}]}

        self.assertEqual(recent_stack_failure_reason(FakeCf(), "stack"), "already exists")

    def test_classify_existing_resource_conflict_marks_import_required_for_ecs_cluster(self):
        template = {
            "Parameters": {
                "ECSClusterName": {"Type": "String", "Default": "legacy-cluster"},
            },
            "Resources": {
                "ECSCluster": {
                    "Type": "AWS::ECS::Cluster",
                    "Properties": {"ClusterName": {"Ref": "ECSClusterName"}},
                }
            },
        }
        deployment_manifest = {
            "resource_mappings": {
                "ecs_cluster_names": {"legacy-cluster": "roma-art-legacy-cluster"},
            }
        }
        parameters = [{"ParameterKey": "ECSClusterName", "ParameterValue": "roma-art-legacy-cluster"}]

        result = classify_existing_resource_conflict(json.dumps(template), parameters, deployment_manifest)

        self.assertTrue(result["import_required"])
        self.assertIn("already exists", result["reason"])

    def test_classify_existing_resource_conflict_supports_queue_and_lambda(self):
        template = {
            "Parameters": {
                "QueueName": {"Type": "String", "Default": "legacy-queue"},
                "FunctionName": {"Type": "String", "Default": "legacy-fn"},
            },
            "Resources": {
                "Queue": {
                    "Type": "AWS::SQS::Queue",
                    "Properties": {"QueueName": {"Ref": "QueueName"}},
                },
                "Function": {
                    "Type": "AWS::Lambda::Function",
                    "Properties": {"FunctionName": {"Ref": "FunctionName"}},
                },
            },
        }
        deployment_manifest = {
            "resource_mappings": {
                "queue_names": {"legacy-queue": "roma-art-legacy-queue"},
                "function_names": {"legacy-fn": "roma-art-legacy-fn"},
            }
        }

        queue_conflict = classify_existing_resource_conflict(
            json.dumps({"Resources": {"Queue": template["Resources"]["Queue"]}}),
            [{"ParameterKey": "QueueName", "ParameterValue": "roma-art-legacy-queue"}],
            deployment_manifest,
        )
        lambda_conflict = classify_existing_resource_conflict(
            json.dumps({"Resources": {"Function": template["Resources"]["Function"]}}),
            [{"ParameterKey": "FunctionName", "ParameterValue": "roma-art-legacy-fn"}],
            deployment_manifest,
        )

        self.assertTrue(queue_conflict["import_required"])
        self.assertIn("SQS queue", queue_conflict["reason"])
        self.assertTrue(lambda_conflict["import_required"])
        self.assertIn("Lambda function", lambda_conflict["reason"])


if __name__ == "__main__":
    unittest.main()
