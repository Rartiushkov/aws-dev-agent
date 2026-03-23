import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from executor.scripts.audit_log import append_audit_event
from executor.scripts.transfer_common import inventory_dir_path, resolve_client_slug


def parse_args():
    parser = argparse.ArgumentParser(description="Build advanced migration plans for RDS, EC2, and CloudFormation workloads.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--target-env", default="")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def sanitize_name(value):
    cleaned = re.sub(r"[^a-zA-Z0-9-_]+", "-", (value or "").strip().lower())
    return re.sub(r"-{2,}", "-", cleaned).strip("-_")


def target_name(name, source_env="", target_env=""):
    if not name:
        return name
    updated = name
    source_env = sanitize_name(source_env)
    target_env = sanitize_name(target_env)
    if source_env and source_env in updated.lower():
        pattern = re.compile(re.escape(source_env), re.IGNORECASE)
        updated = pattern.sub(target_env, updated, count=1)
    elif target_env:
        updated = f"{target_env}-{updated}"
    return updated[:63]


def build_rds_plan(snapshot, target_env=""):
    plan = []
    for db in snapshot.get("rds", {}).get("instances", []):
        identifier = db.get("DBInstanceIdentifier", "")
        engine = db.get("Engine", "")
        is_aurora_like = engine.startswith("aurora")
        plan.append({
            "source_id": identifier,
            "target_id": target_name(identifier, snapshot.get("source_env", ""), target_env),
            "engine": engine,
            "strategy": "cluster-snapshot-restore" if is_aurora_like else "db-snapshot-restore",
            "publicly_accessible": db.get("PubliclyAccessible", False),
            "multi_az": db.get("MultiAZ", False),
            "storage_encrypted": db.get("StorageEncrypted", False),
            "subnet_group": (db.get("DBSubnetGroup", {}) or {}).get("DBSubnetGroupName", ""),
            "parameter_groups": [item.get("DBParameterGroupName", "") for item in db.get("DBParameterGroups", [])],
            "notes": [
                "Create a final snapshot before cutover.",
                "Restore into target account with remapped subnet and security group references.",
            ],
        })
    for cluster in snapshot.get("rds", {}).get("clusters", []):
        identifier = cluster.get("DBClusterIdentifier", "")
        plan.append({
            "source_id": identifier,
            "target_id": target_name(identifier, snapshot.get("source_env", ""), target_env),
            "engine": cluster.get("Engine", ""),
            "strategy": "cluster-snapshot-restore",
            "storage_encrypted": cluster.get("StorageEncrypted", False),
            "subnet_group": cluster.get("DBSubnetGroup", ""),
            "parameter_group": cluster.get("DBClusterParameterGroup", ""),
            "notes": [
                "Create cluster snapshot before cutover.",
                "Restore cluster and recreate instances in target account.",
            ],
        })
    return plan


def build_ec2_plan(snapshot, target_env=""):
    plan = []
    for instance in snapshot.get("ec2_instances", []):
        instance_id = instance.get("InstanceId", "")
        name = next((tag.get("Value", "") for tag in instance.get("Tags", []) if tag.get("Key") == "Name"), "") or instance_id
        plan.append({
            "source_id": instance_id,
            "source_name": name,
            "target_name": target_name(name, snapshot.get("source_env", ""), target_env),
            "instance_type": instance.get("InstanceType", ""),
            "strategy": "create-image-copy-launch",
            "subnet_id": instance.get("SubnetId", ""),
            "security_group_ids": [item.get("GroupId", "") for item in instance.get("SecurityGroups", [])],
            "root_device_name": instance.get("RootDeviceName", ""),
            "notes": [
                "Create AMI from source instance.",
                "Copy AMI if the target account differs, then relaunch with remapped networking.",
            ],
        })
    return plan


def build_cloudformation_plan(snapshot, target_env=""):
    return [
        {
            "stack_name": stack.get("StackName", ""),
            "target_stack_name": target_name(stack.get("StackName", ""), snapshot.get("source_env", ""), target_env),
            "status": stack.get("StackStatus", ""),
            "strategy": "export-template-and-redeploy",
            "requires_template_export": True,
            "notes": [
                "Export current template body from source account.",
                "Review parameters, IAM capabilities, and any custom resource lambdas before deployment.",
            ],
        }
        for stack in snapshot.get("cloudformation_stacks", [])
    ]


def build_advanced_plan(snapshot, target_env=""):
    rds_plan = build_rds_plan(snapshot, target_env=target_env)
    ec2_plan = build_ec2_plan(snapshot, target_env=target_env)
    cloudformation_plan = build_cloudformation_plan(snapshot, target_env=target_env)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_env": snapshot.get("source_env", ""),
        "target_env": sanitize_name(target_env),
        "account_id": snapshot.get("account_id", ""),
        "region": snapshot.get("region", ""),
        "rds": rds_plan,
        "ec2": ec2_plan,
        "cloudformation": cloudformation_plan,
        "summary": {
            "rds_plan_count": len(rds_plan),
            "ec2_plan_count": len(ec2_plan),
            "cloudformation_plan_count": len(cloudformation_plan),
        },
    }


def main():
    args = parse_args()
    source_env = args.source_env or "full-account-scan"
    client_slug = resolve_client_slug(args.client_slug, source_env=source_env, target_env=args.target_env)
    inventory_dir = inventory_dir_path(source_env, client_slug=client_slug)
    snapshot = json.loads((inventory_dir / "source_snapshot.json").read_text(encoding="utf-8"))
    plan = build_advanced_plan(snapshot, target_env=args.target_env)
    plan_path = inventory_dir / "advanced_migration_plan.json"
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    append_audit_event("build_advanced_migration_plan", "ok", {"plan_path": str(plan_path)}, source_env=source_env, target_env=args.target_env, client_slug=client_slug)
    print(json.dumps({"status": "ok", "plan_path": str(plan_path)}, indent=2))


if __name__ == "__main__":
    main()
