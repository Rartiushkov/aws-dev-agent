import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from executor.scripts.transfer_common import inventory_dir_path, resolve_client_slug


def parse_args():
    parser = argparse.ArgumentParser(description="Build a migration strategy report from a discovered AWS snapshot.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def aggregate_overall_status(statuses):
    statuses = list(statuses)
    if any(status == "manual" for status in statuses):
        return "partial"
    if any(status == "partial" for status in statuses):
        return "partial"
    if any(status == "planned" for status in statuses):
        return "planned"
    return "covered"


def _service_counts(snapshot):
    return {
        "vpcs": len(snapshot.get("vpcs", [])),
        "subnets": len(snapshot.get("subnets", [])),
        "route_tables": len(snapshot.get("route_tables", [])),
        "security_groups": len(snapshot.get("security_groups", [])),
        "api_gateways": len(snapshot.get("api_gateways", [])),
        "rds_instances": len(snapshot.get("rds", {}).get("instances", [])),
        "rds_clusters": len(snapshot.get("rds", {}).get("clusters", [])),
        "s3_buckets": len(snapshot.get("s3_buckets", [])),
        "ecs_clusters": len(snapshot.get("ecs", {}).get("clusters", [])),
        "ecs_services": len(snapshot.get("ecs", {}).get("services", [])),
        "ecs_task_definitions": len(snapshot.get("ecs", {}).get("task_definitions", [])),
        "ec2_instances": len(snapshot.get("ec2_instances", [])),
        "ec2_like_signals": len(snapshot.get("ec2_instances", [])) + len(snapshot.get("load_balancers", [])) + len(snapshot.get("cloudformation_stacks", [])),
        "kms_signals": sum(1 for secret in snapshot.get("secrets", []) if secret.get("KmsKeyId")) +
        sum(1 for queue in snapshot.get("sqs_queues", []) if queue.get("Attributes", {}).get("KmsMasterKeyId")),
        "cloudformation_stacks": len(snapshot.get("cloudformation_stacks", [])),
    }


def build_strategy(snapshot, risk_report=None):
    counts = _service_counts(snapshot)
    risk_report = risk_report or {"summary": {}, "findings": []}

    tracks = []

    network_status = "covered" if counts["vpcs"] == counts["subnets"] == counts["route_tables"] == counts["security_groups"] == 0 else "partial"
    tracks.append({
        "name": "network",
        "status": network_status,
        "resource_counts": {
            "vpcs": counts["vpcs"],
            "subnets": counts["subnets"],
            "route_tables": counts["route_tables"],
            "security_groups": counts["security_groups"],
        },
        "current_support": [
            "discovery and dependency graph for VPC, subnet, route table, security group",
            "risk scan for public exposure and route topology",
            "Lambda VPC config recreation when subnet/security group IDs already exist",
        ],
        "recommended_method": "Recreate network explicitly before application deploy; map or precreate VPC/subnet/security-group targets.",
        "next_steps": [
            "Add VPC/subnet/security-group create/update scripts",
            "Add target-side mapping for route tables and VPC IDs",
        ],
    })

    api_status = "covered" if counts["api_gateways"] == 0 else "partial"
    tracks.append({
        "name": "api_gateway",
        "status": api_status,
        "resource_counts": {"api_gateways": counts["api_gateways"]},
        "current_support": [
            "API Gateway discovery is implemented",
            "API creation is implemented in deploy flow",
        ],
        "recommended_method": "Use current deploy flow for simple REST APIs; keep stage/domain/integration review manual until expanded coverage lands.",
        "next_steps": [
            "Add stage deployment, authorizers, domain mappings, and usage plans",
            "Add validation of API integrations after deploy",
        ],
    })

    rds_status = "covered" if counts["rds_instances"] == counts["rds_clusters"] == 0 else "partial"
    tracks.append({
        "name": "rds_data_migration",
        "status": rds_status,
        "resource_counts": {
            "rds_instances": counts["rds_instances"],
            "rds_clusters": counts["rds_clusters"],
        },
        "current_support": [
            "RDS discovery and risk analysis are implemented",
            "Engine-aware snapshot/restore migration planning is implemented",
        ],
        "recommended_method": "Use generated snapshot/restore plan by default; switch to AWS DMS only when downtime or engine constraints require it.",
        "next_steps": [
            "Add optional live snapshot creation and restore orchestration",
            "Add DMS task generation for low-downtime paths",
        ],
    })

    s3_status = "covered" if counts["s3_buckets"] == 0 else "manual"
    tracks.append({
        "name": "s3_object_transfer",
        "status": s3_status,
        "resource_counts": {"s3_buckets": counts["s3_buckets"]},
        "current_support": [
            "S3 bucket discovery and review flags are implemented",
            "Object transfer is not automated yet",
        ],
        "recommended_method": "Use S3 sync/replication/DataSync-style transfer plan for bucket objects; keep object migration explicit.",
        "next_steps": [
            "Add bucket policy recreation",
            "Add object copy/sync executor with dry-run and checksum reporting",
        ],
    })

    ecs_status = "covered" if counts["ecs_services"] == 0 else "partial"
    tracks.append({
        "name": "full_ecs_service_migration",
        "status": ecs_status,
        "resource_counts": {
            "ecs_clusters": counts["ecs_clusters"],
            "ecs_services": counts["ecs_services"],
            "ecs_task_definitions": counts["ecs_task_definitions"],
        },
        "current_support": [
            "ECS clusters, task definitions, and services are present in deploy flow",
            "ECS discovery includes tasks, schedules, metrics, and event history",
        ],
        "recommended_method": "Use current ECS deploy path for metadata recreation; add image, networking, and LB validation for production-grade migrations.",
        "next_steps": [
            "Add load balancer/target group handling",
            "Add ECR image strategy and ECS service validation checks",
        ],
    })

    ec2_status = "partial" if counts["ec2_like_signals"] > 0 else "planned"
    tracks.append({
        "name": "ec2_strategy",
        "status": ec2_status,
        "resource_counts": {"ec2_instances": counts["ec2_instances"], "ec2_like_signals": counts["ec2_like_signals"]},
        "current_support": [
            "EC2 instance discovery is implemented",
            "AMI-style migration planning is implemented",
        ],
        "recommended_method": "Use generated AMI-style migration plan for image-based moves; switch to AWS MGN when continuous replication is needed.",
        "next_steps": [
            "Add optional live image creation orchestration",
            "Add launch-template and EBS deep-copy support",
        ],
    })

    iam_kms_status = "covered" if counts["kms_signals"] == 0 else "partial"
    tracks.append({
        "name": "iam_kms_deeper_handling",
        "status": iam_kms_status,
        "resource_counts": {"kms_signals": counts["kms_signals"]},
        "current_support": [
            "IAM role cloning is implemented",
            "Secrets and queues preserve KMS references when present",
            "Risk scan catches overly broad IAM patterns",
        ],
        "recommended_method": "Use current IAM clone flow for roles; add customer-managed KMS key mapping and policy narrowing before production rollout.",
        "next_steps": [
            "Add KMS key discovery and remapping",
            "Add least-privilege IAM rewrite suggestions",
        ],
    })

    iac_status = "partial" if counts["cloudformation_stacks"] > 0 else "planned"
    tracks.append({
        "name": "iac_export",
        "status": iac_status,
        "resource_counts": {"cloudformation_stacks": counts["cloudformation_stacks"]},
        "current_support": [
            "CloudFormation discovery and review flags are implemented",
            "AWS-to-Git export of discovered artifacts is implemented",
            "CloudFormation template export is implemented",
        ],
        "recommended_method": "Use exported stack templates as the source artifact; promote them into Terraform/CloudFormation import flows as needed.",
        "next_steps": [
            "Add parameter remapping and target-capability checks",
            "Annotate unsupported custom resources in export report",
        ],
    })

    zero_status = "planned"
    if counts["rds_instances"] or counts["s3_buckets"] or counts["ecs_services"] or counts["api_gateways"]:
        zero_status = "partial"
    tracks.append({
        "name": "zero_downtime_orchestration",
        "status": zero_status,
        "resource_counts": {
            "api_gateways": counts["api_gateways"],
            "ecs_services": counts["ecs_services"],
            "rds_instances": counts["rds_instances"],
            "s3_buckets": counts["s3_buckets"],
        },
        "current_support": [
            "Validation and redeploy flows exist",
            "No full change-plan / cutover orchestrator is implemented yet",
        ],
        "recommended_method": "Use staged deploy plus validate for now; add cutover plans per service type before advertising zero-downtime migrations.",
        "next_steps": [
            "Introduce dry-run change plan and cutover ordering",
            "Add rollback hints and per-service downtime classification",
        ],
    })

    overall_status = aggregate_overall_status(track["status"] for track in tracks) if tracks else "planned"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_env": snapshot.get("source_env", ""),
        "account_id": snapshot.get("account_id", ""),
        "region": snapshot.get("region", ""),
        "overall_status": overall_status,
        "risk_summary": risk_report.get("summary", {}),
        "tracks": tracks,
    }


def main():
    args = parse_args()
    source_env = args.source_env or "full-account-scan"
    client_slug = resolve_client_slug(args.client_slug, source_env=source_env)
    inventory_dir = inventory_dir_path(source_env, client_slug=client_slug)
    snapshot = json.loads((inventory_dir / "source_snapshot.json").read_text(encoding="utf-8"))
    risk_report_path = inventory_dir / "risk_report.json"
    risk_report = json.loads(risk_report_path.read_text(encoding="utf-8")) if risk_report_path.exists() else {"summary": {}, "findings": []}
    strategy = build_strategy(snapshot, risk_report)
    strategy_path = inventory_dir / "migration_strategy.json"
    strategy_path.write_text(json.dumps(strategy, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": "ok",
        "strategy_path": str(strategy_path),
        "overall_status": strategy["overall_status"],
        "track_count": len(strategy["tracks"]),
    }, indent=2))


if __name__ == "__main__":
    main()
