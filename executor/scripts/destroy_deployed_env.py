import argparse
import json
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from botocore.exceptions import ClientError

from executor.scripts.transfer_common import deployment_dir_path, load_transfer_config, resolve_client_slug, session_for, should_exclude


def sanitize_name(value):
    return value.strip().lower().replace(" ", "-")


def parse_args():
    parser = argparse.ArgumentParser(description="Destroy a deployed cloned environment.")
    parser.add_argument("--target-env", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--config", default="")
    parser.add_argument("--target-role-arn", default="")
    parser.add_argument("--target-external-id", default="")
    parser.add_argument("--deployment-key", default="")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def load_manifest(target_env, deployment_key="", client_slug=""):
    manifest_path = deployment_dir_path(target_env, deployment_key, client_slug) / "deployment_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Deployment manifest not found: {manifest_path}")
    return manifest_path, json.loads(manifest_path.read_text(encoding="utf-8"))


def safe_delete(callable_obj, deleted, failed, payload):
    try:
        callable_obj()
        deleted.append(payload)
    except Exception as exc:
        failed.append({**payload, "error": str(exc)})


def detach_role_policies(iam_client, role_name):
    attached = iam_client.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", [])
    for policy in attached:
        iam_client.detach_role_policy(RoleName=role_name, PolicyArn=policy["PolicyArn"])
    inline_names = iam_client.list_role_policies(RoleName=role_name).get("PolicyNames", [])
    for policy_name in inline_names:
        iam_client.delete_role_policy(RoleName=role_name, PolicyName=policy_name)


def should_delete_network_resource(item):
    return item.get("operation") == "created"


def find_event_source_mapping_uuid(lambda_client, function_name, event_source_arn):
    paginator = lambda_client.get_paginator("list_event_source_mappings")
    for page in paginator.paginate(FunctionName=function_name):
        for item in page.get("EventSourceMappings", []):
            if item.get("EventSourceArn") == event_source_arn:
                return item.get("UUID")
    return None


def wait_for_ecs_service_inactive(ecs_client, cluster_arn, service_name, attempts=24, delay_seconds=5):
    for _ in range(attempts):
        described = ecs_client.describe_services(cluster=cluster_arn, services=[service_name]).get("services", [])
        if not described or described[0].get("status") == "INACTIVE":
            return
        time.sleep(delay_seconds)


def delete_route_table(ec2_client, route_table_id):
    described = ec2_client.describe_route_tables(RouteTableIds=[route_table_id]).get("RouteTables", [])
    if not described:
        return
    associations = described[0].get("Associations", [])
    for association in associations:
        association_id = association.get("RouteTableAssociationId")
        if association.get("Main") or not association_id:
            continue
        ec2_client.disassociate_route_table(AssociationId=association_id)
    ec2_client.delete_route_table(RouteTableId=route_table_id)


def main():
    args = parse_args()
    config = load_transfer_config(args.config)
    client_slug = resolve_client_slug(args.client_slug, config, target_env=args.target_env)
    manifest_path, manifest = load_manifest(args.target_env, args.deployment_key, client_slug)
    target_external_id = args.target_external_id or config.get("overrides", {}).get("target_external_id", "")
    session = session_for(args.region, args.target_role_arn, external_id=target_external_id)
    lambda_client = session.client("lambda")
    sqs_client = session.client("sqs")
    sns_client = session.client("sns")
    apigw_client = session.client("apigateway")
    dynamodb_client = session.client("dynamodb")
    secrets_client = session.client("secretsmanager")
    iam_client = session.client("iam")
    ecs_client = session.client("ecs")
    codebuild_client = session.client("codebuild")
    ec2_client = session.client("ec2")

    deleted = {
        "event_source_mappings": [],
        "api_gateways": [],
        "sns_subscriptions": [],
        "lambda_permissions": [],
        "ecs_services": [],
        "ecs_task_definitions": [],
        "ecs_clusters": [],
        "codebuild_projects": [],
        "lambda_functions": [],
        "sqs_queues": [],
        "sns_topics": [],
        "dynamodb_tables": [],
        "secrets": [],
        "route_tables": [],
        "subnets": [],
        "security_groups": [],
        "vpcs": [],
        "roles": [],
    }
    failed = {key: [] for key in deleted}

    for item in manifest.get("lambda_event_source_mappings", []):
        mapping_id = item.get("target_uuid")
        if not mapping_id and item.get("target_function") and item.get("target_event_source_arn"):
            mapping_id = find_event_source_mapping_uuid(
                lambda_client,
                item["target_function"],
                item["target_event_source_arn"],
            )
        if not mapping_id:
            continue
        if should_exclude("lambda_event_source_mappings", mapping_id, config):
            continue
        safe_delete(
            lambda mid=mapping_id: lambda_client.delete_event_source_mapping(UUID=mid),
            deleted["event_source_mappings"],
            failed["event_source_mappings"],
            {"uuid": mapping_id},
        )

    for item in manifest.get("api_gateways", []):
        api_id = item.get("target_api_id")
        api_name = item.get("target_api")
        if not api_id:
            continue
        if should_exclude("api_gateways", api_name or api_id, config):
            continue
        safe_delete(
            lambda aid=api_id: apigw_client.delete_rest_api(restApiId=aid),
            deleted["api_gateways"],
            failed["api_gateways"],
            {"api_id": api_id, "api_name": api_name},
        )

    for item in manifest.get("sns_subscriptions", []):
        subscription_arn = item.get("subscription_arn")
        if not subscription_arn or subscription_arn == "PendingConfirmation":
            continue
        safe_delete(
            lambda arn=subscription_arn: sns_client.unsubscribe(SubscriptionArn=arn),
            deleted["sns_subscriptions"],
            failed["sns_subscriptions"],
            {"subscription_arn": subscription_arn},
        )

    for item in manifest.get("lambda_permissions", []):
        function_name = item.get("target_function")
        statement_id = item.get("statement_id")
        if not function_name or not statement_id:
            continue
        safe_delete(
            lambda fn=function_name, sid=statement_id: lambda_client.remove_permission(FunctionName=fn, StatementId=sid),
            deleted["lambda_permissions"],
            failed["lambda_permissions"],
            {"function_name": function_name, "statement_id": statement_id},
        )

    services_by_cluster = {}
    for item in manifest.get("ecs_services", []):
        cluster_arn = item.get("target_cluster_arn")
        service_name = item.get("target_service")
        if not cluster_arn or not service_name:
            continue
        if should_exclude("ecs_services", service_name, config):
            continue
        services_by_cluster.setdefault(cluster_arn, []).append(service_name)
    for cluster_arn, service_names in services_by_cluster.items():
        for service_name in service_names:
            try:
                ecs_client.update_service(cluster=cluster_arn, service=service_name, desiredCount=0)
            except Exception:
                pass
            safe_delete(
                lambda c=cluster_arn, s=service_name: ecs_client.delete_service(cluster=c, service=s, force=True),
                deleted["ecs_services"],
                failed["ecs_services"],
                {"cluster_arn": cluster_arn, "service_name": service_name},
            )
        for service_name in service_names:
            try:
                wait_for_ecs_service_inactive(ecs_client, cluster_arn, service_name)
            except Exception as exc:
                failed["ecs_services"].append({"cluster_arn": cluster_arn, "service_name": service_name, "error": str(exc)})

    for item in manifest.get("ecs_task_definitions", []):
        task_definition_arn = item.get("target_task_definition")
        if not task_definition_arn:
            continue
        if should_exclude("ecs_task_definitions", task_definition_arn, config):
            continue
        safe_delete(
            lambda arn=task_definition_arn: ecs_client.deregister_task_definition(taskDefinition=arn),
            deleted["ecs_task_definitions"],
            failed["ecs_task_definitions"],
            {"task_definition_arn": task_definition_arn},
        )

    for item in manifest.get("ecs_clusters", []):
        cluster_name = item.get("target_cluster")
        cluster_arn = item.get("target_cluster_arn")
        if not cluster_name:
            continue
        if should_exclude("ecs_clusters", cluster_name, config):
            continue
        safe_delete(
            lambda name=cluster_name: ecs_client.delete_cluster(cluster=name),
            deleted["ecs_clusters"],
            failed["ecs_clusters"],
            {"cluster_name": cluster_name, "cluster_arn": cluster_arn},
        )

    for item in manifest.get("codebuild_projects", []):
        project_name = item.get("target_project")
        if not project_name:
            continue
        if should_exclude("codebuild_projects", project_name, config):
            continue
        safe_delete(
            lambda name=project_name: codebuild_client.delete_project(name=name),
            deleted["codebuild_projects"],
            failed["codebuild_projects"],
            {"project_name": project_name},
        )

    for item in manifest.get("lambda_functions", []):
        function_name = item.get("target_function")
        if not function_name:
            continue
        if should_exclude("lambda_functions", function_name, config):
            continue
        safe_delete(
            lambda fn=function_name: lambda_client.delete_function(FunctionName=fn),
            deleted["lambda_functions"],
            failed["lambda_functions"],
            {"function_name": function_name},
        )

    for item in manifest.get("sqs_queues", []):
        queue_url = item.get("target_queue_url")
        queue_name = item.get("target_queue")
        if not queue_url:
            continue
        if should_exclude("sqs_queues", queue_name or queue_url, config):
            continue
        safe_delete(
            lambda url=queue_url: sqs_client.delete_queue(QueueUrl=url),
            deleted["sqs_queues"],
            failed["sqs_queues"],
            {"queue_name": queue_name, "queue_url": queue_url},
        )

    for item in manifest.get("sns_topics", []):
        topic_arn = item.get("target_topic_arn")
        topic_name = item.get("target_topic")
        if not topic_arn:
            continue
        if should_exclude("sns_topics", topic_name or topic_arn, config):
            continue
        safe_delete(
            lambda arn=topic_arn: sns_client.delete_topic(TopicArn=arn),
            deleted["sns_topics"],
            failed["sns_topics"],
            {"topic_name": topic_name, "topic_arn": topic_arn},
        )

    for item in manifest.get("dynamodb_tables", []):
        table_name = item.get("target_table")
        if not table_name:
            continue
        if should_exclude("dynamodb_tables", table_name, config):
            continue
        safe_delete(
            lambda name=table_name: dynamodb_client.delete_table(TableName=name),
            deleted["dynamodb_tables"],
            failed["dynamodb_tables"],
            {"table_name": table_name},
        )

    for item in manifest.get("secrets", []):
        secret_name = item.get("target_secret")
        if not secret_name:
            continue
        if should_exclude("secrets", secret_name, config):
            continue
        safe_delete(
            lambda name=secret_name: secrets_client.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True),
            deleted["secrets"],
            failed["secrets"],
            {"secret_name": secret_name},
        )

    for item in manifest.get("route_tables", []):
        route_table_id = item.get("target_route_table")
        if not route_table_id or not should_delete_network_resource(item):
            continue
        if should_exclude("route_tables", route_table_id, config):
            continue
        safe_delete(
            lambda rid=route_table_id: delete_route_table(ec2_client, rid),
            deleted["route_tables"],
            failed["route_tables"],
            {"route_table_id": route_table_id},
        )

    for item in manifest.get("security_groups", []):
        group_id = item.get("target_group")
        if not group_id or not should_delete_network_resource(item):
            continue
        if should_exclude("security_groups", group_id, config):
            continue
        safe_delete(
            lambda gid=group_id: ec2_client.delete_security_group(GroupId=gid),
            deleted["security_groups"],
            failed["security_groups"],
            {"group_id": group_id},
        )

    for item in manifest.get("subnets", []):
        subnet_id = item.get("target_subnet")
        if not subnet_id or not should_delete_network_resource(item):
            continue
        if should_exclude("subnets", subnet_id, config):
            continue
        safe_delete(
            lambda sid=subnet_id: ec2_client.delete_subnet(SubnetId=sid),
            deleted["subnets"],
            failed["subnets"],
            {"subnet_id": subnet_id},
        )

    for item in manifest.get("vpcs", []):
        vpc_id = item.get("target_vpc")
        if not vpc_id or not should_delete_network_resource(item):
            continue
        if should_exclude("vpcs", vpc_id, config):
            continue
        safe_delete(
            lambda vid=vpc_id: ec2_client.delete_vpc(VpcId=vid),
            deleted["vpcs"],
            failed["vpcs"],
            {"vpc_id": vpc_id},
        )

    for item in manifest.get("roles", []):
        role_name = item.get("target_role")
        if not role_name:
            continue
        if should_exclude("iam_roles", role_name, config):
            continue
        try:
            detach_role_policies(iam_client, role_name)
        except ClientError as exc:
            failed["roles"].append({"role_name": role_name, "error": str(exc)})
            continue
        safe_delete(
            lambda name=role_name: iam_client.delete_role(RoleName=name),
            deleted["roles"],
            failed["roles"],
            {"role_name": role_name},
        )

    report = {
        "manifest_path": str(manifest_path),
        "target_env": sanitize_name(args.target_env),
        "region": args.region,
        "deleted": deleted,
        "failed": failed,
    }
    report_path = manifest_path.parent / "destroy_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    failed_count = sum(len(items) for items in failed.values())
    print(json.dumps({
        "status": "ok" if failed_count == 0 else "partial",
        "report_path": str(report_path),
        "failed_resource_count": failed_count,
    }, indent=2))


if __name__ == "__main__":
    main()
