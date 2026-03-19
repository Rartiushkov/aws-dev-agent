import argparse
import json
from pathlib import Path

import boto3


def sanitize_name(value):
    return "".join(ch.lower() if ch.isalnum() or ch in "-_" else "-" for ch in value).strip("-_")


def parse_args():
    parser = argparse.ArgumentParser(description="Snapshot an ECS environment and prepare a recreation plan.")
    parser.add_argument("--source-cluster", required=True)
    parser.add_argument("--source-service", required=True)
    parser.add_argument("--target-env", required=True)
    parser.add_argument("--team", default="")
    parser.add_argument("--region", default="us-east-1")
    return parser.parse_args()


def describe_service(ecs, cluster, service):
    response = ecs.describe_services(cluster=cluster, services=[service])
    services = response.get("services", [])
    if not services:
        raise ValueError(f"Service '{service}' was not found in cluster '{cluster}'")
    return services[0]


def describe_task_definition(ecs, task_definition):
    response = ecs.describe_task_definition(taskDefinition=task_definition, include=["TAGS"])
    return response["taskDefinition"]


def describe_security_groups(ec2, service):
    awsvpc = service.get("networkConfiguration", {}).get("awsvpcConfiguration", {})
    groups = awsvpc.get("securityGroups", [])
    if not groups:
        return []
    response = ec2.describe_security_groups(GroupIds=groups)
    return response.get("SecurityGroups", [])


def describe_target_groups(elbv2, service):
    target_group_arns = [item["targetGroupArn"] for item in service.get("loadBalancers", []) if item.get("targetGroupArn")]
    if not target_group_arns:
        return []
    response = elbv2.describe_target_groups(TargetGroupArns=target_group_arns)
    return response.get("TargetGroups", [])


def write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def build_recreation_plan(source_cluster, source_service, target_env, team, service, task_definition, security_groups, target_groups):
    sanitized_target = sanitize_name(target_env)
    team_suffix = f"-{sanitize_name(team)}" if team else ""
    target_cluster = f"{sanitized_target}{team_suffix}-cluster"
    target_service = f"{sanitized_target}{team_suffix}-service"
    target_family = f"{sanitized_target}{team_suffix}-taskdef"

    desired_count = service.get("desiredCount", 1)
    container_env = {}
    for container in task_definition.get("containerDefinitions", []):
        container_env[container["name"]] = {
            item["name"]: item.get("value", "")
            for item in container.get("environment", [])
        }

    return {
        "source": {
            "cluster": source_cluster,
            "service": source_service,
            "task_definition": service.get("taskDefinition"),
        },
        "target": {
            "environment_name": sanitized_target,
            "team": team,
            "cluster_name": target_cluster,
            "service_name": target_service,
            "task_definition_family": target_family,
            "desired_count": desired_count,
        },
        "network": {
            "subnets": service.get("networkConfiguration", {}).get("awsvpcConfiguration", {}).get("subnets", []),
            "security_groups": [
                {
                    "group_id": item["GroupId"],
                    "group_name": item.get("GroupName"),
                    "description": item.get("Description"),
                }
                for item in security_groups
            ],
        },
        "load_balancing": [
            {
                "target_group_arn": item["TargetGroupArn"],
                "target_group_name": item.get("TargetGroupName"),
                "port": item.get("Port"),
                "protocol": item.get("Protocol"),
                "vpc_id": item.get("VpcId"),
            }
            for item in target_groups
        ],
        "container_environment": container_env,
        "next_steps": [
            "Create or choose the target ECS cluster and networking.",
            "Register a copied task definition family with environment overrides for the new target.",
            "Create a new ECS service that reuses the reviewed security groups, subnets, and target group pattern.",
            "Commit the generated snapshot and recreation plan to git for review and reuse.",
        ],
    }


def main():
    args = parse_args()
    ecs = boto3.client("ecs", region_name=args.region)
    ec2 = boto3.client("ec2", region_name=args.region)
    elbv2 = boto3.client("elbv2", region_name=args.region)

    service = describe_service(ecs, args.source_cluster, args.source_service)
    task_definition = describe_task_definition(ecs, service["taskDefinition"])
    security_groups = describe_security_groups(ec2, service)
    target_groups = describe_target_groups(elbv2, service)

    output_dir = Path("state") / "env_clones" / sanitize_name(args.target_env)
    output_dir.mkdir(parents=True, exist_ok=True)

    snapshot = {
        "service": service,
        "task_definition": task_definition,
        "security_groups": security_groups,
        "target_groups": target_groups,
    }
    plan = build_recreation_plan(
        args.source_cluster,
        args.source_service,
        args.target_env,
        args.team,
        service,
        task_definition,
        security_groups,
        target_groups,
    )

    write_json(output_dir / "source_snapshot.json", snapshot)
    write_json(output_dir / "recreation_plan.json", plan)

    summary = [
        f"Source cluster: {args.source_cluster}",
        f"Source service: {args.source_service}",
        f"Target environment: {sanitize_name(args.target_env)}",
        f"Team: {args.team or 'n/a'}",
        f"Snapshot file: {output_dir / 'source_snapshot.json'}",
        f"Recreation plan: {output_dir / 'recreation_plan.json'}",
    ]
    (output_dir / "README.md").write_text("\n".join(summary) + "\n", encoding="utf-8")

    print(json.dumps({
        "status": "ok",
        "output_dir": str(output_dir),
        "target_cluster": plan["target"]["cluster_name"],
        "target_service": plan["target"]["service_name"],
    }, indent=2))


if __name__ == "__main__":
    main()
