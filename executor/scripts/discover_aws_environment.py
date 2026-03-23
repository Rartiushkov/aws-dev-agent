import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import boto3
from botocore.exceptions import ClientError
from executor.scripts.audit_log import append_audit_event
from executor.scripts.transfer_common import config_override, inventory_dir_name, inventory_dir_path, load_transfer_config, resolve_client_slug, session_for, should_exclude


SENSITIVE_SNAPSHOT_KEYS = {
    "AccessKeyId",
    "SecretAccessKey",
    "SessionToken",
    "CloudTrailEvent",
    "Authorization",
    "Password",
    "SecretString",
    "SecretBinary",
}


def sanitize_name(value):
    cleaned = re.sub(r"[^a-zA-Z0-9-_]+", "-", value.strip().lower())
    return re.sub(r"-{2,}", "-", cleaned).strip("-_")


def sanitize_snapshot_value(value):
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if key in SENSITIVE_SNAPSHOT_KEYS:
                sanitized[key] = "[REDACTED]"
                continue
            sanitized[key] = sanitize_snapshot_value(item)
        return sanitized

    if isinstance(value, list):
        return [sanitize_snapshot_value(item) for item in value]

    return value


def parse_args():
    parser = argparse.ArgumentParser(description="Discover AWS resources for a source environment.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--team", default="")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--config", default="")
    parser.add_argument("--source-role-arn", default="")
    parser.add_argument("--source-external-id", default="")
    parser.add_argument("--inventory-key", default="")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def _matches_source(name, source_env):
    if not source_env:
        return True
    lowered = name.lower()
    source = source_env.lower()
    return lowered == source or lowered.startswith(f"{source}-") or source in lowered


def _safe_list(getter, default):
    try:
        return getter()
    except Exception:
        return default


def _collect_git_urls(value):
    if not isinstance(value, str):
        return []
    pattern = r"(https?://[^\s'\"]+?\.git|git@[^:\s]+:[^\s'\"]+?\.git)"
    return re.findall(pattern, value)


def _list_lambda_functions(lambda_client, source_env, config):
    paginator = lambda_client.get_paginator("list_functions")
    items = []
    for page in paginator.paginate():
        for fn in page.get("Functions", []):
            if _matches_source(fn["FunctionName"], source_env):
                if should_exclude("lambda_functions", fn["FunctionName"], config):
                    continue
                items.append(fn)
    return items


def _list_cloudformation_stacks(cf_client, source_env, config):
    paginator = cf_client.get_paginator("list_stacks")
    items = []
    for page in paginator.paginate(StackStatusFilter=[
        "CREATE_COMPLETE",
        "UPDATE_COMPLETE",
        "UPDATE_ROLLBACK_COMPLETE",
        "IMPORT_COMPLETE",
    ]):
        for stack in page.get("StackSummaries", []):
            if _matches_source(stack["StackName"], source_env):
                if should_exclude("cloudformation_stacks", stack["StackName"], config):
                    continue
                items.append(stack)
    return items


def _list_load_balancers(elbv2_client, source_env, config):
    paginator = elbv2_client.get_paginator("describe_load_balancers")
    items = []
    for page in paginator.paginate():
        for lb in page.get("LoadBalancers", []):
            if _matches_source(lb["LoadBalancerName"], source_env):
                if should_exclude("load_balancers", lb["LoadBalancerName"], config):
                    continue
                items.append(lb)
    return items


def _list_security_groups(ec2_client, source_env, config):
    paginator = ec2_client.get_paginator("describe_security_groups")
    items = []
    for page in paginator.paginate():
        for sg in page.get("SecurityGroups", []):
            if _matches_source(sg.get("GroupName", ""), source_env):
                if should_exclude("security_groups", sg.get("GroupName", ""), config):
                    continue
                items.append(sg)
    return items


def _list_s3_buckets(s3_client, region, source_env, config):
    items = []
    response = _safe_list(lambda: s3_client.list_buckets(), {})
    for bucket in response.get("Buckets", []):
        bucket_name = bucket["Name"]
        if not _matches_source(bucket_name, source_env):
            continue
        if should_exclude("s3_buckets", bucket_name, config):
            continue
        try:
            location = s3_client.get_bucket_location(Bucket=bucket_name).get("LocationConstraint") or "us-east-1"
        except ClientError:
            location = "unknown"
        if location != region:
            continue
        tags = _safe_list(
            lambda: s3_client.get_bucket_tagging(Bucket=bucket_name).get("TagSet", []),
            [],
        )
        versioning = _safe_list(
            lambda: s3_client.get_bucket_versioning(Bucket=bucket_name),
            {},
        )
        encryption = _safe_list(
            lambda: s3_client.get_bucket_encryption(Bucket=bucket_name).get("ServerSideEncryptionConfiguration", {}),
            {},
        )
        lifecycle = _safe_list(
            lambda: s3_client.get_bucket_lifecycle_configuration(Bucket=bucket_name).get("Rules", []),
            [],
        )
        cors = _safe_list(
            lambda: s3_client.get_bucket_cors(Bucket=bucket_name).get("CORSRules", []),
            [],
        )
        policy = _safe_list(
            lambda: s3_client.get_bucket_policy(Bucket=bucket_name).get("Policy", ""),
            "",
        )
        notifications = _safe_list(
            lambda: s3_client.get_bucket_notification_configuration(Bucket=bucket_name),
            {},
        )
        items.append({
            "Name": bucket_name,
            "Region": location,
            "CreationDate": bucket.get("CreationDate"),
            "Tags": tags,
            "Versioning": versioning,
            "BucketEncryption": encryption,
            "LifecycleRules": lifecycle,
            "CorsRules": cors,
            "Policy": policy,
            "NotificationConfiguration": notifications,
        })
    return items


def _list_codebuild_projects(codebuild_client, source_env, config):
    items = []
    names = _safe_list(lambda: codebuild_client.list_projects().get("projects", []), [])
    for name in names:
        if not _matches_source(name, source_env):
            continue
        if should_exclude("codebuild_projects", name, config):
            continue
        batch = _safe_list(lambda n=name: codebuild_client.batch_get_projects(names=[n]).get("projects", []), [])
        if batch:
            items.append(batch[0])
        else:
            items.append({"name": name})
    return items


def _list_vpcs(ec2_client):
    return _safe_list(lambda: ec2_client.describe_vpcs().get("Vpcs", []), [])


def _list_subnets(ec2_client):
    return _safe_list(lambda: ec2_client.describe_subnets().get("Subnets", []), [])


def _list_route_tables(ec2_client):
    return _safe_list(lambda: ec2_client.describe_route_tables().get("RouteTables", []), [])


def _list_ec2_instances(ec2_client, source_env, config):
    reservations = _safe_list(lambda: ec2_client.describe_instances().get("Reservations", []), [])
    instances = []
    for reservation in reservations:
        for instance in reservation.get("Instances", []):
            instance_id = instance.get("InstanceId", "")
            name = next((tag.get("Value", "") for tag in instance.get("Tags", []) if tag.get("Key") == "Name"), "")
            match_target = name or instance_id
            if not _matches_source(match_target, source_env):
                continue
            if should_exclude("ec2_instances", match_target, config):
                continue
            instances.append(instance)
    return instances


def _list_rds(rds_client, source_env, config):
    instances = []
    for db in _safe_list(lambda: rds_client.describe_db_instances().get("DBInstances", []), []):
        identifier = db.get("DBInstanceIdentifier", "")
        if _matches_source(identifier, source_env) and not should_exclude("rds_instances", identifier, config):
            instances.append(db)

    clusters = []
    for cluster in _safe_list(lambda: rds_client.describe_db_clusters().get("DBClusters", []), []):
        identifier = cluster.get("DBClusterIdentifier", "")
        if _matches_source(identifier, source_env) and not should_exclude("rds_clusters", identifier, config):
            clusters.append(cluster)

    subnet_groups = []
    for subnet_group in _safe_list(lambda: rds_client.describe_db_subnet_groups().get("DBSubnetGroups", []), []):
        name = subnet_group.get("DBSubnetGroupName", "")
        if _matches_source(name, source_env) and not should_exclude("rds_subnet_groups", name, config):
            subnet_groups.append(subnet_group)

    parameter_groups = []
    for parameter_group in _safe_list(lambda: rds_client.describe_db_parameter_groups().get("DBParameterGroups", []), []):
        name = parameter_group.get("DBParameterGroupName", "")
        if _matches_source(name, source_env) and not should_exclude("rds_parameter_groups", name, config):
            parameter_groups.append(parameter_group)

    return {
        "instances": instances,
        "clusters": clusters,
        "subnet_groups": subnet_groups,
        "parameter_groups": parameter_groups,
    }


def _discover_git_repositories(snapshot):
    repositories = {}

    def remember(url, source_type, source_name, extra=None):
        parsed = urlparse(url.replace("git@", "ssh://git@") if url.startswith("git@") else url)
        host = parsed.hostname or ""
        name = parsed.path.rsplit("/", 1)[-1] if parsed.path else url
        repositories.setdefault(url, {
            "url": url,
            "host": host,
            "name": name.removesuffix(".git"),
            "sources": [],
        })
        item = {"type": source_type, "name": source_name}
        if extra:
            item.update(extra)
        repositories[url]["sources"].append(item)

    for fn in snapshot.get("lambda_functions", []):
        for key, value in fn.get("Environment", {}).get("Variables", {}).items():
            for url in _collect_git_urls(value):
                remember(url, "lambda-env", fn.get("FunctionName", ""), {"key": key})

    for project in snapshot.get("codebuild_projects", []):
        source = project.get("source", {})
        if source.get("location"):
            for url in _collect_git_urls(source["location"]):
                remember(url, "codebuild-source", project.get("name", ""))
        for env_var in project.get("environment", {}).get("environmentVariables", []):
            for url in _collect_git_urls(env_var.get("value", "")):
                remember(url, "codebuild-env", project.get("name", ""), {"key": env_var.get("name", "")})

    return list(repositories.values())


def _list_ecs(ecs_client, source_env, config):
    clusters = []
    cluster_arns = ecs_client.list_clusters().get("clusterArns", [])
    if cluster_arns:
        described = ecs_client.describe_clusters(clusters=cluster_arns).get("clusters", [])
        clusters = [
            cluster for cluster in described
            if _matches_source(cluster["clusterName"], source_env)
            and not should_exclude("ecs_clusters", cluster["clusterName"], config)
        ]

    services = []
    task_definitions = []
    tasks = []
    for cluster in clusters:
        service_arns = ecs_client.list_services(cluster=cluster["clusterArn"]).get("serviceArns", [])
        if service_arns:
            described_services = ecs_client.describe_services(cluster=cluster["clusterArn"], services=service_arns).get("services", [])
            for service in described_services:
                if should_exclude("ecs_services", service["serviceName"], config):
                    continue
                services.append(service)
                task_arns = _safe_list(
                    lambda c=cluster["clusterArn"], s=service["serviceName"]: ecs_client.list_tasks(cluster=c, serviceName=s).get("taskArns", []),
                    [],
                )
                if task_arns:
                    tasks.extend(_safe_list(
                        lambda c=cluster["clusterArn"], arns=task_arns: ecs_client.describe_tasks(cluster=c, tasks=arns).get("tasks", []),
                        [],
                    ))
                task_def_arn = service.get("taskDefinition")
                if task_def_arn:
                    task_definitions.append(ecs_client.describe_task_definition(taskDefinition=task_def_arn)["taskDefinition"])

    return {
        "clusters": clusters,
        "services": services,
        "task_definitions": list({item.get("taskDefinitionArn"): item for item in task_definitions if item.get("taskDefinitionArn")}.values()),
        "tasks": tasks,
    }


def _list_ecs_scheduled_tasks(events_client, source_env, config):
    scheduled = []
    rules = _safe_list(lambda: events_client.list_rules().get("Rules", []), [])
    for rule in rules:
        rule_name = rule.get("Name", "")
        if not _matches_source(rule_name, source_env):
            continue
        if should_exclude("eventbridge_rules", rule_name, config):
            continue
        targets = _safe_list(lambda rn=rule_name: events_client.list_targets_by_rule(Rule=rn).get("Targets", []), [])
        ecs_targets = [target for target in targets if "ecsParameters" in json.dumps(target).lower() or ":ecs:" in str(target.get("Arn", ""))]
        if ecs_targets:
            scheduled.append({
                "Rule": rule,
                "Targets": ecs_targets,
            })
    return scheduled


def _list_ecs_event_history(cloudtrail_client, source_env):
    events = _safe_list(
        lambda: cloudtrail_client.lookup_events(
            LookupAttributes=[{"AttributeKey": "EventSource", "AttributeValue": "ecs.amazonaws.com"}],
            MaxResults=50,
        ).get("Events", []),
        [],
    )
    filtered = []
    for event in events:
        resources = event.get("Resources", [])
        joined = " ".join([resource.get("ResourceName", "") for resource in resources])
        if source_env and source_env not in joined.lower():
            continue
        filtered.append(event)
    return filtered


def _ecs_metrics_for_dimension(cloudwatch_client, namespace, metric_name, dimensions):
    response = _safe_list(
        lambda: cloudwatch_client.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=(datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0),
            EndTime=datetime.now(timezone.utc).replace(microsecond=0),
            Period=300,
            Statistics=["Average", "Maximum"],
        ),
        {},
    )
    datapoints = response.get("Datapoints", [])
    datapoints.sort(key=lambda item: item.get("Timestamp", datetime.min.replace(tzinfo=timezone.utc)))
    return datapoints[-1] if datapoints else {}


def _list_ecs_metrics(cloudwatch_client, ecs_snapshot):
    metrics = {"clusters": [], "services": []}
    for cluster in ecs_snapshot.get("clusters", []):
        cluster_name = cluster.get("clusterName")
        if not cluster_name:
            continue
        metrics["clusters"].append({
            "clusterName": cluster_name,
            "cpu": _ecs_metrics_for_dimension(
                cloudwatch_client,
                "AWS/ECS",
                "CPUUtilization",
                [{"Name": "ClusterName", "Value": cluster_name}],
            ),
            "memory": _ecs_metrics_for_dimension(
                cloudwatch_client,
                "AWS/ECS",
                "MemoryUtilization",
                [{"Name": "ClusterName", "Value": cluster_name}],
            ),
        })
    for service in ecs_snapshot.get("services", []):
        cluster_arn = service.get("clusterArn", "")
        cluster_name = cluster_arn.split("/")[-1] if cluster_arn else ""
        service_name = service.get("serviceName")
        if not cluster_name or not service_name:
            continue
        dimensions = [
            {"Name": "ClusterName", "Value": cluster_name},
            {"Name": "ServiceName", "Value": service_name},
        ]
        metrics["services"].append({
            "clusterName": cluster_name,
            "serviceName": service_name,
            "cpu": _ecs_metrics_for_dimension(cloudwatch_client, "AWS/ECS", "CPUUtilization", dimensions),
            "memory": _ecs_metrics_for_dimension(cloudwatch_client, "AWS/ECS", "MemoryUtilization", dimensions),
        })
    return metrics


def _list_sqs_queues(sqs_client, source_env, config):
    queue_urls = sqs_client.list_queues().get("QueueUrls", [])
    items = []
    for url in queue_urls:
        queue_name = url.rstrip("/").split("/")[-1]
        if not _matches_source(queue_name, source_env):
            continue
        if should_exclude("sqs_queues", queue_name, config):
            continue
        attributes = sqs_client.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["All"],
        )["Attributes"]
        tags = _safe_list(
            lambda: sqs_client.list_queue_tags(QueueUrl=url).get("Tags", {}),
            {},
        )
        items.append({
            "QueueName": queue_name,
            "QueueUrl": url,
            "Attributes": attributes,
            "Tags": tags,
        })
    return items


def _list_lambda_event_source_mappings(lambda_client, lambda_functions, source_env, config):
    mappings = []
    for fn in lambda_functions:
        function_name = fn["FunctionName"]
        response = lambda_client.list_event_source_mappings(FunctionName=function_name)
        for item in response.get("EventSourceMappings", []):
            source_arn = item.get("EventSourceArn", "")
            if source_arn and source_env and source_env not in source_arn and source_env not in function_name.lower():
                if not _matches_source(source_arn.split(":")[-1], source_env):
                    continue
            if should_exclude("lambda_event_source_mappings", item.get("UUID", ""), config):
                continue
            mappings.append(item)
    return mappings


def _list_lambda_permissions(lambda_client, lambda_functions):
    permissions = []
    for fn in lambda_functions:
        try:
            policy = lambda_client.get_policy(FunctionName=fn["FunctionName"])
            permissions.append({
                "FunctionName": fn["FunctionName"],
                "Policy": json.loads(policy["Policy"]),
            })
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                permissions.append({
                    "FunctionName": fn["FunctionName"],
                    "PolicyError": str(exc),
                })
    return permissions


def _discover_lambda_roles(iam_client, lambda_functions, source_env, config):
    roles = []
    seen = set()
    for fn in lambda_functions:
        role_arn = fn.get("Role")
        if not role_arn or role_arn in seen:
            continue
        seen.add(role_arn)
        role_name = role_arn.split("/")[-1]
        try:
            role = iam_client.get_role(RoleName=role_name)["Role"]
            attached = iam_client.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", [])
            inline_policy_names = iam_client.list_role_policies(RoleName=role_name).get("PolicyNames", [])
            inline_policies = []
            for policy_name in inline_policy_names:
                document = iam_client.get_role_policy(RoleName=role_name, PolicyName=policy_name)
                inline_policies.append({
                    "PolicyName": policy_name,
                    "PolicyDocument": document["PolicyDocument"],
                })
            if not source_env or _matches_source(role_name, source_env) or role_name.endswith("-role"):
                if should_exclude("iam_roles", role_name, config):
                    continue
                roles.append({
                    "RoleName": role_name,
                    "Arn": role["Arn"],
                    "AssumeRolePolicyDocument": role["AssumeRolePolicyDocument"],
                    "Description": role.get("Description", ""),
                    "Path": role.get("Path", "/"),
                    "ManagedPolicies": attached,
                    "InlinePolicies": inline_policies,
                })
        except ClientError:
            continue
    return roles


def _list_sns_topics(sns_client, source_env, config):
    topics = []
    paginator = sns_client.get_paginator("list_topics")
    for page in paginator.paginate():
        for topic in page.get("Topics", []):
            arn = topic["TopicArn"]
            name = arn.split(":")[-1]
            if not _matches_source(name, source_env):
                continue
            if should_exclude("sns_topics", name, config):
                continue
            attributes = sns_client.get_topic_attributes(TopicArn=arn).get("Attributes", {})
            subscriptions = sns_client.list_subscriptions_by_topic(TopicArn=arn).get("Subscriptions", [])
            topics.append({
                "TopicArn": arn,
                "TopicName": name,
                "Attributes": attributes,
                "Subscriptions": subscriptions,
            })
    return topics


def _list_api_gateways(apigw_client, source_env, config):
    rest_apis = []
    paginator = apigw_client.get_paginator("get_rest_apis")
    for page in paginator.paginate():
        for api in page.get("items", []):
            name = api.get("name", "")
            if not _matches_source(name, source_env):
                continue
            if should_exclude("api_gateways", name, config):
                continue
            resources = _safe_list(
                lambda: apigw_client.get_resources(restApiId=api["id"], limit=500).get("items", []),
                [],
            )
            methods = []
            for resource in resources:
                resource_methods = resource.get("resourceMethods", {})
                for http_method in resource_methods:
                    try:
                        method_config = apigw_client.get_method(
                            restApiId=api["id"],
                            resourceId=resource["id"],
                            httpMethod=http_method,
                        )
                    except ClientError:
                        method_config = {}
                    try:
                        integration = apigw_client.get_integration(
                            restApiId=api["id"],
                            resourceId=resource["id"],
                            httpMethod=http_method,
                        )
                    except ClientError:
                        integration = {}
                    methods.append({
                        "resourceId": resource["id"],
                        "path": resource.get("path"),
                        "httpMethod": http_method,
                        "method": method_config,
                        "integration": integration,
                    })
            stages = _safe_list(
                lambda: apigw_client.get_stages(restApiId=api["id"]).get("item", []),
                [],
            )
            authorizers = _safe_list(
                lambda: apigw_client.get_authorizers(restApiId=api["id"], limit=500).get("items", []),
                [],
            )
            request_validators = _safe_list(
                lambda: apigw_client.get_request_validators(restApiId=api["id"], limit=500).get("items", []),
                [],
            )
            gateway_responses = _safe_list(
                lambda: apigw_client.get_gateway_responses(restApiId=api["id"], limit=500).get("items", []),
                [],
            )
            usage_plans = []
            for usage_plan in _safe_list(lambda: apigw_client.get_usage_plans(limit=500).get("items", []), []):
                if any(item.get("apiId") == api["id"] for item in usage_plan.get("apiStages", [])):
                    usage_plan_keys = _safe_list(
                        lambda upid=usage_plan["id"]: apigw_client.get_usage_plan_keys(usagePlanId=upid, limit=500).get("items", []),
                        [],
                    )
                    api_keys = []
                    for key_ref in usage_plan_keys:
                        key_id = key_ref.get("id")
                        if not key_id:
                            continue
                        api_key = _safe_list(
                            lambda kid=key_id: apigw_client.get_api_key(apiKey= kid, includeValue=True),
                            {},
                        )
                        if api_key:
                            api_keys.append(api_key)
                    usage_plan = dict(usage_plan)
                    usage_plan["apiKeys"] = api_keys
                    usage_plans.append(usage_plan)
            domain_mappings = []
            for domain_name in _safe_list(lambda: apigw_client.get_domain_names(limit=500).get("items", []), []):
                mappings = _safe_list(
                    lambda d=domain_name["domainName"]: apigw_client.get_base_path_mappings(domainName=d, limit=500).get("items", []),
                    [],
                )
                matched = [mapping for mapping in mappings if mapping.get("restApiId") == api["id"]]
                if matched:
                    domain_mappings.append({
                        "domainName": domain_name.get("domainName"),
                        "certificateArn": domain_name.get("certificateArn"),
                        "regionalCertificateArn": domain_name.get("regionalCertificateArn"),
                        "endpointConfiguration": domain_name.get("endpointConfiguration", {}),
                        "securityPolicy": domain_name.get("securityPolicy"),
                        "mappings": matched,
                    })
            export_body = ""
            export_stage_name = stages[0].get("stageName") if stages else ""
            if export_stage_name:
                try:
                    response = apigw_client.get_export(
                        restApiId=api["id"],
                        stageName=export_stage_name,
                        exportType="swagger",
                        parameters={"extensions": "apigateway"},
                        accepts="application/json",
                    )
                    body = response.get("body")
                    if body:
                        export_body = body.read().decode("utf-8")
                except Exception:
                    export_body = ""
            rest_apis.append({
                "id": api["id"],
                "name": name,
                "description": api.get("description", ""),
                "resources": resources,
                "methods": methods,
                "stages": stages,
                "authorizers": authorizers,
                "request_validators": request_validators,
                "gateway_responses": gateway_responses,
                "usage_plans": usage_plans,
                "domain_mappings": domain_mappings,
                "export_body": export_body,
                "export_stage_name": export_stage_name,
            })
    return rest_apis


def _list_secrets(secrets_client, source_env, config):
    secrets = []
    paginator = secrets_client.get_paginator("list_secrets")
    for page in paginator.paginate():
        for secret in page.get("SecretList", []):
            name = secret.get("Name", "")
            if not _matches_source(name, source_env):
                continue
            if should_exclude("secrets", name, config):
                continue
            item = {
                "ARN": secret.get("ARN"),
                "Name": name,
                "Description": secret.get("Description", ""),
                "KmsKeyId": secret.get("KmsKeyId"),
                "Tags": secret.get("Tags", []),
            }
            try:
                value = secrets_client.get_secret_value(SecretId=name)
                item["HasSecretString"] = "SecretString" in value
                item["HasSecretBinary"] = "SecretBinary" in value
                item["VersionId"] = value.get("VersionId")
            except ClientError as exc:
                item["SecretValueError"] = str(exc)
            secrets.append(item)
    return secrets


def _list_dynamodb_tables(dynamodb_client, source_env, config):
    tables = []
    paginator = dynamodb_client.get_paginator("list_tables")
    for page in paginator.paginate():
        for table_name in page.get("TableNames", []):
            if not _matches_source(table_name, source_env):
                continue
            if should_exclude("dynamodb_tables", table_name, config):
                continue
            description = dynamodb_client.describe_table(TableName=table_name)["Table"]
            tags = _safe_list(
                lambda: dynamodb_client.list_tags_of_resource(ResourceArn=description["TableArn"]).get("Tags", []),
                [],
            )
            ttl = _safe_list(
                lambda: dynamodb_client.describe_time_to_live(TableName=table_name).get("TimeToLiveDescription", {}),
                {},
            )
            pitr = _safe_list(
                lambda: dynamodb_client.describe_continuous_backups(TableName=table_name).get("ContinuousBackupsDescription", {}),
                {},
            )
            tables.append({
                "Table": description,
                "Tags": tags,
                "TimeToLiveDescription": ttl,
                "ContinuousBackupsDescription": pitr,
            })
    return tables


def build_dependency_graph(snapshot):
    graph = {"nodes": [], "edges": []}
    node_ids = set()

    def add_node(node_id, node_type, name):
        if not node_id or node_id in node_ids:
            return
        node_ids.add(node_id)
        graph["nodes"].append({"id": node_id, "type": node_type, "name": name or node_id})

    def add_edge(source, target, relationship, **extra):
        if not source or not target:
            return
        edge = {"from": source, "to": target, "relationship": relationship}
        edge.update({key: value for key, value in extra.items() if value is not None})
        graph["edges"].append(edge)

    for queue in snapshot["sqs_queues"]:
        queue_arn = queue.get("QueueArn") or queue.get("Attributes", {}).get("QueueArn")
        if not queue_arn:
            continue
        add_node(queue_arn, "sqs", queue["QueueName"])

    for role in snapshot["iam_roles"]:
        add_node(role["Arn"], "iam-role", role["RoleName"])

    for topic in snapshot["sns_topics"]:
        add_node(topic["TopicArn"], "sns-topic", topic["TopicName"])

    for api in snapshot["api_gateways"]:
        add_node(api["id"], "api-gateway", api["name"])

    for cluster in snapshot.get("ecs", {}).get("clusters", []):
        cluster_arn = cluster.get("clusterArn")
        cluster_name = cluster.get("clusterName") or cluster_arn
        add_node(cluster_arn, "ecs-cluster", cluster_name)

    for service in snapshot.get("ecs", {}).get("services", []):
        service_arn = service.get("serviceArn") or service.get("serviceName")
        service_name = service.get("serviceName") or service_arn
        add_node(service_arn, "ecs-service", service_name)
        add_edge(service_arn, service.get("clusterArn"), "runs-in-cluster")
        add_edge(service_arn, service.get("taskDefinition"), "uses-task-definition")
        awsvpc = (service.get("networkConfiguration") or {}).get("awsvpcConfiguration", {})
        for subnet_id in awsvpc.get("subnets", []):
            add_edge(service_arn, subnet_id, "uses-subnet")
        for sg_id in awsvpc.get("securityGroups", []):
            add_edge(service_arn, sg_id, "uses-security-group")

    for task_definition in snapshot.get("ecs", {}).get("task_definitions", []):
        task_definition_arn = task_definition.get("taskDefinitionArn")
        family = task_definition.get("family") or task_definition_arn
        add_node(task_definition_arn, "ecs-task-definition", family)
        add_edge(task_definition_arn, task_definition.get("taskRoleArn"), "assumes-role")
        add_edge(task_definition_arn, task_definition.get("executionRoleArn"), "execution-role")
        for container in task_definition.get("containerDefinitions", []):
            for env_var in container.get("environment", []):
                key = env_var.get("name", "env")
                value = env_var.get("value")
                if isinstance(value, str) and ":sqs:" in value:
                    add_edge(task_definition_arn, value, f"env:{key}")
                if isinstance(value, str) and ":sns:" in value:
                    add_edge(task_definition_arn, value, f"env:{key}")
                if isinstance(value, str) and ":secretsmanager:" in value:
                    add_edge(task_definition_arn, value, f"env:{key}")
                if isinstance(value, str) and ":dynamodb:" in value:
                    add_edge(task_definition_arn, value, f"env:{key}")
                if isinstance(value, str):
                    for url in _collect_git_urls(value):
                        add_edge(task_definition_arn, url, f"env:{key}")
            for secret in container.get("secrets", []):
                add_edge(task_definition_arn, secret.get("valueFrom"), f"secret:{container.get('name', 'container')}")

    for scheduled_task in snapshot.get("ecs_scheduled_tasks", []):
        rule = scheduled_task.get("Rule", {})
        rule_arn = rule.get("Arn") or rule.get("Name")
        rule_name = rule.get("Name") or rule_arn
        add_node(rule_arn, "eventbridge-rule", rule_name)
        for target in scheduled_task.get("Targets", []):
            ecs_params = target.get("EcsParameters", {}) or target.get("ecsParameters", {})
            add_edge(rule_arn, target.get("Arn"), "schedule-target")
            add_edge(rule_arn, ecs_params.get("TaskDefinitionArn"), "schedule-task-definition")

    for project in snapshot.get("codebuild_projects", []):
        add_node(project.get("arn", project.get("name")), "codebuild-project", project.get("name"))

    for bucket in snapshot.get("s3_buckets", []):
        add_node(bucket["Name"], "s3-bucket", bucket["Name"])

    for vpc in snapshot.get("vpcs", []):
        add_node(vpc["VpcId"], "vpc", vpc["VpcId"])

    for subnet in snapshot.get("subnets", []):
        add_node(subnet["SubnetId"], "subnet", subnet["SubnetId"])
        if subnet.get("VpcId"):
            add_edge(subnet["SubnetId"], subnet["VpcId"], "subnet-of-vpc")

    for sg in snapshot.get("security_groups", []):
        if sg.get("GroupId"):
            add_node(sg["GroupId"], "security-group", sg.get("GroupName", sg["GroupId"]))
            if sg.get("VpcId"):
                add_edge(sg["GroupId"], sg["VpcId"], "security-group-in-vpc")

    for db in snapshot.get("rds", {}).get("instances", []):
        arn = db.get("DBInstanceArn") or db.get("DbiResourceId") or db.get("DBInstanceIdentifier")
        add_node(arn, "rds-instance", db.get("DBInstanceIdentifier"))
        subnet_group = db.get("DBSubnetGroup", {})
        if subnet_group.get("DBSubnetGroupName"):
            add_edge(arn, subnet_group["DBSubnetGroupName"], "uses-subnet-group")
        for sg in db.get("VpcSecurityGroups", []):
            if sg.get("VpcSecurityGroupId"):
                add_edge(arn, sg["VpcSecurityGroupId"], "uses-security-group")

    for instance in snapshot.get("ec2_instances", []):
        instance_id = instance.get("InstanceId")
        if not instance_id:
            continue
        name = next((tag.get("Value", "") for tag in instance.get("Tags", []) if tag.get("Key") == "Name"), instance_id)
        add_node(instance_id, "ec2-instance", name)
        if instance.get("VpcId"):
            add_edge(instance_id, instance["VpcId"], "runs-in-vpc")
        if instance.get("SubnetId"):
            add_edge(instance_id, instance["SubnetId"], "uses-subnet")
        for sg in instance.get("SecurityGroups", []):
            if sg.get("GroupId"):
                add_edge(instance_id, sg["GroupId"], "uses-security-group")

    for repo in snapshot.get("git_repositories", []):
        add_node(repo["url"], "git-repository", repo["name"])

    for secret in snapshot["secrets"]:
        add_node(secret["ARN"], "secret", secret["Name"])

    for table in snapshot["dynamodb_tables"]:
        add_node(table["Table"]["TableArn"], "dynamodb", table["Table"]["TableName"])

    for fn in snapshot["lambda_functions"]:
        add_node(fn["FunctionArn"], "lambda", fn["FunctionName"])
        if fn.get("Role"):
            add_edge(fn["FunctionArn"], fn["Role"], "assumes-role")
        for key, value in fn.get("Environment", {}).get("Variables", {}).items():
            if isinstance(value, str) and ":sqs:" in value:
                add_edge(fn["FunctionArn"], value, f"env:{key}")
            if isinstance(value, str) and ":sns:" in value:
                add_edge(fn["FunctionArn"], value, f"env:{key}")
            if isinstance(value, str) and ":secretsmanager:" in value:
                add_edge(fn["FunctionArn"], value, f"env:{key}")
            if isinstance(value, str) and ":dynamodb:" in value:
                add_edge(fn["FunctionArn"], value, f"env:{key}")
        vpc_config = fn.get("VpcConfig") or {}
        for subnet_id in vpc_config.get("SubnetIds", []):
            add_edge(fn["FunctionArn"], subnet_id, "uses-subnet")
        for sg_id in vpc_config.get("SecurityGroupIds", []):
            add_edge(fn["FunctionArn"], sg_id, "uses-security-group")
        for key, value in fn.get("Environment", {}).get("Variables", {}).items():
            for url in _collect_git_urls(value):
                add_edge(fn["FunctionArn"], url, f"env:{key}")

    for mapping in snapshot["lambda_event_source_mappings"]:
        mapping_id = (
            mapping.get("EventSourceMappingArn")
            or mapping.get("UUID")
            or f"{mapping.get('FunctionArn', 'unknown-function')}::mapping::{mapping.get('EventSourceArn', 'unknown-source')}"
        )
        add_node(mapping_id, "lambda-event-source-mapping", mapping.get("UUID") or mapping_id)
        add_edge(mapping_id, mapping.get("FunctionArn"), "invokes-function")
        add_edge(mapping_id, mapping.get("EventSourceArn"), "reads-from")
        add_edge(mapping.get("EventSourceArn"), mapping_id, "feeds-mapping")
        add_edge(mapping.get("FunctionArn"), mapping.get("EventSourceArn"), "event-source-mapping")

    for permission in snapshot["lambda_permissions"]:
        function_name = permission["FunctionName"]
        source_fn = next((fn for fn in snapshot["lambda_functions"] if fn["FunctionName"] == function_name), None)
        if not source_fn:
            continue
        for statement in permission.get("Policy", {}).get("Statement", []):
            principal = statement.get("Principal", {})
            principal_value = principal.get("Service") or principal.get("AWS")
            source_arn = statement.get("Condition", {}).get("ArnLike", {}).get("AWS:SourceArn")
            add_edge(principal_value or "unknown", source_fn["FunctionArn"], "invoke-permission", source_arn=source_arn)

    for api in snapshot["api_gateways"]:
        for method in api["methods"]:
            uri = method.get("integration", {}).get("uri", "")
            if ":lambda:path/" in uri:
                lambda_match = re.search(r"functions/(arn:aws:lambda:[^/]+)/invocations", uri)
                target = lambda_match.group(1) if lambda_match else uri
                add_edge(api["id"], target, f"api-integration:{method['httpMethod']} {method.get('path')}")

    return graph


SIGNAL_COUNT_KEYS = (
    "lambda_functions",
    "lambda_event_source_mappings",
    "lambda_permissions",
    "iam_roles",
    "sqs_queues",
    "sns_topics",
    "secrets",
    "dynamodb_tables",
    "api_gateways",
    "codebuild_projects",
    "s3_buckets",
    "cloudformation_stacks",
    "load_balancers",
    "ec2_instances",
    "security_groups",
    "rds_instances",
    "rds_clusters",
    "git_repositories",
    "ecs_clusters",
    "ecs_services",
    "ecs_task_definitions",
    "ecs_tasks",
    "ecs_scheduled_tasks",
    "ecs_event_history",
)


def signal_resource_count(counts):
    return sum(int(counts.get(key, 0) or 0) for key in SIGNAL_COUNT_KEYS)


def main():
    args = parse_args()
    region = args.region
    source_env = sanitize_name(args.source_env) if args.source_env else ""
    team = sanitize_name(args.team) if args.team else ""
    config = load_transfer_config(args.config)

    source_external_id = args.source_external_id or config_override(config, "source_external_id", "")
    session = session_for(region, args.source_role_arn, external_id=source_external_id)
    sts = session.client("sts")
    lambda_client = session.client("lambda")
    cf_client = session.client("cloudformation")
    elbv2_client = session.client("elbv2")
    ec2_client = session.client("ec2")
    rds_client = session.client("rds")
    ecs_client = session.client("ecs")
    sqs_client = session.client("sqs")
    iam_client = session.client("iam")
    sns_client = session.client("sns")
    apigw_client = session.client("apigateway")
    codebuild_client = session.client("codebuild")
    events_client = session.client("events")
    cloudwatch_client = session.client("cloudwatch")
    cloudtrail_client = session.client("cloudtrail")
    secrets_client = session.client("secretsmanager")
    dynamodb_client = session.client("dynamodb")
    s3_client = session.client("s3")

    identity = sts.get_caller_identity()
    lambda_functions = _list_lambda_functions(lambda_client, source_env, config)
    ecs_snapshot = _list_ecs(ecs_client, source_env, config)
    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "account_id": identity["Account"],
        "region": region,
        "source_env": source_env,
        "team": team,
        "lambda_functions": lambda_functions,
        "lambda_event_source_mappings": _list_lambda_event_source_mappings(lambda_client, lambda_functions, source_env, config),
        "lambda_permissions": _list_lambda_permissions(lambda_client, lambda_functions),
        "iam_roles": _discover_lambda_roles(iam_client, lambda_functions, source_env, config),
        "sqs_queues": _list_sqs_queues(sqs_client, source_env, config),
        "sns_topics": _list_sns_topics(sns_client, source_env, config),
        "secrets": _list_secrets(secrets_client, source_env, config),
        "dynamodb_tables": _list_dynamodb_tables(dynamodb_client, source_env, config),
        "api_gateways": _list_api_gateways(apigw_client, source_env, config),
        "codebuild_projects": _list_codebuild_projects(codebuild_client, source_env, config),
        "s3_buckets": _list_s3_buckets(s3_client, region, source_env, config),
        "cloudformation_stacks": _list_cloudformation_stacks(cf_client, source_env, config),
        "load_balancers": _list_load_balancers(elbv2_client, source_env, config),
        "security_groups": _list_security_groups(ec2_client, source_env, config),
        "vpcs": _list_vpcs(ec2_client),
        "subnets": _list_subnets(ec2_client),
        "route_tables": _list_route_tables(ec2_client),
        "ec2_instances": _list_ec2_instances(ec2_client, source_env, config),
        "rds": _list_rds(rds_client, source_env, config),
        "ecs": ecs_snapshot,
        "ecs_scheduled_tasks": _list_ecs_scheduled_tasks(events_client, source_env, config),
        "ecs_metrics": _list_ecs_metrics(cloudwatch_client, ecs_snapshot),
        "ecs_event_history": _list_ecs_event_history(cloudtrail_client, source_env),
    }
    snapshot["git_repositories"] = _discover_git_repositories(snapshot)
    snapshot["dependency_graph"] = build_dependency_graph(snapshot)

    client_slug = resolve_client_slug(args.client_slug, config, source_env=source_env)
    target_dir = inventory_dir_path(source_env, args.inventory_key, client_slug)
    target_dir.mkdir(parents=True, exist_ok=True)

    snapshot_path = target_dir / "source_snapshot.json"
    summary_path = target_dir / "summary.json"
    graph_path = target_dir / "dependency_graph.json"

    summary = {
        "source_env": source_env,
        "inventory_key": inventory_dir_name(source_env, args.inventory_key),
        "team": team,
        "region": region,
        "account_id": identity["Account"],
        "counts": {
            "lambda_functions": len(snapshot["lambda_functions"]),
            "lambda_event_source_mappings": len(snapshot["lambda_event_source_mappings"]),
            "lambda_permissions": len(snapshot["lambda_permissions"]),
            "iam_roles": len(snapshot["iam_roles"]),
            "sqs_queues": len(snapshot["sqs_queues"]),
            "sns_topics": len(snapshot["sns_topics"]),
            "secrets": len(snapshot["secrets"]),
            "dynamodb_tables": len(snapshot["dynamodb_tables"]),
            "api_gateways": len(snapshot["api_gateways"]),
            "codebuild_projects": len(snapshot["codebuild_projects"]),
            "s3_buckets": len(snapshot["s3_buckets"]),
            "cloudformation_stacks": len(snapshot["cloudformation_stacks"]),
            "load_balancers": len(snapshot["load_balancers"]),
            "ec2_instances": len(snapshot["ec2_instances"]),
            "security_groups": len(snapshot["security_groups"]),
            "vpcs": len(snapshot["vpcs"]),
            "subnets": len(snapshot["subnets"]),
            "route_tables": len(snapshot["route_tables"]),
            "rds_instances": len(snapshot["rds"]["instances"]),
            "rds_clusters": len(snapshot["rds"]["clusters"]),
            "git_repositories": len(snapshot["git_repositories"]),
            "ecs_clusters": len(snapshot["ecs"]["clusters"]),
            "ecs_services": len(snapshot["ecs"]["services"]),
            "ecs_task_definitions": len(snapshot["ecs"]["task_definitions"]),
            "ecs_tasks": len(snapshot["ecs"]["tasks"]),
            "ecs_scheduled_tasks": len(snapshot["ecs_scheduled_tasks"]),
            "ecs_event_history": len(snapshot["ecs_event_history"]),
            "dependency_nodes": len(snapshot["dependency_graph"]["nodes"]),
            "dependency_edges": len(snapshot["dependency_graph"]["edges"]),
        },
    }
    summary["signal_resource_count"] = signal_resource_count(summary["counts"])
    summary["has_signal_resources"] = summary["signal_resource_count"] > 0

    sanitized_snapshot = sanitize_snapshot_value(snapshot)
    sanitized_graph = sanitized_snapshot["dependency_graph"]

    snapshot_path.write_text(json.dumps(sanitized_snapshot, indent=2, default=str), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    graph_path.write_text(json.dumps(sanitized_graph, indent=2, default=str), encoding="utf-8")
    append_audit_event(
        "discover_aws_environment",
        "ok",
        {"snapshot_path": str(snapshot_path), "summary_path": str(summary_path), "graph_path": str(graph_path)},
        source_env=source_env,
        client_slug=client_slug,
    )

    print(json.dumps({
        "status": "ok",
        "client_slug": client_slug,
        "snapshot_path": str(snapshot_path),
        "summary_path": str(summary_path),
        "graph_path": str(graph_path),
        "summary": summary,
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
