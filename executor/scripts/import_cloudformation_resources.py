import argparse
import json
import sys
import time
import uuid
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from botocore.exceptions import ClientError

from executor.scripts.audit_log import append_audit_event
from executor.scripts.deploy_cloudformation_templates import parse_template_body
from executor.scripts.pre_migration_snapshot import build_snapshot_manifest
from executor.scripts.transfer_common import config_override, inventory_dir_path, load_transfer_config, resolve_client_slug, session_for


def parse_args():
    parser = argparse.ArgumentParser(description="Import existing AWS resources into CloudFormation stacks using an import-ready plan.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--target-env", required=True)
    parser.add_argument("--inventory-key", default="")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--config", default="")
    parser.add_argument("--target-role-arn", default="")
    parser.add_argument("--target-external-id", default="")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def safe_name(value):
    return value.strip().lower().replace(" ", "-")


def load_import_plan(source_env, inventory_key, target_env, client_slug=""):
    inventory_dir = inventory_dir_path(source_env, inventory_key, client_slug)
    plan_path = inventory_dir / f"cloudformation_import_plan_{safe_name(target_env)}.json"
    if not plan_path.exists():
        raise FileNotFoundError(f"CloudFormation import plan not found: {plan_path}")
    return inventory_dir, plan_path, json.loads(plan_path.read_text(encoding="utf-8"))


def ensure_deletion_policies(template, logical_ids):
    template = json.loads(json.dumps(template))
    resources = template.get("Resources", {}) or {}
    for logical_id in logical_ids:
        resource = resources.get(logical_id)
        if not resource:
            continue
        resource.setdefault("DeletionPolicy", "Retain")
        resource.setdefault("UpdateReplacePolicy", "Retain")
    return template


def prepare_template_for_import(template, logical_ids):
    prepared = ensure_deletion_policies(template, logical_ids)
    prepared.pop("Outputs", None)
    return prepared


def describe_stack_status(cf_client, stack_name):
    try:
        stacks = cf_client.describe_stacks(StackName=stack_name).get("Stacks", [])
    except Exception:
        return ""
    if not stacks:
        return ""
    return stacks[0].get("StackStatus", "")


def wait_for_stack_delete(cf_client, stack_name, timeout_seconds=900):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = describe_stack_status(cf_client, stack_name)
        if not status:
            return
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for stack deletion: {stack_name}")


def delete_failed_stack_if_needed(cf_client, stack_name):
    status = describe_stack_status(cf_client, stack_name)
    if status == "ROLLBACK_COMPLETE":
        cf_client.delete_stack(StackName=stack_name)
        wait_for_stack_delete(cf_client, stack_name)
        return "deleted_rollback_stack"
    return ""


def wait_for_change_set(cf_client, stack_name, change_set_name, timeout_seconds=900):
    deadline = time.time() + timeout_seconds
    last_status = ""
    last_reason = ""
    while time.time() < deadline:
        response = cf_client.describe_change_set(StackName=stack_name, ChangeSetName=change_set_name)
        last_status = response.get("Status", "")
        last_reason = response.get("StatusReason", "")
        if last_status in {"CREATE_COMPLETE", "FAILED"}:
            return response
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for change set {change_set_name}: {last_status} {last_reason}")


def wait_for_stack_operation(cf_client, stack_name, timeout_seconds=1800):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = describe_stack_status(cf_client, stack_name)
        if status.endswith("_COMPLETE"):
            return status
        if status.endswith("_FAILED") or "ROLLBACK" in status:
            return status
        time.sleep(10)
    raise TimeoutError(f"Timed out waiting for stack operation: {stack_name}")


def import_resources(cf_client, stack_plan):
    stack_name = stack_plan["target_stack"]
    template_path = stack_plan["template_path"]
    template = parse_template_body(Path(template_path).read_text(encoding="utf-8"))
    logical_ids = [item["logical_id"] for item in stack_plan.get("resources", []) if item.get("logical_id")]
    prepared_template = prepare_template_for_import(template, logical_ids)
    resources_to_import = [
        {
            "ResourceType": item["resource_type"],
            "LogicalResourceId": item["logical_id"],
            "ResourceIdentifier": item["identifier_values"],
        }
        for item in stack_plan.get("resources", [])
        if item.get("importable") and item.get("resource_type") and item.get("logical_id") and item.get("identifier_values")
    ]
    if not resources_to_import:
        return {
            "stack_name": stack_name,
            "cleanup_action": "",
            "change_set_name": "",
            "operation": "skipped",
            "status": "NO_IMPORTABLE_RESOURCES",
            "reason": "No importable resources were identified in the plan.",
        }
    cleanup_action = delete_failed_stack_if_needed(cf_client, stack_name)
    change_set_name = f"import-{uuid.uuid4().hex[:8]}"
    try:
        cf_client.create_change_set(
            StackName=stack_name,
            ChangeSetName=change_set_name,
            ChangeSetType="IMPORT",
            TemplateBody=json.dumps(prepared_template, indent=2),
            Parameters=stack_plan.get("parameter_overrides", []),
            Capabilities=["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"],
            ResourcesToImport=resources_to_import,
        )
    except Exception as exc:
        return {
            "stack_name": stack_name,
            "cleanup_action": cleanup_action,
            "change_set_name": change_set_name,
            "operation": "failed",
            "status": describe_stack_status(cf_client, stack_name) or "FAILED",
            "reason": str(exc),
        }
    change_set = wait_for_change_set(cf_client, stack_name, change_set_name)
    if change_set.get("Status") == "FAILED":
        return {
            "stack_name": stack_name,
            "cleanup_action": cleanup_action,
            "change_set_name": change_set_name,
            "operation": "failed",
            "status": change_set.get("Status", ""),
            "reason": change_set.get("StatusReason", ""),
        }
    cf_client.execute_change_set(StackName=stack_name, ChangeSetName=change_set_name)
    stack_status = wait_for_stack_operation(cf_client, stack_name)
    return {
        "stack_name": stack_name,
        "cleanup_action": cleanup_action,
        "change_set_name": change_set_name,
        "operation": "imported" if stack_status == "IMPORT_COMPLETE" else "failed",
        "status": stack_status,
        "resource_count": len(resources_to_import),
    }


def main():
    args = parse_args()
    source_env = args.source_env or "full-account-scan"
    config = load_transfer_config(args.config)
    client_slug = resolve_client_slug(args.client_slug, config, source_env=source_env, target_env=args.target_env)
    target_external_id = args.target_external_id or config_override(config, "target_external_id", "")
    inventory_dir, plan_path, plan = load_import_plan(source_env, args.inventory_key, args.target_env, client_slug)
    session = session_for(args.region, args.target_role_arn, external_id=target_external_id)
    cf_client = session.client("cloudformation")
    snapshot_manifest = build_snapshot_manifest(
        source_env=source_env,
        inventory_key=args.inventory_key,
        target_env=args.target_env,
        client_slug=client_slug,
        config_path=args.config,
    )

    results = [import_resources(cf_client, stack_plan) for stack_plan in plan.get("stacks", [])]
    result_path = inventory_dir / f"cloudformation_import_result_{safe_name(args.target_env)}.json"
    result_path.write_text(json.dumps({"results": results, "pre_migration_snapshot": snapshot_manifest}, indent=2), encoding="utf-8")
    append_audit_event(
        "import_cloudformation_resources",
        "ok",
        {"plan_path": str(plan_path), "result_path": str(result_path), "stack_count": len(results), "pre_migration_snapshot": snapshot_manifest["report_path"]},
        source_env=source_env,
        target_env=args.target_env,
        client_slug=client_slug,
    )
    print(json.dumps({"status": "ok", "result_path": str(result_path), "stack_count": len(results)}, indent=2))


if __name__ == "__main__":
    main()
