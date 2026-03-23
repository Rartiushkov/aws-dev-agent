import argparse
import json
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from executor.scripts.transfer_common import enabled_regions, inventory_dir_name, load_transfer_config, session_for
from executor.scripts.pre_migration_snapshot import build_snapshot_manifest
from executor.scripts.transfer_common import migration_dir_path, resolve_client_slug


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


def sanitize_name(value):
    return value.strip().lower().replace(" ", "-")


def parse_args():
    parser = argparse.ArgumentParser(description="Discover and migrate a source AWS environment into a new account.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--target-env", required=True)
    parser.add_argument("--team", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--source-role-arn", default="")
    parser.add_argument("--target-role-arn", default="")
    parser.add_argument("--source-external-id", default="")
    parser.add_argument("--target-external-id", default="")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--source-regions", default="")
    parser.add_argument("--target-region", default="")
    parser.add_argument("--read-only-plan", action="store_true")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def signal_resource_count(counts):
    return sum(int(counts.get(key, 0) or 0) for key in SIGNAL_COUNT_KEYS)


def parse_regions(raw_value):
    if not raw_value:
        return []
    return [item.strip() for item in str(raw_value).split(",") if item.strip()]


def region_inventory_key(source_env, region):
    base = source_env or "full-account-scan"
    return inventory_dir_name(base, f"{base}-{region}")


def region_deployment_key(target_env, source_region, target_region):
    if source_region == target_region:
        return sanitize_name(f"{target_env}-{target_region}")
    return sanitize_name(f"{target_env}-{source_region}-to-{target_region}")


def merged_region_config(base_config, source_region, target_region):
    merged = deepcopy(base_config or {})
    overrides = dict(merged.get("overrides", {}))
    overrides["source_region"] = source_region
    overrides["target_region"] = target_region
    merged["overrides"] = overrides
    return merged


def run_json_command(command):
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"Command failed: {' '.join(command)}")
    payload = (completed.stdout or "").strip()
    if not payload:
        return {}
    return json.loads(payload)


def discover_region(base_args, source_env, team, region, config_path, source_role_arn="", source_external_id=""):
    inventory_key = region_inventory_key(source_env, region)
    command = [
        sys.executable,
        "executor/scripts/discover_aws_environment.py",
        "--region",
        region,
        "--inventory-key",
        inventory_key,
    ]
    if source_env:
        command.extend(["--source-env", source_env])
    if team:
        command.extend(["--team", team])
    if config_path:
        command.extend(["--config", config_path])
    if source_role_arn:
        command.extend(["--source-role-arn", source_role_arn])
    if source_external_id:
        command.extend(["--source-external-id", source_external_id])
    if getattr(base_args, "client_slug", ""):
        command.extend(["--client-slug", base_args.client_slug])
    result = run_json_command(command)
    summary = result.get("summary", {})
    summary.setdefault("signal_resource_count", signal_resource_count(summary.get("counts", {})))
    summary.setdefault("has_signal_resources", summary["signal_resource_count"] > 0)
    return {
        "region": region,
        "inventory_key": inventory_key,
        "snapshot_path": result.get("snapshot_path", ""),
        "summary_path": result.get("summary_path", ""),
        "summary": summary,
    }


def deploy_region(source_env, target_env, team, source_region, target_region, inventory_key, deployment_key, config_path, source_role_arn="", target_role_arn="", source_external_id="", target_external_id="", read_only_plan=False):
    command = [
        sys.executable,
        "executor/scripts/deploy_discovered_env.py",
        "--source-env",
        source_env or "full-account-scan",
        "--target-env",
        target_env,
        "--source-region",
        source_region,
        "--region",
        target_region,
        "--inventory-key",
        inventory_key,
        "--deployment-key",
        deployment_key,
    ]
    if team:
        command.extend(["--team", team])
    if config_path:
        command.extend(["--config", config_path])
    if source_role_arn:
        command.extend(["--source-role-arn", source_role_arn])
    if target_role_arn:
        command.extend(["--target-role-arn", target_role_arn])
    if source_external_id:
        command.extend(["--source-external-id", source_external_id])
    if target_external_id:
        command.extend(["--target-external-id", target_external_id])
    if getattr(deploy_region, "_client_slug", ""):
        command.extend(["--client-slug", deploy_region._client_slug])
    if read_only_plan:
        command.append("--read-only-plan")
    return run_json_command(command)


def validate_region(target_env, target_region, deployment_key, config_path, source_role_arn="", target_role_arn="", source_external_id="", target_external_id=""):
    command = [
        sys.executable,
        "executor/scripts/validate_deployed_env.py",
        "--target-env",
        target_env,
        "--region",
        target_region,
        "--deployment-key",
        deployment_key,
    ]
    if config_path:
        command.extend(["--config", config_path])
    if source_role_arn:
        command.extend(["--source-role-arn", source_role_arn])
    if target_role_arn:
        command.extend(["--target-role-arn", target_role_arn])
    if source_external_id:
        command.extend(["--source-external-id", source_external_id])
    if target_external_id:
        command.extend(["--target-external-id", target_external_id])
    if getattr(validate_region, "_client_slug", ""):
        command.extend(["--client-slug", validate_region._client_slug])
    return run_json_command(command)


def transfer_s3_region(source_env, source_region, target_region, inventory_key, config_path, source_role_arn="", target_role_arn="", source_external_id="", target_external_id=""):
    command = [
        sys.executable,
        "executor/scripts/transfer_s3_objects.py",
        "--source-env",
        source_env or "full-account-scan",
        "--source-region",
        source_region,
        "--region",
        target_region,
        "--config",
        config_path,
        "--execute",
    ]
    if source_role_arn:
        command.extend(["--source-role-arn", source_role_arn])
    if target_role_arn:
        command.extend(["--target-role-arn", target_role_arn])
    if source_external_id:
        command.extend(["--source-external-id", source_external_id])
    if target_external_id:
        command.extend(["--target-external-id", target_external_id])
    if getattr(transfer_s3_region, "_client_slug", ""):
        command.extend(["--client-slug", transfer_s3_region._client_slug])
    return run_json_command(command)


def main():
    args = parse_args()
    base_config = load_transfer_config(args.config)
    client_slug = resolve_client_slug(args.client_slug, base_config, source_env=args.source_env, target_env=args.target_env)
    deploy_region._client_slug = client_slug
    validate_region._client_slug = client_slug
    transfer_s3_region._client_slug = client_slug
    source_external_id = args.source_external_id or base_config.get("overrides", {}).get("source_external_id", "")
    target_external_id = args.target_external_id or base_config.get("overrides", {}).get("target_external_id", "")
    configured_source_regions = parse_regions(base_config.get("overrides", {}).get("source_regions", ""))
    requested_regions = parse_regions(args.source_regions) or configured_source_regions

    if requested_regions:
        source_regions = requested_regions
    else:
        session = session_for(args.region, args.source_role_arn, external_id=source_external_id)
        source_regions = enabled_regions(session, fallback_region=args.region)

    temp_dir = Path(tempfile.mkdtemp(prefix="aws-dev-agent-migrate-"))
    region_results = []
    try:
        for source_region in source_regions:
            target_region = args.target_region or source_region
            merged_config = merged_region_config(base_config, source_region, target_region)
            config_path = temp_dir / f"config-{source_region}.json"
            config_path.write_text(json.dumps(merged_config, indent=2), encoding="utf-8")
            discovered = discover_region(
                args,
                args.source_env,
                args.team,
                source_region,
                str(config_path),
                source_role_arn=args.source_role_arn,
                source_external_id=source_external_id,
            )
            if not discovered["summary"].get("has_signal_resources"):
                continue
            deployment_key = region_deployment_key(args.target_env, source_region, target_region)
            deployed = deploy_region(
                args.source_env,
                args.target_env,
                args.team,
                source_region,
                target_region,
                discovered["inventory_key"],
                deployment_key,
                str(config_path),
                source_role_arn=args.source_role_arn,
                target_role_arn=args.target_role_arn,
                source_external_id=source_external_id,
                target_external_id=target_external_id,
                read_only_plan=args.read_only_plan,
            )
            validation = None
            s3_transfer = None
            has_s3_buckets = int(discovered["summary"].get("counts", {}).get("s3_buckets", 0) or 0) > 0
            if not args.read_only_plan:
                build_snapshot_manifest(
                    source_env=args.source_env,
                    inventory_key=discovered["inventory_key"],
                    target_env=args.target_env,
                    client_slug=client_slug,
                    config_path=str(config_path),
                )
                if has_s3_buckets:
                    s3_transfer = transfer_s3_region(
                        args.source_env,
                        source_region,
                        target_region,
                        discovered["inventory_key"],
                        str(config_path),
                        source_role_arn=args.source_role_arn,
                        target_role_arn=args.target_role_arn,
                        source_external_id=source_external_id,
                        target_external_id=target_external_id,
                    )
                validation = validate_region(
                    args.target_env,
                    target_region,
                    deployment_key,
                    str(config_path),
                    source_role_arn=args.source_role_arn,
                    target_role_arn=args.target_role_arn,
                    source_external_id=source_external_id,
                    target_external_id=target_external_id,
                )
            region_results.append({
                "source_region": source_region,
                "target_region": target_region,
                "inventory_key": discovered["inventory_key"],
                "deployment_key": deployment_key,
                "discovery": discovered,
                "deployment": deployed,
                "s3_transfer": s3_transfer,
                "validation": validation,
            })
    finally:
        pass

    report_dir = migration_dir_path(args.target_env, client_slug)
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "mode": "read-only-plan" if args.read_only_plan else "apply",
        "client_slug": client_slug,
        "source_env": sanitize_name(args.source_env) if args.source_env else "",
        "target_env": sanitize_name(args.target_env),
        "team": sanitize_name(args.team) if args.team else "",
        "regions_considered": source_regions,
        "regions_migrated": [item["source_region"] for item in region_results],
        "results": region_results,
    }
    report_path = report_dir / "migration_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": "ok",
        "mode": report["mode"],
        "report_path": str(report_path),
        "region_count": len(region_results),
    }, indent=2))


if __name__ == "__main__":
    main()
