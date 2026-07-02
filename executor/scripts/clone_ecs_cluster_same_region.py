import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import boto3
from botocore.exceptions import ClientError

from executor.scripts.deploy_discovered_env import (
    deploy_ecs_clusters,
    deploy_ecs_services,
    deploy_ecs_task_definitions,
)


def parse_args():
    parser = argparse.ArgumentParser(description="Clone an ECS cluster into a new cluster name in the same AWS region.")
    parser.add_argument("--source-cluster", required=True)
    parser.add_argument("--target-cluster", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def sanitize_name(value):
    return "".join(ch.lower() if ch.isalnum() or ch in "-_" else "-" for ch in value).strip("-_")


def load_cluster_snapshot(ecs_client, source_cluster):
    described = ecs_client.describe_clusters(clusters=[source_cluster], include=["SETTINGS", "CONFIGURATIONS", "TAGS"])
    clusters = described.get("clusters", [])
    if not clusters:
        raise RuntimeError(f"Source cluster not found: {source_cluster}")
    cluster = clusters[0]

    service_arns = []
    paginator = ecs_client.get_paginator("list_services")
    for page in paginator.paginate(cluster=source_cluster):
        service_arns.extend(page.get("serviceArns", []))

    services = []
    if service_arns:
        for index in range(0, len(service_arns), 10):
            chunk = service_arns[index:index + 10]
            response = ecs_client.describe_services(cluster=source_cluster, services=chunk)
            services.extend(response.get("services", []))

    task_defs = []
    seen_task_defs = set()
    for service in services:
        task_def_arn = service.get("taskDefinition")
        if not task_def_arn or task_def_arn in seen_task_defs:
            continue
        seen_task_defs.add(task_def_arn)
        task_defs.append(
            ecs_client.describe_task_definition(taskDefinition=task_def_arn, include=["TAGS"])["taskDefinition"]
        )

    return {
        "ecs": {
            "clusters": [cluster],
            "services": services,
            "task_definitions": task_defs,
        }
    }


def service_preflight_issues(services):
    issues = []
    for service in services:
        service_name = service.get("serviceName", "")
        if service.get("loadBalancers"):
            issues.append({
                "service": service_name,
                "reason": "load_balancers_not_supported_yet",
            })
        if service.get("serviceRegistries"):
            issues.append({
                "service": service_name,
                "reason": "service_registries_not_supported_yet",
            })
        if service.get("deploymentController", {}).get("type") not in ("ECS", None):
            issues.append({
                "service": service_name,
                "reason": "custom_deployment_controller_not_supported",
            })
    return issues


def target_service_names(snapshot, source_cluster, target_cluster):
    mapping = {}
    for service in snapshot.get("ecs", {}).get("services", []):
        source_name = service.get("serviceName", "")
        if source_cluster in source_name:
            mapping[source_name] = source_name.replace(source_cluster, target_cluster, 1)
        else:
            mapping[source_name] = f"{target_cluster}-{source_name}"
    return mapping


def target_task_families(snapshot, source_cluster, target_cluster):
    mapping = {}
    for task_def in snapshot.get("ecs", {}).get("task_definitions", []):
        source_family = task_def.get("family", "")
        if source_cluster in source_family:
            mapping[source_family] = source_family.replace(source_cluster, target_cluster, 1)
        else:
            mapping[source_family] = f"{target_cluster}-{source_family}"
    return mapping


def build_plan(snapshot, source_cluster, target_cluster, region):
    services = snapshot.get("ecs", {}).get("services", [])
    task_defs = snapshot.get("ecs", {}).get("task_definitions", [])
    cluster = snapshot.get("ecs", {}).get("clusters", [{}])[0]
    issues = service_preflight_issues(services)
    return {
        "mode": "read-only-assessment",
        "source_cluster": source_cluster,
        "target_cluster": target_cluster,
        "region": region,
        "planned_actions": {
            "cluster": 1,
            "services": len(services),
            "task_definitions": len(task_defs),
        },
        "source_summary": {
            "cluster_arn": cluster.get("clusterArn", ""),
            "service_names": [service.get("serviceName", "") for service in services],
            "task_definition_families": [task_def.get("family", "") for task_def in task_defs],
        },
        "target_summary": {
            "cluster_name": target_cluster,
            "service_names": list(target_service_names(snapshot, source_cluster, target_cluster).values()),
            "task_definition_families": list(target_task_families(snapshot, source_cluster, target_cluster).values()),
        },
        "preflight_issues": issues,
        "ready_to_apply": not issues,
    }


def ensure_target_cluster_absent(ecs_client, target_cluster):
    response = ecs_client.describe_clusters(clusters=[target_cluster])
    clusters = response.get("clusters", [])
    if clusters and clusters[0].get("status") != "INACTIVE":
        raise RuntimeError(f"Target cluster already exists: {target_cluster}")


def clone_cluster(snapshot, ecs_client, source_cluster, target_cluster):
    source_env = sanitize_name(source_cluster)
    target_env = sanitize_name(target_cluster)

    resource_mappings, deployed_clusters, cluster_failures = deploy_ecs_clusters(
        snapshot,
        ecs_client,
        source_env,
        target_env,
        team="",
        preserve_names=False,
        config={},
    )

    if cluster_failures:
        raise RuntimeError(json.dumps({"cluster_failures": cluster_failures}, indent=2))

    subnet_ids = {}
    security_group_ids = {}
    for service in snapshot.get("ecs", {}).get("services", []):
        awsvpc = service.get("networkConfiguration", {}).get("awsvpcConfiguration", {})
        for subnet_id in awsvpc.get("subnets", []):
            subnet_ids[subnet_id] = subnet_id
        for security_group_id in awsvpc.get("securityGroups", []):
            security_group_ids[security_group_id] = security_group_id

    resource_mappings["role_arns"] = {}
    resource_mappings["subnet_ids"] = subnet_ids
    resource_mappings["security_group_ids"] = security_group_ids
    resource_mappings["target_region"] = ecs_client.meta.region_name

    task_mappings, deployed_task_defs, task_failures = deploy_ecs_task_definitions(
        snapshot,
        ecs_client,
        resource_mappings,
        source_env,
        target_env,
        team="",
        preserve_names=False,
        config={},
    )
    resource_mappings.update(task_mappings)

    if task_failures:
        raise RuntimeError(json.dumps({"task_definition_failures": task_failures}, indent=2))

    deployed_services, service_failures = deploy_ecs_services(
        snapshot,
        ecs_client,
        resource_mappings,
        source_env,
        target_env,
        team="",
        preserve_names=False,
        config={},
    )

    if service_failures:
        raise RuntimeError(json.dumps({"service_failures": service_failures}, indent=2))

    return {
        "status": "ok",
        "deployed_clusters": deployed_clusters,
        "deployed_task_definitions": deployed_task_defs,
        "deployed_services": deployed_services,
    }


def write_artifacts(source_cluster, target_cluster, plan, apply_result=None):
    output_dir = Path("state") / "ecs_cluster_clones" / sanitize_name(target_cluster)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "clone_plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    if apply_result is not None:
        (output_dir / "clone_result.json").write_text(json.dumps(apply_result, indent=2), encoding="utf-8")
    readme = [
        f"Source cluster: {source_cluster}",
        f"Target cluster: {target_cluster}",
        f"Plan: {output_dir / 'clone_plan.json'}",
    ]
    if apply_result is not None:
        readme.append(f"Result: {output_dir / 'clone_result.json'}")
    (output_dir / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")
    return output_dir


def main():
    args = parse_args()
    ecs_client = boto3.client("ecs", region_name=args.region)
    snapshot = load_cluster_snapshot(ecs_client, args.source_cluster)
    plan = build_plan(snapshot, args.source_cluster, args.target_cluster, args.region)
    output_dir = write_artifacts(args.source_cluster, args.target_cluster, plan)

    if not args.apply:
        print(json.dumps({
            "status": "ok",
            "mode": "read-only-plan",
            "output_dir": str(output_dir),
            "ready_to_apply": plan["ready_to_apply"],
            "preflight_issues": plan["preflight_issues"],
        }, indent=2))
        return

    if plan["preflight_issues"]:
        raise RuntimeError("Preflight issues detected; refusing to apply cluster clone")

    ensure_target_cluster_absent(ecs_client, args.target_cluster)
    result = clone_cluster(snapshot, ecs_client, args.source_cluster, args.target_cluster)
    output_dir = write_artifacts(args.source_cluster, args.target_cluster, plan, apply_result=result)
    print(json.dumps({
        "status": "ok",
        "mode": "applied",
        "output_dir": str(output_dir),
        "result": result,
    }, indent=2))


if __name__ == "__main__":
    main()
