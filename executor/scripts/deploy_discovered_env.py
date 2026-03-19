import argparse
import json
import re
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def sanitize_name(value):
    cleaned = re.sub(r"[^a-zA-Z0-9-_]+", "-", value.strip().lower())
    return re.sub(r"-{2,}", "-", cleaned).strip("-_")


def parse_args():
    parser = argparse.ArgumentParser(description="Deploy a new environment from a discovered snapshot.")
    parser.add_argument("--source-env", required=True)
    parser.add_argument("--target-env", required=True)
    parser.add_argument("--team", default="")
    parser.add_argument("--region", default="us-east-1")
    return parser.parse_args()


def load_snapshot(source_env):
    snapshot_path = Path("state") / "aws_inventory" / sanitize_name(source_env) / "source_snapshot.json"
    if not snapshot_path.exists():
        raise FileNotFoundError(f"Snapshot not found: {snapshot_path}")
    return snapshot_path, json.loads(snapshot_path.read_text(encoding="utf-8"))


def target_name(original_name, source_env, target_env, team):
    sanitized_source = sanitize_name(source_env)
    sanitized_target = sanitize_name(target_env)
    sanitized_team = sanitize_name(team) if team else ""
    updated = original_name
    if sanitized_source and sanitized_source in updated:
        updated = updated.replace(sanitized_source, sanitized_target, 1)
    else:
        updated = f"{sanitized_target}-{updated}"
    if sanitized_team and sanitized_team not in updated:
        updated = f"{updated}-{sanitized_team}"
    return updated[:64]


def queue_target_name(queue_name, source_env, target_env, team):
    suffix = ".fifo" if queue_name.endswith(".fifo") else ""
    base_name = queue_name[:-5] if suffix else queue_name
    return f"{target_name(base_name, source_env, target_env, team)}{suffix}"[:80]


def role_name_from_arn(role_arn):
    return role_arn.split("/")[-1]


def function_name_from_arn(function_arn):
    return function_arn.split(":")[-1]


def should_skip_recloning(resource_name, target_env, team):
    target_prefix = sanitize_name(target_env)
    team_suffix = f"-{sanitize_name(team)}" if team else ""
    lowered = resource_name.lower()
    if lowered.startswith(f"{target_prefix}-"):
        return True
    if team_suffix and lowered.endswith(team_suffix):
        return True
    return False


def rewrite_string_value(value, mappings, source_env, target_env, team):
    if not isinstance(value, str):
        return value
    updated = value
    for original, replacement in mappings.get("queue_urls", {}).items():
        updated = updated.replace(original, replacement)
    for original, replacement in mappings.get("queue_arns", {}).items():
        updated = updated.replace(original, replacement)
    for original, replacement in mappings.get("topic_arns", {}).items():
        updated = updated.replace(original, replacement)
    for original, replacement in mappings.get("function_arns", {}).items():
        updated = updated.replace(original, replacement)
    for original, replacement in mappings.get("role_arns", {}).items():
        updated = updated.replace(original, replacement)
    if source_env:
        updated = updated.replace(source_env, target_env)
    if team:
        updated = updated.replace("{team}", team)
    return updated


def update_env_values(variables, mappings, source_env, target_env, team):
    return {
        key: rewrite_string_value(value, mappings, source_env, target_env, team)
        for key, value in variables.items()
    }


def download_lambda_zip(location_url):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        with urllib.request.urlopen(location_url, timeout=60) as response:
            tmp.write(response.read())
        return Path(tmp.name)


def ensure_zip_is_readable(zip_path):
    with zipfile.ZipFile(zip_path, "r") as archive:
        archive.testzip()


def role_allows_lambda_assume(role_document):
    for statement in role_document.get("Statement", []):
        principal = statement.get("Principal", {})
        service = principal.get("Service")
        services = service if isinstance(service, list) else [service]
        if statement.get("Effect") != "Allow":
            continue
        if "sts:AssumeRole" not in str(statement.get("Action", "")):
            continue
        if any(item == "lambda.amazonaws.com" for item in services if item):
            return True
    return False


def trim_create_payload(create_payload):
    payload = dict(create_payload)
    for key in ["PackageType", "Architectures", "EphemeralStorage"]:
        if not payload.get(key):
            payload.pop(key, None)
    if not payload.get("Environment", {}).get("Variables"):
        payload.pop("Environment", None)
    return payload


def create_or_update_roles(snapshot, source_env, target_env, team, iam_client):
    mappings = {"role_arns": {}, "role_names": {}}
    deployed = []
    failed = []
    for role in snapshot.get("iam_roles", []):
        source_role_name = role["RoleName"]
        if should_skip_recloning(source_role_name, target_env, team):
            continue
        target_role_name = target_name(source_role_name, source_env, target_env, team)
        try:
            try:
                response = iam_client.create_role(
                    RoleName=target_role_name,
                    AssumeRolePolicyDocument=json.dumps(role.get("AssumeRolePolicyDocument", {})),
                    Description=f"Cloned from {source_role_name}",
                    Path=role.get("Path", "/"),
                )
                target_role_arn = response["Role"]["Arn"]
                operation = "created"
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "EntityAlreadyExists":
                    raise
                target_role_arn = iam_client.get_role(RoleName=target_role_name)["Role"]["Arn"]
                iam_client.update_assume_role_policy(
                    RoleName=target_role_name,
                    PolicyDocument=json.dumps(role.get("AssumeRolePolicyDocument", {})),
                )
                operation = "updated"
            attached = iam_client.list_attached_role_policies(RoleName=target_role_name).get("AttachedPolicies", [])
            attached_arns = {item["PolicyArn"] for item in attached}
            for policy in role.get("ManagedPolicies", []):
                if policy["PolicyArn"] not in attached_arns:
                    iam_client.attach_role_policy(RoleName=target_role_name, PolicyArn=policy["PolicyArn"])
            for policy in role.get("InlinePolicies", []):
                iam_client.put_role_policy(
                    RoleName=target_role_name,
                    PolicyName=policy["PolicyName"],
                    PolicyDocument=json.dumps(policy["PolicyDocument"]),
                )
            mappings["role_arns"][role["Arn"]] = target_role_arn
            mappings["role_names"][source_role_name] = target_role_name
            deployed.append({
                "source_role": source_role_name,
                "target_role": target_role_name,
                "target_role_arn": target_role_arn,
                "operation": operation,
            })
        except Exception as exc:
            failed.append({"source_role": source_role_name, "target_role": target_role_name, "error": str(exc)})
    return mappings, deployed, failed


def create_or_update_sqs_queues(snapshot, source_env, target_env, team, sqs_client):
    mappings = {"queue_urls": {}, "queue_arns": {}, "queue_names": {}}
    deployed = []
    failed = []
    allowed_attributes = {
        "DelaySeconds", "MaximumMessageSize", "MessageRetentionPeriod", "Policy",
        "ReceiveMessageWaitTimeSeconds", "RedrivePolicy", "RedriveAllowPolicy",
        "VisibilityTimeout", "FifoQueue", "ContentBasedDeduplication",
        "DeduplicationScope", "FifoThroughputLimit", "KmsMasterKeyId",
        "KmsDataKeyReusePeriodSeconds", "SqsManagedSseEnabled",
    }
    for queue in snapshot.get("sqs_queues", []):
        source_name = queue["QueueName"]
        if should_skip_recloning(source_name, target_env, team):
            continue
        target_queue_name = queue_target_name(source_name, source_env, target_env, team)
        attributes = {k: v for k, v in queue.get("Attributes", {}).items() if k in allowed_attributes}
        try:
            response = sqs_client.create_queue(QueueName=target_queue_name, Attributes=attributes, tags=queue.get("Tags", {}))
            queue_url = response["QueueUrl"]
            operation = "created"
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "QueueAlreadyExists":
                failed.append({"source_queue": source_name, "target_queue": target_queue_name, "error": str(exc)})
                continue
            queue_url = sqs_client.get_queue_url(QueueName=target_queue_name)["QueueUrl"]
            if attributes:
                sqs_client.set_queue_attributes(QueueUrl=queue_url, Attributes=attributes)
            operation = "updated"
        queue_arn = sqs_client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        mappings["queue_urls"][queue["QueueUrl"]] = queue_url
        mappings["queue_arns"][queue["Attributes"]["QueueArn"]] = queue_arn
        mappings["queue_names"][source_name] = target_queue_name
        deployed.append({
            "source_queue": source_name,
            "target_queue": target_queue_name,
            "target_queue_url": queue_url,
            "target_queue_arn": queue_arn,
            "operation": operation,
        })
    return mappings, deployed, failed


def create_or_update_sns_topics(snapshot, source_env, target_env, team, sns_client):
    mappings = {"topic_arns": {}, "topic_names": {}}
    deployed = []
    failed = []
    for topic in snapshot.get("sns_topics", []):
        source_name = topic["TopicName"]
        if should_skip_recloning(source_name, target_env, team):
            continue
        target_topic_name = target_name(source_name, source_env, target_env, team)
        attributes = {}
        if topic.get("Attributes", {}).get("FifoTopic") == "true":
            if not target_topic_name.endswith(".fifo"):
                target_topic_name = f"{target_topic_name}.fifo"
            attributes["FifoTopic"] = "true"
        try:
            response = sns_client.create_topic(Name=target_topic_name, Attributes=attributes)
            target_topic_arn = response["TopicArn"]
            mappings["topic_arns"][topic["TopicArn"]] = target_topic_arn
            mappings["topic_names"][source_name] = target_topic_name
            deployed.append({
                "source_topic": source_name,
                "target_topic": target_topic_name,
                "target_topic_arn": target_topic_arn,
                "operation": "created_or_verified",
            })
        except Exception as exc:
            failed.append({"source_topic": source_name, "target_topic": target_topic_name, "error": str(exc)})
    return mappings, deployed, failed


def resolve_execution_role(snapshot, resource_mappings, source_role_arn):
    if source_role_arn in resource_mappings["role_arns"]:
        return resource_mappings["role_arns"][source_role_arn], resource_mappings["role_arns"][source_role_arn] != source_role_arn
    for role in snapshot.get("iam_roles", []):
        if role["Arn"] == source_role_arn and role_allows_lambda_assume(role.get("AssumeRolePolicyDocument", {})):
            return source_role_arn, False
    fallback_arn = next(iter(resource_mappings["role_arns"].values()), source_role_arn)
    return fallback_arn, fallback_arn != source_role_arn


def deploy_lambda_functions(snapshot, source_env, target_env, team, session, resource_mappings):
    lambda_client = session.client("lambda")
    deployed = []
    failed = []
    for source_fn in snapshot.get("lambda_functions", []):
        source_name = source_fn["FunctionName"]
        if should_skip_recloning(source_name, target_env, team):
            continue
        target_fn = target_name(source_name, source_env, target_env, team)
        try:
            function_details = lambda_client.get_function(FunctionName=source_name)
            configuration = function_details["Configuration"]
            zip_path = download_lambda_zip(function_details["Code"]["Location"])
            ensure_zip_is_readable(zip_path)
            zip_bytes = zip_path.read_bytes()
            environment = update_env_values(
                configuration.get("Environment", {}).get("Variables", {}),
                resource_mappings,
                sanitize_name(source_env),
                sanitize_name(target_env),
                sanitize_name(team),
            )
            role_arn, fallback_used = resolve_execution_role(snapshot, resource_mappings, configuration.get("Role"))
            create_payload = trim_create_payload({
                "FunctionName": target_fn,
                "Runtime": configuration.get("Runtime"),
                "Role": role_arn,
                "Handler": configuration.get("Handler"),
                "Code": {"ZipFile": zip_bytes},
                "Description": f"Cloned from {source_name}",
                "Timeout": configuration.get("Timeout", 3),
                "MemorySize": configuration.get("MemorySize", 128),
                "Publish": False,
                "Environment": {"Variables": environment},
                "PackageType": configuration.get("PackageType", "Zip"),
                "Architectures": configuration.get("Architectures", ["x86_64"]),
                "EphemeralStorage": configuration.get("EphemeralStorage", {"Size": 512}),
            })
            vpc_config = configuration.get("VpcConfig") or {}
            if vpc_config.get("SubnetIds") and vpc_config.get("SecurityGroupIds"):
                create_payload["VpcConfig"] = {"SubnetIds": vpc_config["SubnetIds"], "SecurityGroupIds": vpc_config["SecurityGroupIds"]}
            try:
                lambda_client.create_function(**create_payload)
                operation = "created"
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceConflictException":
                    raise
                lambda_client.update_function_code(FunctionName=target_fn, ZipFile=zip_bytes, Publish=False)
                lambda_client.get_waiter("function_updated").wait(FunctionName=target_fn)
                update_kwargs = {
                    "FunctionName": target_fn,
                    "Role": role_arn,
                    "Handler": configuration.get("Handler"),
                    "Description": f"Cloned from {source_name}",
                    "Timeout": configuration.get("Timeout", 3),
                    "MemorySize": configuration.get("MemorySize", 128),
                    "Environment": {"Variables": environment},
                }
                if "VpcConfig" in create_payload:
                    update_kwargs["VpcConfig"] = create_payload["VpcConfig"]
                lambda_client.update_function_configuration(**update_kwargs)
                operation = "updated"
            lambda_client.get_waiter("function_active_v2").wait(FunctionName=target_fn)
            lambda_client.get_waiter("function_updated").wait(FunctionName=target_fn)
            target_config = lambda_client.get_function(FunctionName=target_fn)["Configuration"]
            resource_mappings["function_arns"][source_fn["FunctionArn"]] = target_config["FunctionArn"]
            resource_mappings["function_names"][source_name] = target_fn
            deployed.append({
                "source_function": source_name,
                "target_function": target_fn,
                "target_function_arn": target_config["FunctionArn"],
                "operation": operation,
                "execution_role": role_arn,
                "fallback_role_used": fallback_used,
            })
        except Exception as exc:
            failed.append({"source_function": source_name, "target_function": target_fn, "error": str(exc)})
    return deployed, failed


def create_event_source_mappings(snapshot, lambda_client, resource_mappings):
    deployed = []
    failed = []
    for mapping in snapshot.get("lambda_event_source_mappings", []):
        source_function_name = function_name_from_arn(mapping["FunctionArn"])
        if should_skip_recloning(source_function_name, resource_mappings.get("target_env", ""), resource_mappings.get("team", "")):
            continue
        target_function_name = resource_mappings["function_names"].get(source_function_name)
        target_source_arn = resource_mappings["queue_arns"].get(mapping.get("EventSourceArn"), mapping.get("EventSourceArn"))
        if not target_function_name or not target_source_arn:
            failed.append({"source_mapping": mapping.get("UUID"), "error": "Missing target function or event source"})
            continue
        params = {
            "FunctionName": target_function_name,
            "EventSourceArn": target_source_arn,
            "Enabled": mapping.get("State") != "Disabled",
            "BatchSize": mapping.get("BatchSize", 10),
        }
        if mapping.get("MaximumBatchingWindowInSeconds") is not None:
            params["MaximumBatchingWindowInSeconds"] = mapping["MaximumBatchingWindowInSeconds"]
        try:
            lambda_client.create_event_source_mapping(**params)
            deployed.append({"source_uuid": mapping.get("UUID"), "target_function": target_function_name, "target_event_source_arn": target_source_arn, "operation": "created"})
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceConflictException":
                deployed.append({"source_uuid": mapping.get("UUID"), "target_function": target_function_name, "target_event_source_arn": target_source_arn, "operation": "existing"})
            else:
                failed.append({"source_uuid": mapping.get("UUID"), "target_function": target_function_name, "error": str(exc)})
    return deployed, failed


def apply_lambda_permissions(snapshot, lambda_client, resource_mappings, source_env, target_env, team):
    deployed = []
    failed = []
    for permission in snapshot.get("lambda_permissions", []):
        if should_skip_recloning(permission["FunctionName"], target_env, team):
            continue
        target_function_name = resource_mappings["function_names"].get(permission["FunctionName"])
        if not target_function_name:
            continue
        for index, statement in enumerate(permission.get("Policy", {}).get("Statement", []), start=1):
            principal = statement.get("Principal", {})
            if not principal:
                continue
            params = {
                "FunctionName": target_function_name,
                "StatementId": f"cloned-{index}-{sanitize_name(target_function_name)[:40]}",
                "Action": statement.get("Action", "lambda:InvokeFunction"),
                "Principal": principal.get("Service") or principal.get("AWS"),
            }
            source_arn = statement.get("Condition", {}).get("ArnLike", {}).get("AWS:SourceArn")
            if source_arn:
                params["SourceArn"] = rewrite_string_value(source_arn, resource_mappings, sanitize_name(source_env), sanitize_name(target_env), sanitize_name(team))
            try:
                lambda_client.add_permission(**params)
                deployed.append({"target_function": target_function_name, "statement_id": params["StatementId"], "principal": params["Principal"]})
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceConflictException":
                    failed.append({"target_function": target_function_name, "statement_id": params["StatementId"], "error": str(exc)})
    return deployed, failed


def create_sns_subscriptions(snapshot, sns_client, resource_mappings):
    deployed = []
    failed = []
    for topic in snapshot.get("sns_topics", []):
        if should_skip_recloning(topic["TopicName"], resource_mappings.get("target_env", ""), resource_mappings.get("team", "")):
            continue
        target_topic_arn = resource_mappings["topic_arns"].get(topic["TopicArn"])
        if not target_topic_arn:
            continue
        for subscription in topic.get("Subscriptions", []):
            protocol = subscription.get("Protocol")
            endpoint = subscription.get("Endpoint")
            if protocol == "lambda":
                endpoint = resource_mappings["function_arns"].get(endpoint, endpoint)
            elif protocol == "sqs":
                endpoint = resource_mappings["queue_arns"].get(endpoint, endpoint)
            try:
                response = sns_client.subscribe(TopicArn=target_topic_arn, Protocol=protocol, Endpoint=endpoint)
                deployed.append({"target_topic_arn": target_topic_arn, "protocol": protocol, "endpoint": endpoint, "subscription_arn": response.get("SubscriptionArn")})
            except Exception as exc:
                failed.append({"target_topic_arn": target_topic_arn, "protocol": protocol, "endpoint": endpoint, "error": str(exc)})
    return deployed, failed


def create_api_gateways(snapshot, apigw_client, resource_mappings, source_env, target_env, team):
    deployed = []
    failed = []
    api_ids = {}
    for api in snapshot.get("api_gateways", []):
        source_api_name = api["name"]
        if should_skip_recloning(source_api_name, target_env, team):
            continue
        target_api_name = target_name(source_api_name, source_env, target_env, team)
        try:
            created = apigw_client.create_rest_api(name=target_api_name, description=f"Cloned from {source_api_name}")
            api_ids[api["id"]] = created["id"]
            deployed.append({"source_api": source_api_name, "target_api": target_api_name, "target_api_id": created["id"], "operation": "created"})
        except Exception as exc:
            failed.append({"source_api": source_api_name, "target_api": target_api_name, "error": str(exc)})
    return api_ids, deployed, failed


def main():
    args = parse_args()
    snapshot_path, snapshot = load_snapshot(args.source_env)
    session = boto3.session.Session(region_name=args.region)
    iam_client = session.client("iam")
    sqs_client = session.client("sqs")
    sns_client = session.client("sns")
    lambda_client = session.client("lambda")
    apigw_client = session.client("apigateway")

    deployment_dir = Path("state") / "deployments" / sanitize_name(args.target_env)
    deployment_dir.mkdir(parents=True, exist_ok=True)

    resource_mappings = {
        "role_arns": {}, "role_names": {}, "queue_urls": {}, "queue_arns": {}, "queue_names": {},
        "topic_arns": {}, "topic_names": {}, "function_arns": {}, "function_names": {}, "api_ids": {},
        "target_env": sanitize_name(args.target_env), "team": sanitize_name(args.team) if args.team else "",
    }
    role_mappings, deployed_roles, failed_roles = create_or_update_roles(snapshot, args.source_env, args.target_env, args.team, iam_client)
    resource_mappings.update(role_mappings)
    queue_mappings, deployed_queues, failed_queues = create_or_update_sqs_queues(snapshot, args.source_env, args.target_env, args.team, sqs_client)
    resource_mappings.update(queue_mappings)
    topic_mappings, deployed_topics, failed_topics = create_or_update_sns_topics(snapshot, args.source_env, args.target_env, args.team, sns_client)
    resource_mappings.update(topic_mappings)
    deployed_lambdas, failed_lambdas = deploy_lambda_functions(snapshot, args.source_env, args.target_env, args.team, session, resource_mappings)
    deployed_event_mappings, failed_event_mappings = create_event_source_mappings(snapshot, lambda_client, resource_mappings)
    deployed_permissions, failed_permissions = apply_lambda_permissions(snapshot, lambda_client, resource_mappings, args.source_env, args.target_env, args.team)
    deployed_subscriptions, failed_subscriptions = create_sns_subscriptions(snapshot, sns_client, resource_mappings)
    api_ids, deployed_apis, failed_apis = create_api_gateways(snapshot, apigw_client, resource_mappings, args.source_env, args.target_env, args.team)
    resource_mappings["api_ids"] = api_ids

    failures = {
        "roles": failed_roles,
        "queues": failed_queues,
        "topics": failed_topics,
        "lambda_functions": failed_lambdas,
        "event_source_mappings": failed_event_mappings,
        "lambda_permissions": failed_permissions,
        "sns_subscriptions": failed_subscriptions,
        "api_gateways": failed_apis,
    }
    total_failures = sum(len(items) for items in failures.values())

    manifest = {
        "source_snapshot": str(snapshot_path),
        "source_env": sanitize_name(args.source_env),
        "target_env": sanitize_name(args.target_env),
        "team": sanitize_name(args.team) if args.team else "",
        "region": args.region,
        "roles": deployed_roles,
        "sqs_queues": deployed_queues,
        "sns_topics": deployed_topics,
        "lambda_functions": deployed_lambdas,
        "lambda_event_source_mappings": deployed_event_mappings,
        "lambda_permissions": deployed_permissions,
        "sns_subscriptions": deployed_subscriptions,
        "api_gateways": deployed_apis,
        "resource_mappings": resource_mappings,
        "failures": failures,
        "follow_up": {
            "manual_review_required": total_failures > 0,
            "cloudformation_stacks_review_required": len(snapshot.get("cloudformation_stacks", [])) > 0,
            "load_balancer_review_required": len(snapshot.get("load_balancers", [])) > 0,
            "ecs_review_required": len(snapshot.get("ecs", {}).get("services", [])) > 0,
        },
    }

    manifest_path = deployment_dir / "deployment_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": "ok" if total_failures == 0 else "partial",
        "manifest_path": str(manifest_path),
        "deployed_role_count": len(deployed_roles),
        "deployed_queue_count": len(deployed_queues),
        "deployed_topic_count": len(deployed_topics),
        "deployed_lambda_count": len(deployed_lambdas),
        "deployed_mapping_count": len(deployed_event_mappings),
        "deployed_permission_count": len(deployed_permissions),
        "deployed_subscription_count": len(deployed_subscriptions),
        "deployed_api_count": len(deployed_apis),
        "failed_resource_count": total_failures,
    }, indent=2))


if __name__ == "__main__":
    main()
