import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def sanitize_name(value):
    cleaned = re.sub(r"[^a-zA-Z0-9-_]+", "-", value.strip().lower())
    return re.sub(r"-{2,}", "-", cleaned).strip("-_")


def parse_args():
    parser = argparse.ArgumentParser(description="Discover AWS resources for a source environment.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--team", default="")
    parser.add_argument("--region", default="us-east-1")
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


def _list_lambda_functions(lambda_client, source_env):
    paginator = lambda_client.get_paginator("list_functions")
    items = []
    for page in paginator.paginate():
        for fn in page.get("Functions", []):
            if _matches_source(fn["FunctionName"], source_env):
                items.append(fn)
    return items


def _list_cloudformation_stacks(cf_client, source_env):
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
                items.append(stack)
    return items


def _list_load_balancers(elbv2_client, source_env):
    paginator = elbv2_client.get_paginator("describe_load_balancers")
    items = []
    for page in paginator.paginate():
        for lb in page.get("LoadBalancers", []):
            if _matches_source(lb["LoadBalancerName"], source_env):
                items.append(lb)
    return items


def _list_security_groups(ec2_client, source_env):
    paginator = ec2_client.get_paginator("describe_security_groups")
    items = []
    for page in paginator.paginate():
        for sg in page.get("SecurityGroups", []):
            if _matches_source(sg.get("GroupName", ""), source_env):
                items.append(sg)
    return items


def _list_ecs(ecs_client, source_env):
    clusters = []
    cluster_arns = ecs_client.list_clusters().get("clusterArns", [])
    if cluster_arns:
        described = ecs_client.describe_clusters(clusters=cluster_arns).get("clusters", [])
        clusters = [cluster for cluster in described if _matches_source(cluster["clusterName"], source_env)]

    services = []
    task_definitions = []
    for cluster in clusters:
        service_arns = ecs_client.list_services(cluster=cluster["clusterArn"]).get("serviceArns", [])
        if service_arns:
            described_services = ecs_client.describe_services(cluster=cluster["clusterArn"], services=service_arns).get("services", [])
            for service in described_services:
                if _matches_source(service["serviceName"], source_env):
                    services.append(service)
                    task_def_arn = service.get("taskDefinition")
                    if task_def_arn:
                        task_definitions.append(ecs_client.describe_task_definition(taskDefinition=task_def_arn)["taskDefinition"])

    return {
        "clusters": clusters,
        "services": services,
        "task_definitions": task_definitions,
    }


def _list_sqs_queues(sqs_client, source_env):
    queue_urls = sqs_client.list_queues().get("QueueUrls", [])
    items = []
    for url in queue_urls:
        queue_name = url.rstrip("/").split("/")[-1]
        if not _matches_source(queue_name, source_env):
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


def _list_lambda_event_source_mappings(lambda_client, lambda_functions, source_env):
    mappings = []
    for fn in lambda_functions:
        function_name = fn["FunctionName"]
        response = lambda_client.list_event_source_mappings(FunctionName=function_name)
        for item in response.get("EventSourceMappings", []):
            source_arn = item.get("EventSourceArn", "")
            if source_arn and source_env and source_env not in source_arn and source_env not in function_name.lower():
                if not _matches_source(source_arn.split(":")[-1], source_env):
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


def _discover_lambda_roles(iam_client, lambda_functions, source_env):
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


def _list_sns_topics(sns_client, source_env):
    topics = []
    paginator = sns_client.get_paginator("list_topics")
    for page in paginator.paginate():
        for topic in page.get("Topics", []):
            arn = topic["TopicArn"]
            name = arn.split(":")[-1]
            if not _matches_source(name, source_env):
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


def _list_api_gateways(apigw_client, source_env):
    rest_apis = []
    paginator = apigw_client.get_paginator("get_rest_apis")
    for page in paginator.paginate():
        for api in page.get("items", []):
            name = api.get("name", "")
            if not _matches_source(name, source_env):
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
                        "integration": integration,
                    })
            rest_apis.append({
                "id": api["id"],
                "name": name,
                "description": api.get("description", ""),
                "resources": resources,
                "methods": methods,
            })
    return rest_apis


def build_dependency_graph(snapshot):
    graph = {"nodes": [], "edges": []}

    for queue in snapshot["sqs_queues"]:
        queue_arn = queue.get("QueueArn") or queue.get("Attributes", {}).get("QueueArn")
        if not queue_arn:
            continue
        graph["nodes"].append({"id": queue_arn, "type": "sqs", "name": queue["QueueName"]})

    for role in snapshot["iam_roles"]:
        graph["nodes"].append({"id": role["Arn"], "type": "iam-role", "name": role["RoleName"]})

    for topic in snapshot["sns_topics"]:
        graph["nodes"].append({"id": topic["TopicArn"], "type": "sns-topic", "name": topic["TopicName"]})

    for api in snapshot["api_gateways"]:
        graph["nodes"].append({"id": api["id"], "type": "api-gateway", "name": api["name"]})

    for fn in snapshot["lambda_functions"]:
        graph["nodes"].append({"id": fn["FunctionArn"], "type": "lambda", "name": fn["FunctionName"]})
        if fn.get("Role"):
            graph["edges"].append({
                "from": fn["FunctionArn"],
                "to": fn["Role"],
                "relationship": "assumes-role",
            })
        for key, value in fn.get("Environment", {}).get("Variables", {}).items():
            if isinstance(value, str) and ":sqs:" in value:
                graph["edges"].append({
                    "from": fn["FunctionArn"],
                    "to": value,
                    "relationship": f"env:{key}",
                })
            if isinstance(value, str) and ":sns:" in value:
                graph["edges"].append({
                    "from": fn["FunctionArn"],
                    "to": value,
                    "relationship": f"env:{key}",
                })

    for mapping in snapshot["lambda_event_source_mappings"]:
        graph["edges"].append({
            "from": mapping.get("FunctionArn"),
            "to": mapping.get("EventSourceArn"),
            "relationship": "event-source-mapping",
        })

    for permission in snapshot["lambda_permissions"]:
        function_name = permission["FunctionName"]
        source_fn = next((fn for fn in snapshot["lambda_functions"] if fn["FunctionName"] == function_name), None)
        if not source_fn:
            continue
        for statement in permission.get("Policy", {}).get("Statement", []):
            principal = statement.get("Principal", {})
            principal_value = principal.get("Service") or principal.get("AWS")
            source_arn = statement.get("Condition", {}).get("ArnLike", {}).get("AWS:SourceArn")
            graph["edges"].append({
                "from": principal_value or "unknown",
                "to": source_fn["FunctionArn"],
                "relationship": "invoke-permission",
                "source_arn": source_arn,
            })

    for api in snapshot["api_gateways"]:
        for method in api["methods"]:
            uri = method.get("integration", {}).get("uri", "")
            if ":lambda:path/" in uri:
                graph["edges"].append({
                    "from": api["id"],
                    "to": uri,
                    "relationship": f"api-integration:{method['httpMethod']} {method.get('path')}",
                })

    return graph


def main():
    args = parse_args()
    region = args.region
    source_env = sanitize_name(args.source_env) if args.source_env else ""
    team = sanitize_name(args.team) if args.team else ""

    session = boto3.session.Session(region_name=region)
    sts = session.client("sts")
    lambda_client = session.client("lambda")
    cf_client = session.client("cloudformation")
    elbv2_client = session.client("elbv2")
    ec2_client = session.client("ec2")
    ecs_client = session.client("ecs")
    sqs_client = session.client("sqs")
    iam_client = session.client("iam")
    sns_client = session.client("sns")
    apigw_client = session.client("apigateway")

    identity = sts.get_caller_identity()
    lambda_functions = _list_lambda_functions(lambda_client, source_env)
    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "account_id": identity["Account"],
        "region": region,
        "source_env": source_env,
        "team": team,
        "lambda_functions": lambda_functions,
        "lambda_event_source_mappings": _list_lambda_event_source_mappings(lambda_client, lambda_functions, source_env),
        "lambda_permissions": _list_lambda_permissions(lambda_client, lambda_functions),
        "iam_roles": _discover_lambda_roles(iam_client, lambda_functions, source_env),
        "sqs_queues": _list_sqs_queues(sqs_client, source_env),
        "sns_topics": _list_sns_topics(sns_client, source_env),
        "api_gateways": _list_api_gateways(apigw_client, source_env),
        "cloudformation_stacks": _list_cloudformation_stacks(cf_client, source_env),
        "load_balancers": _list_load_balancers(elbv2_client, source_env),
        "security_groups": _list_security_groups(ec2_client, source_env),
        "ecs": _list_ecs(ecs_client, source_env),
    }
    snapshot["dependency_graph"] = build_dependency_graph(snapshot)

    base_dir = Path("state") / "aws_inventory"
    target_dir = base_dir / (source_env or "full-account-scan")
    target_dir.mkdir(parents=True, exist_ok=True)

    snapshot_path = target_dir / "source_snapshot.json"
    summary_path = target_dir / "summary.json"
    graph_path = target_dir / "dependency_graph.json"

    summary = {
        "source_env": source_env,
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
            "api_gateways": len(snapshot["api_gateways"]),
            "cloudformation_stacks": len(snapshot["cloudformation_stacks"]),
            "load_balancers": len(snapshot["load_balancers"]),
            "security_groups": len(snapshot["security_groups"]),
            "ecs_clusters": len(snapshot["ecs"]["clusters"]),
            "ecs_services": len(snapshot["ecs"]["services"]),
            "ecs_task_definitions": len(snapshot["ecs"]["task_definitions"]),
            "dependency_nodes": len(snapshot["dependency_graph"]["nodes"]),
            "dependency_edges": len(snapshot["dependency_graph"]["edges"]),
        },
    }

    snapshot_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    graph_path.write_text(json.dumps(snapshot["dependency_graph"], indent=2), encoding="utf-8")

    print(json.dumps({
        "status": "ok",
        "snapshot_path": str(snapshot_path),
        "summary_path": str(summary_path),
        "graph_path": str(graph_path),
        "summary": summary,
    }, indent=2))


if __name__ == "__main__":
    main()
