import unittest
from pathlib import Path
import shutil

from executor.scripts.agent_memory import record_incident
from executor.scripts.validate_deployed_env import (
    attach_known_fixes_to_checks,
    api_gateway_parity_checks,
    build_smoke_checks,
    codebuild_smoke_checks,
    expected_mapping_count,
    kms_smoke_checks,
    lambda_log_errors,
    s3_parity_checks,
)


class ValidateDeployedEnvTests(unittest.TestCase):

    def setUp(self):
        self.base_dir = Path("state") / "test_validate_known_fixes"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.base_dir, ignore_errors=True)

    def test_expected_mapping_count_uses_manifest(self):
        manifest = {"lambda_event_source_mappings": [{}, {}, {}]}
        self.assertEqual(expected_mapping_count(manifest), 3)

    def test_build_smoke_checks_reports_ok_for_matching_state(self):
        class FakeLambdaClient:
            def get_function_configuration(self, FunctionName):
                return {"State": "Active", "LastUpdateStatus": "Successful"}

            def invoke(self, FunctionName, InvocationType):
                return {"StatusCode": 204}

            def get_paginator(self, _name):
                class Paginator:
                    def paginate(self_inner):
                        return [{
                            "EventSourceMappings": [
                                {"FunctionArn": "arn:aws:lambda:us-east-2:123:function:fn-a", "State": "Enabled"},
                                {"FunctionArn": "arn:aws:lambda:us-east-2:123:function:fn-b", "State": "Enabled"},
                            ]
                        }]
                return Paginator()

        class FakeSqsClient:
            def get_queue_attributes(self, QueueUrl, AttributeNames):
                return {"Attributes": {"QueueArn": "arn"}}

        class FakeEcsClient:
            def describe_clusters(self, clusters):
                return {"clusters": [{"clusterName": item} for item in clusters]}

            def describe_services(self, cluster, services):
                return {
                    "services": [
                        {"serviceName": item, "status": "ACTIVE", "desiredCount": 1, "runningCount": 1}
                        for item in services
                    ]
                }

        class FakeApiGwClient:
            def get_rest_api(self, restApiId):
                return {"id": restApiId}

            def get_stages(self, restApiId):
                return {"item": [{"stageName": "prod"}]}

        manifest = {
            "lambda_functions": [{"target_function": "fn-a"}, {"target_function": "fn-b"}],
            "lambda_event_source_mappings": [
                {"source_uuid": "m-a", "target_function": "fn-a", "target_event_source_arn": "arn:a"},
                {"source_uuid": "m-b", "target_function": "fn-b", "target_event_source_arn": "arn:b"},
            ],
            "sqs_queues": [{"target_queue": "queue-a", "target_queue_url": "https://example.com/q"}],
            "ecs_clusters": [{"target_cluster": "cluster-a"}],
            "ecs_services": [{"source_service": "svc-a", "target_cluster_arn": "cluster-a", "target_service": "svc-a"}],
            "api_gateways": [{"target_api": "api-a", "target_api_id": "api-1"}],
        }
        source_snapshot = {
            "lambda_event_source_mappings": [
                {"UUID": "m-a", "State": "Enabled"},
                {"UUID": "m-b", "State": "Enabled"},
            ],
            "ecs": {"services": [{"serviceName": "svc-a", "status": "ACTIVE", "desiredCount": 1, "runningCount": 1}]},
        }

        checks = build_smoke_checks(manifest, FakeLambdaClient(), FakeSqsClient(), FakeEcsClient(), source_snapshot=source_snapshot, apigw_client=FakeApiGwClient())
        self.assertTrue(all(item["status"] == "ok" for item in checks))

    def test_build_smoke_checks_reports_ecs_service_issue(self):
        class FakeLambdaClient:
            def get_function_configuration(self, FunctionName):
                return {"State": "Active", "LastUpdateStatus": "Successful"}

            def invoke(self, FunctionName, InvocationType):
                return {"StatusCode": 204}

            def get_paginator(self, _name):
                class Paginator:
                    def paginate(self_inner):
                        return [{"EventSourceMappings": []}]
                return Paginator()

        class FakeSqsClient:
            def get_queue_attributes(self, QueueUrl, AttributeNames):
                return {"Attributes": {"QueueArn": "arn"}}

        class FakeEcsClient:
            def describe_clusters(self, clusters):
                return {"clusters": [{"clusterName": item} for item in clusters]}

            def describe_services(self, cluster, services):
                return {
                    "services": [
                        {"serviceName": services[0], "status": "ACTIVE", "desiredCount": 2, "runningCount": 1}
                    ]
                }

        manifest = {
            "lambda_functions": [],
            "lambda_event_source_mappings": [],
            "sqs_queues": [],
            "ecs_clusters": [{"target_cluster": "cluster-a"}],
            "ecs_services": [{"source_service": "svc-a", "target_cluster_arn": "cluster-a", "target_service": "svc-a"}],
        }
        source_snapshot = {"ecs": {"services": [{"serviceName": "svc-a", "status": "ACTIVE", "desiredCount": 2, "runningCount": 2}]}}

        checks = build_smoke_checks(manifest, FakeLambdaClient(), FakeSqsClient(), FakeEcsClient(), source_snapshot=source_snapshot)
        service_check = next(item for item in checks if item["name"] == "ecs-services-steady")
        self.assertEqual(service_check["status"], "issue")

    def test_build_smoke_checks_allows_disabled_mapping_when_source_is_disabled(self):
        class FakeLambdaClient:
            def get_function_configuration(self, FunctionName):
                return {"State": "Active", "LastUpdateStatus": "Successful"}

            def invoke(self, FunctionName, InvocationType):
                return {"StatusCode": 204}

            def get_paginator(self, _name):
                class Paginator:
                    def paginate(self_inner):
                        return [{"EventSourceMappings": [{
                            "UUID": "m-1",
                            "FunctionArn": "arn:aws:lambda:us-east-2:123:function:fn-a",
                            "EventSourceArn": "arn:q",
                            "State": "Disabled",
                            "StateTransitionReason": "USER_INITIATED",
                        }]}]
                return Paginator()

        class FakeSqsClient:
            def get_queue_attributes(self, QueueUrl, AttributeNames):
                return {"Attributes": {"QueueArn": "arn"}}

        class FakeEcsClient:
            def describe_clusters(self, clusters):
                return {"clusters": []}

            def describe_services(self, cluster, services):
                return {"services": []}

        manifest = {
            "lambda_functions": [{"target_function": "fn-a"}],
            "lambda_event_source_mappings": [{"source_uuid": "m-1", "target_function": "fn-a", "target_event_source_arn": "arn:q"}],
            "sqs_queues": [],
            "ecs_clusters": [],
            "ecs_services": [],
        }
        source_snapshot = {"lambda_event_source_mappings": [{"UUID": "m-1", "State": "Disabled"}]}

        checks = build_smoke_checks(manifest, FakeLambdaClient(), FakeSqsClient(), FakeEcsClient(), source_snapshot=source_snapshot)
        mapping_check = next(item for item in checks if item["name"] == "lambda-event-source-mappings-enabled")
        self.assertEqual(mapping_check["status"], "ok")

    def test_attach_known_fixes_to_checks_adds_suggestions_for_matching_issue(self):
        record_incident(
            "validation-smoke-check-issue",
            "ecs service failed because awslogs region stayed in source region",
            client_slug="test-validate-known-fixes",
            tags=["ecs", "validation"],
            resolution="rewrite awslogs-region to target region",
            validated=True,
        )
        checks = [{
            "name": "ecs-services-steady",
            "status": "issue",
            "issues": [{"service": "svc-a", "error": "running=0 desired=1 because awslogs region stayed in source region"}],
        }]
        updated = attach_known_fixes_to_checks(checks, client_slug="test-validate-known-fixes")
        self.assertIn("known_fixes", updated[0])
        self.assertGreaterEqual(len(updated[0]["known_fixes"]), 1)

    def test_build_smoke_checks_allows_unsteady_ecs_service_when_source_is_unsteady(self):
        class FakeLambdaClient:
            def get_function_configuration(self, FunctionName):
                return {"State": "Active", "LastUpdateStatus": "Successful"}

            def invoke(self, FunctionName, InvocationType):
                return {"StatusCode": 204}

            def get_paginator(self, _name):
                class Paginator:
                    def paginate(self_inner):
                        return [{"EventSourceMappings": []}]
                return Paginator()

        class FakeSqsClient:
            def get_queue_attributes(self, QueueUrl, AttributeNames):
                return {"Attributes": {"QueueArn": "arn"}}

        class FakeEcsClient:
            def describe_clusters(self, clusters):
                return {"clusters": [{"clusterName": item} for item in clusters]}

            def describe_services(self, cluster, services):
                return {"services": [{"serviceName": services[0], "status": "ACTIVE", "desiredCount": 1, "runningCount": 0}]}

        manifest = {
            "lambda_functions": [],
            "lambda_event_source_mappings": [],
            "sqs_queues": [],
            "ecs_clusters": [{"target_cluster": "cluster-a"}],
            "ecs_services": [{"source_service": "svc-a", "target_cluster_arn": "cluster-a", "target_service": "svc-a"}],
        }
        source_snapshot = {"ecs": {"services": [{"serviceName": "svc-a", "status": "ACTIVE", "desiredCount": 1, "runningCount": 0}]}}

        checks = build_smoke_checks(manifest, FakeLambdaClient(), FakeSqsClient(), FakeEcsClient(), source_snapshot=source_snapshot)
        service_check = next(item for item in checks if item["name"] == "ecs-services-steady")
        self.assertEqual(service_check["status"], "ok")

    def test_build_smoke_checks_reports_lambda_and_mapping_health_issues(self):
        class FakeLambdaClient:
            def get_function_configuration(self, FunctionName):
                if FunctionName == "fn-bad":
                    return {"State": "Pending", "LastUpdateStatus": "InProgress"}
                return {"State": "Active", "LastUpdateStatus": "Successful"}

            def invoke(self, FunctionName, InvocationType):
                raise RuntimeError("invoke failed")

            def get_paginator(self, _name):
                class Paginator:
                    def paginate(self_inner):
                        return [{
                            "EventSourceMappings": [
                                {
                                    "UUID": "m-1",
                                    "FunctionArn": "arn:aws:lambda:us-east-2:123:function:fn-bad",
                                    "State": "Creating",
                                    "StateTransitionReason": "User action",
                                    "LastProcessingResult": "PROBLEM",
                                }
                            ]
                        }]
                return Paginator()

        class FakeSqsClient:
            def get_queue_attributes(self, QueueUrl, AttributeNames):
                return {"Attributes": {"QueueArn": "arn"}}

        class FakeEcsClient:
            def describe_clusters(self, clusters):
                return {"clusters": []}

            def describe_services(self, cluster, services):
                return {"services": []}

        class FakeApiGwClient:
            def get_rest_api(self, restApiId):
                return {"id": restApiId}

            def get_stages(self, restApiId):
                return {"item": []}

        manifest = {
            "lambda_functions": [{"target_function": "fn-bad"}],
            "lambda_event_source_mappings": [{}],
            "sqs_queues": [],
            "ecs_clusters": [],
            "ecs_services": [],
            "api_gateways": [{"target_api": "api-a", "target_api_id": "api-1"}],
        }

        checks = build_smoke_checks(manifest, FakeLambdaClient(), FakeSqsClient(), FakeEcsClient(), apigw_client=FakeApiGwClient())
        lambda_check = next(item for item in checks if item["name"] == "lambda-functions-active")
        invoke_check = next(item for item in checks if item["name"] == "lambda-dry-run-invoke")
        mapping_check = next(item for item in checks if item["name"] == "lambda-event-source-mappings-enabled")
        api_stage_check = next(item for item in checks if item["name"] == "api-gateway-stages-present")
        self.assertEqual(lambda_check["status"], "issue")
        self.assertEqual(invoke_check["status"], "issue")
        self.assertEqual(mapping_check["status"], "issue")
        self.assertEqual(api_stage_check["status"], "issue")

    def test_codebuild_smoke_checks_reports_ok(self):
        class FakeCodeBuildClient:
            def batch_get_projects(self, names):
                return {
                    "projects": [
                        {
                            "name": item,
                            "serviceRole": "arn:aws:iam::123:role/codebuild",
                            "environment": {
                                "type": "LINUX_CONTAINER",
                                "image": "aws/codebuild/standard:7.0",
                                "computeType": "BUILD_GENERAL1_SMALL",
                            },
                            "source": {"type": "S3"},
                        }
                        for item in names
                    ]
                }

        manifest = {"codebuild_projects": [{"target_project": "build-a"}]}
        check = codebuild_smoke_checks(manifest, FakeCodeBuildClient())
        self.assertEqual(check["status"], "ok")

    def test_codebuild_smoke_checks_reports_readiness_issue(self):
        class FakeCodeBuildClient:
            def batch_get_projects(self, names):
                return {"projects": [{"name": names[0], "environment": {}, "source": {}}]}

        manifest = {"codebuild_projects": [{"target_project": "build-a"}]}
        check = codebuild_smoke_checks(manifest, FakeCodeBuildClient())
        self.assertEqual(check["status"], "issue")

    def test_kms_smoke_checks_reports_ok_for_mapped_keys(self):
        class FakeKmsClient:
            def describe_key(self, KeyId):
                return {"KeyMetadata": {"KeyId": KeyId}}

        snapshot = {
            "secrets": [{"Name": "secret-a", "KmsKeyId": "alias/source-key"}],
            "sqs_queues": [],
            "codebuild_projects": [{"name": "build-a", "encryptionKey": "arn:aws:kms:us-east-1:123:key/abc"}],
            "s3_buckets": [],
        }
        manifest = {
            "region": "us-east-2",
            "resource_mappings": {
                "kms_key_ids": {"arn:aws:kms:us-east-1:123:key/abc": "arn:aws:kms:us-east-2:123:key/xyz"},
                "kms_aliases": {"alias/source-key": "alias/target-key"},
            },
        }

        check = kms_smoke_checks(snapshot, manifest, FakeKmsClient())
        self.assertEqual(check["status"], "ok")

    def test_kms_smoke_checks_reports_missing_region_mapping(self):
        class FakeKmsClient:
            def describe_key(self, KeyId):
                return {"KeyMetadata": {"KeyId": KeyId}}

        snapshot = {
            "secrets": [],
            "sqs_queues": [],
            "codebuild_projects": [{"name": "build-a", "encryptionKey": "arn:aws:kms:us-east-1:123:key/abc"}],
            "s3_buckets": [],
        }
        manifest = {"region": "us-east-2", "resource_mappings": {"kms_key_ids": {}, "kms_aliases": {}}}

        check = kms_smoke_checks(snapshot, manifest, FakeKmsClient())
        self.assertEqual(check["status"], "issue")

    def test_kms_smoke_checks_accepts_aws_managed_alias_after_region_rewrite(self):
        class FakeKmsClient:
            def describe_key(self, KeyId):
                self.key_id = KeyId
                return {"KeyMetadata": {"KeyId": KeyId}}

        snapshot = {
            "secrets": [],
            "sqs_queues": [],
            "codebuild_projects": [{"name": "build-a", "encryptionKey": "arn:aws:kms:us-east-1:123:alias/aws/s3"}],
            "s3_buckets": [],
        }
        manifest = {
            "region": "us-east-2",
            "target_account_id": "123",
            "resource_mappings": {"kms_key_ids": {}, "kms_aliases": {}},
        }

        kms_client = FakeKmsClient()
        check = kms_smoke_checks(snapshot, manifest, kms_client)
        self.assertEqual(check["status"], "ok")
        self.assertEqual(kms_client.key_id, "arn:aws:kms:us-east-2:123:alias/aws/s3")

    def test_lambda_log_errors_ignores_missing_log_group(self):
        class FakeLogsClient:
            def filter_log_events(self, **kwargs):
                raise RuntimeError("ResourceNotFoundException: The specified log group does not exist.")

        self.assertEqual(lambda_log_errors(FakeLogsClient(), "fn-a"), [])

    def test_api_gateway_parity_checks_reports_ok(self):
        class FakeApiGwClient:
            def get_authorizers(self, restApiId, limit):
                return {"items": [{"id": "a1"}]}

            def get_request_validators(self, restApiId, limit):
                return {"items": [{"id": "v1"}]}

            def get_gateway_responses(self, restApiId, limit):
                return {"items": [{"responseType": "DEFAULT_4XX"}]}

            def get_usage_plans(self, limit):
                return {"items": [{"id": "plan-1", "apiStages": [{"apiId": "api-1", "stage": "prod"}]}]}

            def get_usage_plan_keys(self, usagePlanId, limit):
                return {"items": [{"id": "key-1"}]}

            def get_domain_names(self, limit):
                return {"items": [{"domainName": "api.example.com"}]}

            def get_base_path_mappings(self, domainName, limit):
                return {"items": [{"restApiId": "api-1", "basePath": "(none)"}]}

        source_snapshot = {
            "api_gateways": [
                {
                    "name": "legacy-api",
                    "authorizers": [{}],
                    "request_validators": [{}],
                    "gateway_responses": [{}],
                    "usage_plans": [{"apiKeys": [{}]}],
                    "domain_mappings": [{"mappings": [{}]}],
                }
            ]
        }
        manifest = {"api_gateways": [{"source_api": "legacy-api", "target_api": "virgin-api", "target_api_id": "api-1"}]}

        check = api_gateway_parity_checks(source_snapshot, manifest, FakeApiGwClient())
        self.assertEqual(check["status"], "ok")

    def test_api_gateway_parity_checks_reports_missing_extras(self):
        class FakeApiGwClient:
            def get_authorizers(self, restApiId, limit):
                return {"items": []}

            def get_request_validators(self, restApiId, limit):
                return {"items": []}

            def get_gateway_responses(self, restApiId, limit):
                return {"items": []}

            def get_usage_plans(self, limit):
                return {"items": []}

            def get_usage_plan_keys(self, usagePlanId, limit):
                return {"items": []}

            def get_domain_names(self, limit):
                return {"items": []}

            def get_base_path_mappings(self, domainName, limit):
                return {"items": []}

        source_snapshot = {
            "api_gateways": [
                {
                    "name": "legacy-api",
                    "authorizers": [{}],
                    "request_validators": [{}],
                    "gateway_responses": [{}],
                    "usage_plans": [{"apiKeys": [{}]}],
                    "domain_mappings": [{"mappings": [{}]}],
                }
            ]
        }
        manifest = {"api_gateways": [{"source_api": "legacy-api", "target_api": "virgin-api", "target_api_id": "api-1"}]}

        check = api_gateway_parity_checks(source_snapshot, manifest, FakeApiGwClient())
        self.assertEqual(check["status"], "issue")

    def test_s3_parity_checks_reports_ok(self):
        class FakeS3Client:
            def head_bucket(self, Bucket):
                return {}

            def get_bucket_tagging(self, Bucket):
                return {"TagSet": [{"Key": "team", "Value": "platform"}]}

            def get_bucket_versioning(self, Bucket):
                return {"Status": "Enabled"}

            def get_bucket_encryption(self, Bucket):
                return {"ServerSideEncryptionConfiguration": {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}}

            def get_bucket_lifecycle_configuration(self, Bucket):
                return {"Rules": [{"ID": "expire-old"}]}

            def get_bucket_cors(self, Bucket):
                return {"CORSRules": [{"AllowedMethods": ["GET"], "AllowedOrigins": ["*"]}]}

            def get_bucket_notification_configuration(self, Bucket):
                return {"QueueConfigurations": [{"Id": "queue-events"}]}

        source_snapshot = {
            "s3_buckets": [
                {
                    "Name": "bucket-a",
                    "Tags": [{"Key": "team", "Value": "platform"}],
                    "Versioning": {"Status": "Enabled"},
                    "BucketEncryption": {"Rules": [{}]},
                    "LifecycleRules": [{}],
                    "CorsRules": [{}],
                    "NotificationConfiguration": {"QueueConfigurations": [{}]},
                }
            ]
        }

        check = s3_parity_checks(source_snapshot, FakeS3Client())
        self.assertEqual(check["status"], "ok")

    def test_s3_parity_checks_reports_missing_config(self):
        class FakeS3Client:
            def head_bucket(self, Bucket):
                return {}

            def get_bucket_tagging(self, Bucket):
                return {"TagSet": []}

            def get_bucket_versioning(self, Bucket):
                return {}

            def get_bucket_encryption(self, Bucket):
                return {"ServerSideEncryptionConfiguration": {}}

            def get_bucket_lifecycle_configuration(self, Bucket):
                return {"Rules": []}

            def get_bucket_cors(self, Bucket):
                return {"CORSRules": []}

            def get_bucket_notification_configuration(self, Bucket):
                return {}

        source_snapshot = {
            "s3_buckets": [
                {
                    "Name": "bucket-a",
                    "Tags": [{"Key": "team", "Value": "platform"}],
                    "Versioning": {"Status": "Enabled"},
                    "BucketEncryption": {"Rules": [{}]},
                    "LifecycleRules": [{}],
                    "CorsRules": [{}],
                    "NotificationConfiguration": {"QueueConfigurations": [{}]},
                }
            ]
        }

        check = s3_parity_checks(source_snapshot, FakeS3Client())
        self.assertEqual(check["status"], "issue")

    def test_s3_parity_checks_uses_mapped_target_bucket_name(self):
        class FakeS3Client:
            def __init__(self):
                self.head_bucket_calls = []

            def head_bucket(self, Bucket):
                self.head_bucket_calls.append(Bucket)
                return {}

            def get_bucket_tagging(self, Bucket):
                return {"TagSet": []}

            def get_bucket_versioning(self, Bucket):
                return {}

            def get_bucket_encryption(self, Bucket):
                return {"ServerSideEncryptionConfiguration": {}}

            def get_bucket_lifecycle_configuration(self, Bucket):
                return {"Rules": []}

            def get_bucket_cors(self, Bucket):
                return {"CORSRules": []}

            def get_bucket_notification_configuration(self, Bucket):
                return {}

        source_snapshot = {"s3_buckets": [{"Name": "legacy-bucket"}]}
        manifest = {"resource_mappings": {"s3_bucket_names": {"legacy-bucket": "virgin-bucket"}}}

        client = FakeS3Client()
        check = s3_parity_checks(source_snapshot, client, manifest=manifest)

        self.assertEqual(check["status"], "ok")
        self.assertEqual(client.head_bucket_calls, ["virgin-bucket"])

    def test_s3_parity_checks_prefers_transfer_plan_mapping(self):
        class FakeS3Client:
            def __init__(self):
                self.head_bucket_calls = []

            def head_bucket(self, Bucket):
                self.head_bucket_calls.append(Bucket)
                return {}

            def get_bucket_tagging(self, Bucket):
                return {"TagSet": []}

            def get_bucket_versioning(self, Bucket):
                return {}

            def get_bucket_encryption(self, Bucket):
                return {"ServerSideEncryptionConfiguration": {}}

            def get_bucket_lifecycle_configuration(self, Bucket):
                return {"Rules": []}

            def get_bucket_cors(self, Bucket):
                return {"CORSRules": []}

            def get_bucket_notification_configuration(self, Bucket):
                return {}

        source_snapshot = {"s3_buckets": [{"Name": "legacy-bucket"}]}
        transfer_plan = {
            "buckets": [{"source_bucket": "legacy-bucket", "target_bucket": "transfer-bucket"}],
            "execution_results": [{"source_bucket": "legacy-bucket", "target_bucket": "transfer-bucket", "issues": []}],
        }

        client = FakeS3Client()
        check = s3_parity_checks(source_snapshot, client, s3_transfer_plan=transfer_plan)

        self.assertEqual(check["status"], "ok")
        self.assertEqual(client.head_bucket_calls, ["transfer-bucket"])


if __name__ == "__main__":
    unittest.main()
