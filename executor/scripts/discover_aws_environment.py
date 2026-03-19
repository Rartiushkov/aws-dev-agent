import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import boto3


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


def _list_ecs(lambda_client, ecs_client, source_env):
    del lambda_client  # keep signature stable in case we expand later
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

    identity = sts.get_caller_identity()
    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "account_id": identity["Account"],
        "region": region,
        "source_env": source_env,
        "team": team,
        "lambda_functions": _list_lambda_functions(lambda_client, source_env),
        "cloudformation_stacks": _list_cloudformation_stacks(cf_client, source_env),
        "load_balancers": _list_load_balancers(elbv2_client, source_env),
        "security_groups": _list_security_groups(ec2_client, source_env),
        "ecs": _list_ecs(lambda_client, ecs_client, source_env),
    }

    base_dir = Path("state") / "aws_inventory"
    target_dir = base_dir / (source_env or "full-account-scan")
    target_dir.mkdir(parents=True, exist_ok=True)

    snapshot_path = target_dir / "source_snapshot.json"
    summary_path = target_dir / "summary.json"

    summary = {
        "source_env": source_env,
        "team": team,
        "region": region,
        "account_id": identity["Account"],
        "counts": {
            "lambda_functions": len(snapshot["lambda_functions"]),
            "cloudformation_stacks": len(snapshot["cloudformation_stacks"]),
            "load_balancers": len(snapshot["load_balancers"]),
            "security_groups": len(snapshot["security_groups"]),
            "ecs_clusters": len(snapshot["ecs"]["clusters"]),
            "ecs_services": len(snapshot["ecs"]["services"]),
            "ecs_task_definitions": len(snapshot["ecs"]["task_definitions"]),
        },
    }

    snapshot_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps({
        "status": "ok",
        "snapshot_path": str(snapshot_path),
        "summary_path": str(summary_path),
        "summary": summary,
    }, indent=2))


if __name__ == "__main__":
    main()
