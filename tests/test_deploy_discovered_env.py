import unittest

from executor.scripts.deploy_discovered_env import (
    build_preflight_assessment,
    build_read_only_plan,
    build_codebuild_project_payload,
    build_synthetic_lambda_roles,
    create_api_gateways,
    create_event_source_mappings,
    create_or_update_network,
    create_or_update_roles,
    deploy_codebuild_projects,
    deploy_ecs_task_definitions,
    deploy_ecs_services,
    create_or_update_sqs_queues,
    queue_url_for_target_arn,
    queue_target_name,
    remap_vpc_config,
    required_queue_visibility_by_source,
    rewrite_structure,
    rewrite_string_value,
    rewrite_queue_attributes,
    role_allows_lambda_assume,
    should_skip_recloning,
    target_name,
    update_env_values,
)


class DeployDiscoveredEnvTests(unittest.TestCase):

    def test_build_read_only_plan_summarizes_actions(self):
        snapshot = {
            "iam_roles": [{}, {}],
            "sqs_queues": [{}],
            "sns_topics": [],
            "secrets": [{}],
            "dynamodb_tables": [{}, {}],
            "lambda_functions": [{}, {}],
            "lambda_event_source_mappings": [{}],
            "lambda_permissions": [{}],
            "api_gateways": [{}],
            "codebuild_projects": [{}],
            "ecs": {"clusters": [{}], "task_definitions": [{}], "services": [{}]},
            "cloudformation_stacks": [{}],
            "s3_buckets": [{}, {}],
            "load_balancers": [],
        }

        plan = build_read_only_plan(snapshot, "legacy", "virgin", "platform")

        self.assertEqual(plan["mode"], "read-only-assessment")
        self.assertEqual(plan["planned_actions"]["roles"], 2)
        self.assertEqual(plan["planned_actions"]["lambda_functions"], 2)
        self.assertEqual(plan["planned_actions"]["codebuild_projects"], 1)
        self.assertEqual(plan["manual_review"]["s3_buckets"], 2)
        self.assertIn("preflight_checks", plan)

    def test_build_preflight_assessment_flags_hardcoded_account_refs_and_kms(self):
        snapshot = {
            "lambda_functions": [
                {
                    "Environment": {
                        "Variables": {
                            "ROLE_ARN": "arn:aws:iam::111111111111:role/legacy-role"
                        }
                    }
                }
            ],
            "sqs_queues": [],
            "secrets": [],
            "codebuild_projects": [{"encryptionKey": "arn:aws:kms:us-east-1:111111111111:key/abc"}],
            "s3_buckets": [
                {
                    "BucketEncryption": {
                        "Rules": [
                            {"ApplyServerSideEncryptionByDefault": {"KMSMasterKeyID": "arn:aws:kms:us-east-1:111111111111:key/def"}}
                        ]
                    }
                }
            ],
            "ecs": {"services": []},
            "vpcs": [],
            "subnets": [],
        }

        checks = build_preflight_assessment(
            snapshot,
            {},
            source_account_id="111111111111",
            target_account_id="222222222222",
            source_region="us-east-1",
            target_region="us-east-2",
        )

        hardcoded = next(item for item in checks if item["name"] == "hardcoded-source-account-references")
        kms = next(item for item in checks if item["name"] == "kms-remap")
        self.assertEqual(hardcoded["status"], "warning")
        self.assertEqual(kms["status"], "warning")
        self.assertIn("known_fixes", kms)

    def test_build_codebuild_project_payload_maps_role_and_env_values(self):
        project = {
            "name": "legacy-build",
            "description": "desc",
            "source": {"type": "S3", "location": "legacy-bucket/source.zip"},
            "artifacts": {"type": "S3", "location": "legacy-bucket/output"},
            "environment": {
                "type": "LINUX_CONTAINER",
                "image": "aws/codebuild/standard:7.0",
                "computeType": "BUILD_GENERAL1_SMALL",
                "environmentVariables": [{"name": "QUEUE_URL", "value": "https://sqs.us-east-1.amazonaws.com/123/legacy-q"}],
            },
            "serviceRole": "arn:aws:iam::123:role/legacy-role",
        }
        mappings = {
            "role_arns": {"arn:aws:iam::123:role/legacy-role": "arn:aws:iam::123:role/virgin-role"},
            "queue_urls": {"https://sqs.us-east-1.amazonaws.com/123/legacy-q": "https://sqs.us-east-2.amazonaws.com/123/virgin-q"},
            "queue_arns": {},
            "topic_arns": {},
            "s3_bucket_names": {"legacy-bucket": "virgin-bucket"},
            "s3_bucket_arns": {},
            "secret_arns": {},
            "secret_names": {},
            "dynamodb_table_arns": {},
            "dynamodb_table_names": {},
            "dynamodb_stream_arns": {},
            "function_arns": {},
            "role_arns_extra": {},
        }

        target_project_name, payload = build_codebuild_project_payload(project, mappings, "legacy", "virgin", "platform")

        self.assertEqual(target_project_name, "virgin-build-platform")
        self.assertEqual(payload["serviceRole"], "arn:aws:iam::123:role/virgin-role")
        self.assertEqual(
            payload["environment"]["environmentVariables"][0]["value"],
            "https://sqs.us-east-2.amazonaws.com/123/virgin-q",
        )
        self.assertEqual(payload["source"]["location"], "virgin-bucket/source.zip")
        self.assertEqual(payload["artifacts"]["location"], "virgin-bucket/output")

    def test_build_codebuild_project_payload_remaps_vpc_and_kms(self):
        project = {
            "name": "legacy-build",
            "source": {"type": "S3", "location": "bucket/source.zip"},
            "artifacts": {"type": "NO_ARTIFACTS"},
            "environment": {"type": "LINUX_CONTAINER", "image": "aws/codebuild/standard:7.0", "computeType": "BUILD_GENERAL1_SMALL"},
            "serviceRole": "arn:aws:iam::123:role/legacy-role",
            "encryptionKey": "alias/source-key",
            "vpcConfig": {"subnets": ["subnet-a"], "securityGroupIds": ["sg-a"]},
        }
        mappings = {
            "role_arns": {"arn:aws:iam::123:role/legacy-role": "arn:aws:iam::123:role/virgin-role"},
            "queue_urls": {},
            "queue_arns": {},
            "topic_arns": {},
            "secret_arns": {},
            "secret_names": {},
            "dynamodb_table_arns": {},
            "dynamodb_table_names": {},
            "dynamodb_stream_arns": {},
            "function_arns": {},
            "kms_aliases": {"alias/source-key": "alias/target-key"},
            "subnet_ids": {"subnet-a": "subnet-b"},
            "security_group_ids": {"sg-a": "sg-b"},
        }

        _, payload = build_codebuild_project_payload(project, mappings, "legacy", "virgin", "platform")

        self.assertEqual(payload["encryptionKey"], "alias/target-key")
        self.assertEqual(payload["vpcConfig"]["subnets"], ["subnet-b"])
        self.assertEqual(payload["vpcConfig"]["securityGroupIds"], ["sg-b"])

    def test_build_synthetic_lambda_roles_includes_codebuild_and_ecs_roles(self):
        snapshot = {
            "lambda_functions": [
                {"Role": "arn:aws:iam::123:role/lambda-role"},
            ],
            "codebuild_projects": [
                {"serviceRole": "arn:aws:iam::123:role/codebuild-role"},
            ],
            "ecs": {
                "task_definitions": [
                    {
                        "executionRoleArn": "arn:aws:iam::123:role/ecs-execution-role",
                        "taskRoleArn": "arn:aws:iam::123:role/ecs-task-role",
                    }
                ]
            },
        }

        roles = build_synthetic_lambda_roles(snapshot)
        role_names = {item["RoleName"] for item in roles}

        self.assertIn("lambda-role", role_names)
        self.assertIn("codebuild-role", role_names)
        self.assertIn("ecs-execution-role", role_names)
        self.assertIn("ecs-task-role", role_names)

    def test_build_codebuild_project_payload_drops_cross_region_kms_key(self):
        project = {
            "name": "legacy-build",
            "source": {"type": "S3", "location": "bucket/source.zip"},
            "artifacts": {"type": "NO_ARTIFACTS"},
            "environment": {"type": "LINUX_CONTAINER", "image": "aws/codebuild/standard:7.0", "computeType": "BUILD_GENERAL1_SMALL"},
            "serviceRole": "arn:aws:iam::123:role/legacy-role",
            "encryptionKey": "arn:aws:kms:us-east-1:123:key/abc",
        }
        mappings = {
            "role_arns": {"arn:aws:iam::123:role/legacy-role": "arn:aws:iam::123:role/virgin-role"},
            "queue_urls": {},
            "queue_arns": {},
            "topic_arns": {},
            "secret_arns": {},
            "secret_names": {},
            "dynamodb_table_arns": {},
            "dynamodb_table_names": {},
            "dynamodb_stream_arns": {},
            "function_arns": {},
            "kms_aliases": {},
            "subnet_ids": {},
            "security_group_ids": {},
            "target_region": "us-east-2",
        }

        _, payload = build_codebuild_project_payload(project, mappings, "legacy", "virgin", "platform")

        self.assertNotIn("encryptionKey", payload)

    def test_remap_vpc_config_uses_mapped_ids(self):
        config = remap_vpc_config(
            {"SubnetIds": ["subnet-a"], "SecurityGroupIds": ["sg-a"]},
            {"subnet_ids": {"subnet-a": "subnet-b"}, "security_group_ids": {"sg-a": "sg-b"}},
        )

        self.assertEqual(config["SubnetIds"], ["subnet-b"])
        self.assertEqual(config["SecurityGroupIds"], ["sg-b"])

    def test_create_api_gateways_imports_export_and_extras(self):
        class FakeApiGateway:
            def __init__(self):
                self.request_validators = []
                self.authorizers = []
                self.deployments = []
                self.stage_updates = []
                self.usage_plans = []
                self.api_keys = []
                self.usage_plan_keys = []
                self.gateway_responses = []
                self.domain_names = []
                self.base_path_mappings = []

            def import_rest_api(self, **kwargs):
                return {"id": "api-new"}

            def update_rest_api(self, **kwargs):
                return None

            def create_request_validator(self, **kwargs):
                self.request_validators.append(kwargs)
                return {"id": "validator-1"}

            def create_authorizer(self, **kwargs):
                self.authorizers.append(kwargs)
                return {"id": "auth-1"}

            def create_deployment(self, **kwargs):
                self.deployments.append(kwargs)
                return {"id": "dep-1"}

            def update_stage(self, **kwargs):
                self.stage_updates.append(kwargs)
                return {}

            def create_usage_plan(self, **kwargs):
                self.usage_plans.append(kwargs)
                return {"id": "plan-1"}

            def create_api_key(self, **kwargs):
                self.api_keys.append(kwargs)
                return {"id": "key-1"}

            def create_usage_plan_key(self, **kwargs):
                self.usage_plan_keys.append(kwargs)
                return {"id": "upk-1"}

            def put_gateway_response(self, **kwargs):
                self.gateway_responses.append(kwargs)
                return {"responseType": kwargs["responseType"]}

            def create_domain_name(self, **kwargs):
                self.domain_names.append(kwargs)
                return {"domainName": kwargs["domainName"]}

            def create_base_path_mapping(self, **kwargs):
                self.base_path_mappings.append(kwargs)
                return {"basePath": kwargs.get("basePath", "(none)")}

        snapshot = {
            "api_gateways": [
                {
                    "id": "api-1",
                    "name": "legacy-api",
                    "export_body": "{\"swagger\":\"2.0\"}",
                    "request_validators": [
                        {
                            "name": "legacy-validator",
                            "validateRequestBody": True,
                            "validateRequestParameters": True,
                        }
                    ],
                    "authorizers": [
                        {
                            "name": "legacy-authorizer",
                            "type": "TOKEN",
                            "authorizerUri": "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/arn:aws:lambda:us-east-1:123:function:legacy-authorizer/invocations",
                            "authorizerCredentials": "arn:aws:iam::123:role/legacy-authorizer-role",
                            "identitySource": "method.request.header.Authorization",
                            "authorizerResultTtlInSeconds": 300,
                        }
                    ],
                    "stages": [
                        {
                            "stageName": "prod",
                            "variables": {"QUEUE_URL": "https://legacy"},
                            "methodSettings": {
                                "*/*": {
                                    "metricsEnabled": True,
                                    "loggingLevel": "INFO",
                                    "throttlingBurstLimit": 100,
                                }
                            },
                        }
                    ],
                    "usage_plans": [
                        {
                            "name": "legacy-plan",
                            "apiStages": [{"apiId": "api-1", "stage": "prod"}],
                            "apiKeys": [{"name": "legacy-client-key", "value": "secret-key-value", "enabled": True}],
                        }
                    ],
                    "gateway_responses": [
                        {
                            "responseType": "DEFAULT_4XX",
                            "statusCode": "400",
                            "responseParameters": {"gatewayresponse.header.X-Env": "'legacy'"},
                            "responseTemplates": {"application/json": "{\"env\":\"legacy\"}"},
                        }
                    ],
                    "domain_mappings": [{"domainName": "api.example.com", "endpointConfiguration": {"types": ["REGIONAL"]}, "mappings": [{"stage": "prod"}]}],
                }
            ]
        }
        client = FakeApiGateway()
        mappings = {
            "queue_urls": {"https://legacy": "https://target"},
            "queue_arns": {},
            "topic_arns": {},
            "secret_arns": {},
            "secret_names": {},
            "dynamodb_table_arns": {},
            "dynamodb_table_names": {},
            "dynamodb_stream_arns": {},
            "function_arns": {
                "arn:aws:lambda:us-east-1:123:function:legacy-authorizer": "arn:aws:lambda:us-east-2:456:function:virgin-authorizer"
            },
            "role_arns": {
                "arn:aws:iam::123:role/legacy-authorizer-role": "arn:aws:iam::456:role/virgin-authorizer-role"
            },
            "source_account_id": "123",
            "target_account_id": "456",
        }

        api_ids, deployed, failed = create_api_gateways(snapshot, client, mappings, "legacy", "virgin", "platform")

        self.assertEqual(api_ids["api-1"], "api-new")
        self.assertEqual(deployed[0]["operation"], "imported")
        self.assertEqual(failed, [])
        self.assertEqual(client.request_validators[0]["name"], "virgin-validator-platform")
        self.assertEqual(client.authorizers[0]["name"], "virgin-authorizer-platform")
        self.assertIn("arn:aws:lambda:us-east-2:456:function:virgin-authorizer", client.authorizers[0]["authorizerUri"])
        self.assertEqual(client.authorizers[0]["authorizerCredentials"], "arn:aws:iam::456:role/virgin-authorizer-role")
        self.assertEqual(client.stage_updates[0]["stageName"], "prod")
        self.assertTrue(any(item["path"] == "/*~1*/metrics/enabled" and item["value"] == "true" for item in client.stage_updates[0]["patchOperations"]))
        self.assertTrue(any(item["path"] == "/*~1*/logging/loglevel" and item["value"] == "INFO" for item in client.stage_updates[0]["patchOperations"]))
        self.assertEqual(client.api_keys[0]["name"], "virgin-client-key-platform")
        self.assertEqual(client.api_keys[0]["value"], "secret-key-value")
        self.assertEqual(client.usage_plan_keys[0]["usagePlanId"], "plan-1")
        self.assertEqual(client.gateway_responses[0]["responseType"], "DEFAULT_4XX")
        self.assertEqual(client.gateway_responses[0]["responseParameters"]["gatewayresponse.header.X-Env"], "'virgin'")

    def test_deploy_codebuild_projects_does_not_update_on_invalid_input(self):
        class FakeCodeBuild:
            def create_project(self, **kwargs):
                from botocore.exceptions import ClientError
                raise ClientError({"Error": {"Code": "InvalidInputException", "Message": "bad input"}}, "CreateProject")

        mappings, deployed, failed = deploy_codebuild_projects(
            {"codebuild_projects": [{"name": "aws-dev-agent-tests"}]},
            FakeCodeBuild(),
            {"role_arns": {}, "queue_urls": {}, "queue_arns": {}, "topic_arns": {}, "secret_arns": {}, "secret_names": {}, "dynamodb_table_arns": {}, "dynamodb_table_names": {}, "dynamodb_stream_arns": {}, "function_arns": {}, "kms_aliases": {}, "subnet_ids": {}, "security_group_ids": {}},
            "legacy",
            "virgin",
            "platform",
        )

        self.assertEqual(mappings["codebuild_project_names"], {})
        self.assertEqual(deployed, [])
        self.assertEqual(len(failed), 1)

    def test_create_or_update_network_skips_default_group_without_vpc_mapping(self):
        mappings, deployed, failed = create_or_update_network(
            {"vpcs": [], "subnets": [], "route_tables": [], "security_groups": [{"GroupId": "sg-1", "GroupName": "default", "VpcId": "vpc-1"}]},
            object(),
            "legacy",
            "virgin",
            "platform",
            {},
        )

        self.assertEqual(mappings["security_group_ids"], {})
        self.assertEqual(deployed["security_groups"], [])
        self.assertEqual(failed["security_groups"], [])

    def test_create_or_update_roles_rewrites_customer_managed_policy_arns(self):
        class FakeIam:
            def __init__(self):
                self.attached = []
                self.inline = []

            def create_role(self, **kwargs):
                return {"Role": {"Arn": "arn:aws:iam::456:role/virgin-role"}}

            def list_attached_role_policies(self, RoleName):
                return {"AttachedPolicies": []}

            def attach_role_policy(self, **kwargs):
                self.attached.append(kwargs)

            def put_role_policy(self, **kwargs):
                self.inline.append(kwargs)

        iam = FakeIam()
        mappings, deployed, failed = create_or_update_roles(
            {
                "iam_roles": [
                    {
                        "RoleName": "legacy-role",
                        "Arn": "arn:aws:iam::123:role/legacy-role",
                        "AssumeRolePolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}],
                        },
                        "ManagedPolicies": [
                            {"PolicyArn": "arn:aws:iam::123:policy/legacy-access"}
                        ],
                        "InlinePolicies": [],
                    }
                ]
            },
            "legacy",
            "virgin",
            "platform",
            iam,
        )

        self.assertEqual(failed, [])
        self.assertEqual(deployed[0]["operation"], "created")
        self.assertEqual(mappings["role_arns"]["arn:aws:iam::123:role/legacy-role"], "arn:aws:iam::456:role/virgin-role")
        self.assertEqual(iam.attached[0]["PolicyArn"], "arn:aws:iam::123:policy/virgin-access")

    def test_create_or_update_network_adopts_existing_vpc_and_subnet(self):
        class FakeEc2:
            def create_vpc(self, CidrBlock):
                raise Exception("VpcLimitExceeded")

            def describe_vpcs(self, Filters):
                if Filters[0]["Name"] == "tag:Name":
                    return {"Vpcs": [{"VpcId": "vpc-existing", "CidrBlock": "10.0.0.0/16"}]}
                return {"Vpcs": []}

            def create_subnet(self, VpcId, CidrBlock):
                raise Exception("SubnetConflict")

            def describe_subnets(self, Filters):
                if Filters[1]["Name"] == "tag:Name":
                    return {"Subnets": [{"SubnetId": "subnet-existing", "VpcId": "vpc-existing", "CidrBlock": "10.0.1.0/24"}]}
                return {"Subnets": []}

        mappings, deployed, failed = create_or_update_network(
            {
                "vpcs": [{"VpcId": "vpc-source", "CidrBlock": "10.0.0.0/16", "Tags": [{"Key": "Name", "Value": "main"}]}],
                "subnets": [{"SubnetId": "subnet-source", "VpcId": "vpc-source", "CidrBlock": "10.0.1.0/24", "Tags": [{"Key": "Name", "Value": "private-a"}]}],
                "route_tables": [],
                "security_groups": [],
            },
            FakeEc2(),
            "legacy",
            "virgin",
            "platform",
            {},
        )

        self.assertEqual(mappings["vpc_ids"]["vpc-source"], "vpc-existing")
        self.assertEqual(mappings["subnet_ids"]["subnet-source"], "subnet-existing")
        self.assertEqual(deployed["vpcs"][0]["operation"], "adopted-existing")
        self.assertEqual(deployed["subnets"][0]["operation"], "adopted-existing")
        self.assertEqual(failed["vpcs"], [])
        self.assertEqual(failed["subnets"], [])

    def test_build_synthetic_lambda_roles_creates_basic_roles(self):
        roles = build_synthetic_lambda_roles(
            {
                "lambda_functions": [
                    {"Role": "arn:aws:iam::123:role/remediator-role"},
                    {"Role": "arn:aws:iam::123:role/remediator-role"},
                    {"Role": "arn:aws:iam::123:role/plan-executor-role"},
                ]
            }
        )

        self.assertEqual(len(roles), 2)
        self.assertTrue(any(role["RoleName"] == "remediator-role" for role in roles))

    def test_deploy_ecs_services_does_not_update_on_missing_service(self):
        class FakeEcs:
            def create_service(self, **kwargs):
                from botocore.exceptions import ClientError
                raise ClientError({"Error": {"Code": "InvalidParameterException", "Message": "bad request"}}, "CreateService")

        deployed, failed = deploy_ecs_services(
            {
                "ecs": {
                    "services": [
                        {
                            "serviceName": "svc-a",
                            "clusterArn": "cluster-a",
                            "taskDefinition": "td-a",
                            "desiredCount": 1,
                        }
                    ]
                }
            },
            FakeEcs(),
            {"ecs_cluster_arns": {"cluster-a": "cluster-b"}, "ecs_task_definition_arns": {"td-a": "td-b"}},
            "legacy",
            "virgin",
            "platform",
        )

        self.assertEqual(deployed, [])
        self.assertEqual(len(failed), 1)

    def test_deploy_ecs_services_fails_early_when_network_not_remapped(self):
        deployed, failed = deploy_ecs_services(
            {
                "ecs": {
                    "services": [
                        {
                            "serviceName": "svc-a",
                            "clusterArn": "cluster-a",
                            "taskDefinition": "td-a",
                            "desiredCount": 1,
                            "networkConfiguration": {
                                "awsvpcConfiguration": {
                                    "subnets": ["subnet-a"],
                                    "securityGroups": ["sg-a"],
                                }
                            },
                        }
                    ]
                }
            },
            object(),
            {
                "ecs_cluster_arns": {"cluster-a": "cluster-b"},
                "ecs_task_definition_arns": {"td-a": "td-b"},
                "subnet_ids": {},
                "security_group_ids": {},
            },
            "legacy",
            "virgin",
            "platform",
        )

        self.assertEqual(deployed, [])
        self.assertEqual(failed[0]["error"], "Missing remapped network resources for ECS service")

    def test_deploy_ecs_services_handles_not_idempotent_create_as_existing(self):
        class FakeEcs:
            def create_service(self, **kwargs):
                from botocore.exceptions import ClientError
                raise ClientError(
                    {"Error": {"Code": "InvalidParameterException", "Message": "Creation of service was not idempotent."}},
                    "CreateService",
                )

            def update_service(self, **kwargs):
                return {}

            def describe_services(self, cluster, services):
                return {"services": [{"serviceArn": "arn:aws:ecs:us-east-2:123:service/cluster-b/svc-a"}]}

        deployed, failed = deploy_ecs_services(
            {
                "ecs": {
                    "services": [
                        {
                            "serviceName": "svc-a",
                            "clusterArn": "cluster-a",
                            "taskDefinition": "td-a",
                            "desiredCount": 1,
                        }
                    ]
                }
            },
            FakeEcs(),
            {"ecs_cluster_arns": {"cluster-a": "cluster-b"}, "ecs_task_definition_arns": {"td-a": "td-b"}},
            "legacy",
            "virgin",
            "platform",
        )

        self.assertEqual(failed, [])
        self.assertEqual(deployed[0]["operation"], "updated")

    def test_create_event_source_mappings_adds_dynamodb_stream_permissions(self):
        class FakeLambda:
            def get_function_configuration(self, FunctionName):
                return {"Role": "arn:aws:iam::123:role/task-dispatcher-role"}

            def create_event_source_mapping(self, **kwargs):
                return {"UUID": "mapping-new"}

        class FakeIam:
            def __init__(self):
                self.calls = []

            def put_role_policy(self, **kwargs):
                self.calls.append(kwargs)

        deployed, failed = create_event_source_mappings(
            {
                "lambda_event_source_mappings": [
                    {
                        "UUID": "src-1",
                        "FunctionArn": "arn:aws:lambda:us-east-1:123:function:task-dispatcher",
                        "EventSourceArn": "arn:aws:dynamodb:us-east-1:123:table/agent_errors/stream/abc",
                        "BatchSize": 10,
                        "StartingPosition": "LATEST",
                    }
                ]
            },
            FakeLambda(),
            FakeIam(),
            object(),
            {
                "function_names": {"task-dispatcher": "task-dispatcher"},
                "queue_arns": {},
                "dynamodb_stream_arns": {
                    "arn:aws:dynamodb:us-east-1:123:table/agent_errors/stream/abc": "arn:aws:dynamodb:us-east-2:123:table/agent_errors/stream/xyz"
                },
                "target_env": "virgin",
                "team": "platform",
            },
            "us-east-2",
        )

        self.assertEqual(failed, [])
        self.assertEqual(deployed[0]["operation"], "created")

    def test_required_queue_visibility_by_source_uses_lambda_timeout(self):
        snapshot = {
            "lambda_functions": [
                {"FunctionName": "remediator", "Timeout": 60},
            ],
            "lambda_event_source_mappings": [
                {
                    "UUID": "mapping-1",
                    "EventSourceArn": "arn:aws:sqs:us-east-1:123456789012:agent-fix-tasks.fifo",
                    "FunctionArn": "arn:aws:lambda:us-east-1:123456789012:function:remediator",
                }
            ],
        }

        self.assertEqual(
            required_queue_visibility_by_source(snapshot),
            {"arn:aws:sqs:us-east-1:123456789012:agent-fix-tasks.fifo": 90},
        )

    def test_create_or_update_sqs_queues_raises_visibility_timeout_for_lambda_mapping(self):
        class FakeSqsClient:
            def __init__(self):
                self.created = []

            def create_queue(self, QueueName, Attributes, tags):
                self.created.append(
                    {
                        "QueueName": QueueName,
                        "Attributes": dict(Attributes),
                        "tags": dict(tags),
                    }
                )
                return {"QueueUrl": f"https://sqs.us-east-2.amazonaws.com/123456789012/{QueueName}"}

            def get_queue_attributes(self, QueueUrl, AttributeNames):
                queue_name = QueueUrl.rsplit("/", 1)[-1]
                return {"Attributes": {"QueueArn": f"arn:aws:sqs:us-east-2:123456789012:{queue_name}"}}

        snapshot = {
            "lambda_functions": [
                {"FunctionName": "remediator", "Timeout": 60},
            ],
            "lambda_event_source_mappings": [
                {
                    "UUID": "mapping-1",
                    "EventSourceArn": "arn:aws:sqs:us-east-1:123456789012:agent-fix-tasks.fifo",
                    "FunctionArn": "arn:aws:lambda:us-east-1:123456789012:function:remediator",
                }
            ],
            "sqs_queues": [
                {
                    "QueueName": "agent-fix-tasks.fifo",
                    "QueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/agent-fix-tasks.fifo",
                    "Attributes": {
                        "QueueArn": "arn:aws:sqs:us-east-1:123456789012:agent-fix-tasks.fifo",
                        "VisibilityTimeout": "30",
                        "FifoQueue": "true",
                    },
                    "Tags": {},
                }
            ],
        }
        resource_mappings = {"queue_urls": {}, "queue_arns": {}, "queue_names": {}}
        sqs_client = FakeSqsClient()

        _, deployed, failed = create_or_update_sqs_queues(
            snapshot,
            "full-account-scan",
            "virgin",
            "platform",
            sqs_client,
            resource_mappings,
            preserve_names=True,
        )

        self.assertEqual(failed, [])
        self.assertEqual(deployed[0]["operation"], "created")
        self.assertEqual(sqs_client.created[0]["Attributes"]["VisibilityTimeout"], "90")

    def test_role_allows_lambda_assume_when_trust_policy_matches(self):
        document = {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ]
        }

        self.assertTrue(role_allows_lambda_assume(document))

    def test_role_allows_lambda_assume_rejects_non_lambda_service(self):
        document = {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ]
        }

        self.assertFalse(role_allows_lambda_assume(document))

    def test_target_name_rewrites_source_and_adds_team(self):
        self.assertEqual(
            target_name("legacy-worker", "legacy", "virgin", "payments"),
            "virgin-worker-payments",
        )

    def test_target_name_preserves_name_when_requested(self):
        self.assertEqual(
            target_name("legacy-worker", "legacy", "virgin", "payments", preserve_names=True),
            "legacy-worker",
        )

    def test_queue_target_name_preserves_fifo_suffix(self):
        self.assertEqual(
            queue_target_name("legacy-events.fifo", "legacy", "virgin", "payments"),
            "virgin-events-payments.fifo",
        )

    def test_queue_target_name_preserves_full_name_when_requested(self):
        self.assertEqual(
            queue_target_name("legacy-events.fifo", "legacy", "virgin", "payments", preserve_names=True),
            "legacy-events.fifo",
        )

    def test_should_skip_recloning_for_existing_target_resources(self):
        self.assertTrue(should_skip_recloning("virgin-worker-platform", "virgin", "platform"))
        self.assertFalse(should_skip_recloning("legacy-worker", "virgin", "platform"))

    def test_rewrite_string_value_updates_dependency_links(self):
        mappings = {
            "queue_urls": {
                "https://sqs.us-east-1.amazonaws.com/123/legacy-events": "https://sqs.us-east-1.amazonaws.com/123/virgin-events"
            },
            "queue_arns": {},
            "topic_arns": {},
            "secret_arns": {},
            "secret_names": {},
            "dynamodb_table_arns": {},
            "dynamodb_table_names": {},
            "dynamodb_stream_arns": {},
            "function_arns": {},
            "role_arns": {},
        }
        self.assertEqual(
            rewrite_string_value(
                "https://sqs.us-east-1.amazonaws.com/123/legacy-events",
                mappings,
                "legacy",
                "virgin",
                "payments",
            ),
            "https://sqs.us-east-1.amazonaws.com/123/virgin-events",
        )

    def test_rewrite_string_value_updates_account_ids_when_mappings_are_generic(self):
        mappings = {
            "queue_urls": {},
            "queue_arns": {},
            "topic_arns": {},
            "secret_arns": {},
            "secret_names": {},
            "dynamodb_table_arns": {},
            "dynamodb_table_names": {},
            "dynamodb_stream_arns": {},
            "function_arns": {},
            "role_arns": {},
            "source_account_id": "111111111111",
            "target_account_id": "222222222222",
        }

        self.assertEqual(
            rewrite_string_value(
                "arn:aws:iam::111111111111:role/legacy-role",
                mappings,
                "legacy",
                "virgin",
                "platform",
            ),
            "arn:aws:iam::222222222222:role/virgin-role",
        )
        self.assertEqual(
            rewrite_string_value(
                "https://sqs.us-east-1.amazonaws.com/111111111111/legacy-q",
                mappings,
                "legacy",
                "virgin",
                "platform",
            ),
            "https://sqs.us-east-1.amazonaws.com/222222222222/virgin-q",
        )

    def test_update_env_values_replaces_env_and_team_placeholder(self):
        variables = {
            "BASE_URL": "https://legacy.example.com",
            "TEAM_NAME": "{team}",
            "UNCHANGED": "value",
            "REGION": "us-east-1",
        }
        mappings = {
            "queue_urls": {},
            "queue_arns": {},
            "topic_arns": {},
            "secret_arns": {},
            "secret_names": {},
            "dynamodb_table_arns": {},
            "dynamodb_table_names": {},
            "dynamodb_stream_arns": {},
            "function_arns": {},
            "role_arns": {},
            "target_region": "us-east-2",
        }

        self.assertEqual(
            update_env_values(variables, mappings, "legacy", "virgin", "payments"),
            {
                "BASE_URL": "https://virgin.example.com",
                "TEAM_NAME": "payments",
                "UNCHANGED": "value",
                "REGION": "us-east-2",
            },
        )

    def test_rewrite_queue_attributes_retargets_redrive_policy(self):
        attributes = {
            "RedrivePolicy": '{"deadLetterTargetArn":"arn:aws:sqs:us-east-1:123:legacy-dlq","maxReceiveCount":5}'
        }
        mappings = {
            "queue_urls": {},
            "queue_arns": {
                "arn:aws:sqs:us-east-1:123:legacy-dlq": "arn:aws:sqs:us-east-2:123:virgin-dlq"
            },
            "topic_arns": {},
            "secret_arns": {},
            "secret_names": {},
            "dynamodb_table_arns": {},
            "dynamodb_table_names": {},
            "dynamodb_stream_arns": {},
            "function_arns": {},
            "role_arns": {},
        }

        rewritten = rewrite_queue_attributes(attributes, mappings, "legacy", "virgin", "payments")

        self.assertEqual(
            rewritten["RedrivePolicy"],
            '{"deadLetterTargetArn":"arn:aws:sqs:us-east-2:123:virgin-dlq","maxReceiveCount":5}',
        )

    def test_rewrite_string_value_updates_secret_and_dynamodb_links(self):
        mappings = {
            "queue_urls": {},
            "queue_arns": {},
            "topic_arns": {},
            "secret_arns": {
                "arn:aws:secretsmanager:us-east-1:123:secret:legacy-db": "arn:aws:secretsmanager:us-east-2:123:secret:virgin-db"
            },
            "secret_names": {"legacy-db": "virgin-db"},
            "dynamodb_table_arns": {
                "arn:aws:dynamodb:us-east-1:123:table/legacy-table": "arn:aws:dynamodb:us-east-2:123:table/virgin-table"
            },
            "dynamodb_table_names": {"legacy-table": "virgin-table"},
            "dynamodb_stream_arns": {},
            "function_arns": {},
            "role_arns": {},
        }

        self.assertEqual(
            rewrite_string_value(
                "arn:aws:secretsmanager:us-east-1:123:secret:legacy-db",
                mappings,
                "legacy",
                "virgin",
                "payments",
            ),
            "arn:aws:secretsmanager:us-east-2:123:secret:virgin-db",
        )
        self.assertEqual(
            rewrite_string_value(
                "arn:aws:dynamodb:us-east-1:123:table/legacy-table",
                mappings,
                "legacy",
                "virgin",
                "payments",
            ),
            "arn:aws:dynamodb:us-east-2:123:table/virgin-table",
        )

    def test_rewrite_string_value_updates_kms_alias_links(self):
        mappings = {
            "queue_urls": {},
            "queue_arns": {},
            "topic_arns": {},
            "secret_arns": {},
            "secret_names": {},
            "dynamodb_table_arns": {},
            "dynamodb_table_names": {},
            "dynamodb_stream_arns": {},
            "function_arns": {},
            "role_arns": {},
            "kms_key_ids": {},
            "kms_aliases": {"alias/source-key": "alias/target-key"},
        }

        self.assertEqual(
            rewrite_string_value(
                "alias/source-key",
                mappings,
                "legacy",
                "virgin",
                "platform",
            ),
            "alias/target-key",
        )

    def test_deploy_ecs_task_definitions_remaps_awslogs_region(self):
        class FakeEcsClient:
            def register_task_definition(self, **payload):
                self.payload = payload
                return {"taskDefinition": {"taskDefinitionArn": "arn:aws:ecs:us-east-2:123:task-definition/test:1"}}

        ecs_client = FakeEcsClient()
        snapshot = {
            "ecs": {
                "task_definitions": [{
                    "family": "test",
                    "taskDefinitionArn": "arn:aws:ecs:us-east-1:123:task-definition/test:1",
                    "networkMode": "awsvpc",
                    "containerDefinitions": [{
                        "name": "app",
                        "image": "nginx",
                        "logConfiguration": {
                            "logDriver": "awslogs",
                            "options": {
                                "awslogs-group": "/ecs/test",
                                "awslogs-region": "us-east-1",
                            },
                        },
                    }],
                }]
            }
        }
        resource_mappings = {"role_arns": {}, "target_region": "us-east-2"}

        deploy_ecs_task_definitions(snapshot, ecs_client, resource_mappings, "src", "dst", "team")

        self.assertEqual(
            ecs_client.payload["containerDefinitions"][0]["logConfiguration"]["options"]["awslogs-region"],
            "us-east-2",
        )

    def test_rewrite_string_value_updates_table_name_references(self):
        mappings = {
            "queue_urls": {},
            "queue_arns": {},
            "topic_arns": {},
            "secret_arns": {},
            "secret_names": {},
            "dynamodb_table_arns": {},
            "dynamodb_table_names": {"agent_errors": "virgin-agent_errors-platform"},
            "dynamodb_stream_arns": {},
            "function_arns": {},
            "role_arns": {},
        }

        self.assertEqual(
            rewrite_string_value(
                "agent_errors",
                mappings,
                "",
                "virgin",
                "platform",
            ),
            "virgin-agent_errors-platform",
        )

    def test_rewrite_structure_updates_nested_policy_and_env_refs(self):
        mappings = {
            "queue_urls": {},
            "queue_arns": {"arn:aws:sqs:us-east-1:123:legacy-q": "arn:aws:sqs:us-east-2:456:virgin-q"},
            "topic_arns": {},
            "secret_arns": {},
            "secret_names": {},
            "dynamodb_table_arns": {},
            "dynamodb_table_names": {},
            "dynamodb_stream_arns": {},
            "function_arns": {},
            "role_arns": {"arn:aws:iam::123:role/legacy-role": "arn:aws:iam::456:role/virgin-role"},
        }

        value = {
            "Statement": [
                {
                    "Resource": ["arn:aws:sqs:us-east-1:123:legacy-q"],
                    "Principal": {"AWS": "arn:aws:iam::123:role/legacy-role"},
                }
            ]
        }

        rewritten = rewrite_structure(value, mappings, "legacy", "virgin", "platform")

        self.assertEqual(rewritten["Statement"][0]["Resource"][0], "arn:aws:sqs:us-east-2:456:virgin-q")
        self.assertEqual(rewritten["Statement"][0]["Principal"]["AWS"], "arn:aws:iam::456:role/virgin-role")

    def test_queue_url_for_target_arn_falls_back_to_derived_url(self):
        self.assertEqual(
            queue_url_for_target_arn({}, "arn:aws:sqs:us-east-2:123456789012:agent-fix-tasks.fifo"),
            "https://sqs.us-east-2.amazonaws.com/123456789012/agent-fix-tasks.fifo",
        )


if __name__ == "__main__":
    unittest.main()
