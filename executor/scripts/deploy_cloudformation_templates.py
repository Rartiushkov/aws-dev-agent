import argparse
import ast
import json
import re
import sys
import time
from pathlib import Path
from collections import OrderedDict

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from botocore.exceptions import ClientError

from executor.scripts.audit_log import append_audit_event
from executor.scripts.pre_migration_snapshot import build_snapshot_manifest
from executor.scripts.transfer_common import (
    config_override,
    deployment_dir_path,
    inventory_dir_path,
    load_transfer_config,
    resolve_client_slug,
    session_for,
)


def parse_args():
    parser = argparse.ArgumentParser(description="Deploy exported CloudFormation templates into a target account.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--target-env", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--config", default="")
    parser.add_argument("--inventory-key", default="")
    parser.add_argument("--deployment-key", default="")
    parser.add_argument("--target-role-arn", default="")
    parser.add_argument("--target-external-id", default="")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def sanitize_name(value):
    cleaned = re.sub(r"[^a-zA-Z0-9-_]+", "-", (value or "").strip().lower())
    return re.sub(r"-{2,}", "-", cleaned).strip("-_")


def target_stack_name(source_name, target_env):
    return f"{sanitize_name(target_env)}-{source_name}"[:128]


def load_template_exports(source_env, inventory_key="", client_slug=""):
    inventory_dir = inventory_dir_path(source_env, inventory_key, client_slug)
    manifest_path = inventory_dir / "cloudformation_template_exports.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"CloudFormation export manifest not found: {manifest_path}")
    return inventory_dir, json.loads(manifest_path.read_text(encoding="utf-8"))


def load_deployment_manifest(target_env, deployment_key="", client_slug=""):
    manifest_path = deployment_dir_path(target_env, deployment_key, client_slug) / "deployment_manifest.json"
    if not manifest_path.exists():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def load_template_body(template_path):
    return Path(template_path).read_text(encoding="utf-8")


def parse_template_body(template_body):
    try:
        return json.loads(template_body)
    except Exception:
        pass
    try:
        parsed = ast.literal_eval(template_body)
        if isinstance(parsed, OrderedDict):
            return json.loads(json.dumps(parsed))
        return parsed
    except Exception:
        pass
    raise ValueError("Unsupported CloudFormation template format")


def _first_mapping_value(mapping):
    if not isinstance(mapping, dict) or not mapping:
        return ""
    return next(iter(mapping.values()))


def _find_mapped_name(mapping, source_default, target_env=""):
    if source_default and source_default in mapping:
        return mapping[source_default]
    if source_default:
        for value in mapping.values():
            if value.endswith(source_default):
                return value
        if target_env:
            return f"{sanitize_name(target_env)}-{source_default}"
    return _first_mapping_value(mapping)


def build_parameter_overrides(template_body, deployment_manifest, target_env):
    template = parse_template_body(template_body)
    parameters = template.get("Parameters", {})
    mappings = (deployment_manifest.get("resource_mappings", {}) or {})
    overrides = []
    ecs_cluster_names = mappings.get("ecs_cluster_names", {})
    vpc_ids = mappings.get("vpc_ids", {})
    subnet_ids = mappings.get("subnet_ids", {})
    security_group_ids = mappings.get("security_group_ids", {})
    queue_names = mappings.get("queue_names", {})
    table_names = mappings.get("dynamodb_table_names", {})
    role_names = mappings.get("role_names", {})
    function_names = mappings.get("function_names", {})
    secret_names = mappings.get("secret_names", {})

    for name, definition in parameters.items():
        lower_name = name.lower()
        default = definition.get("Default", "")
        value = ""
        if "clustername" in lower_name:
            value = ecs_cluster_names.get(str(default), "") or _first_mapping_value(ecs_cluster_names)
            if not value and default:
                value = f"{sanitize_name(target_env)}-{default}"
        elif lower_name == "vpcid" or lower_name.endswith("vpcid"):
            value = _first_mapping_value(vpc_ids)
        elif lower_name == "subnetids" or lower_name.endswith("subnetids"):
            value = ",".join(subnet_ids.values())
        elif lower_name == "securitygroupids" or lower_name.endswith("securitygroupids"):
            value = ",".join(security_group_ids.values())
        elif lower_name == "queuename" or lower_name.endswith("queuename"):
            value = _find_mapped_name(queue_names, str(default), target_env=target_env)
        elif lower_name == "tablename" or lower_name.endswith("tablename"):
            value = _find_mapped_name(table_names, str(default), target_env=target_env)
        elif lower_name == "rolename" or lower_name.endswith("rolename"):
            value = _find_mapped_name(role_names, str(default), target_env=target_env)
        elif lower_name == "functionname" or lower_name.endswith("functionname"):
            value = _find_mapped_name(function_names, str(default), target_env=target_env)
        elif lower_name == "secretname" or lower_name.endswith("secretname"):
            value = _find_mapped_name(secret_names, str(default), target_env=target_env)
        elif lower_name == "loggroupname" or lower_name.endswith("loggroupname"):
            if default:
                value = default.replace("/aws/lambda/", f"/aws/lambda/{sanitize_name(target_env)}-") if "/aws/lambda/" in str(default) else str(default)
        if value not in {"", None}:
            overrides.append({"ParameterKey": name, "ParameterValue": value})
    return overrides


def parameter_value(parameters, key, default=""):
    for item in parameters or []:
        if item.get("ParameterKey") == key:
            return item.get("ParameterValue", default)
    return default


def classify_existing_resource_conflict(template_body, parameters, deployment_manifest):
    template = parse_template_body(template_body)
    mappings = (deployment_manifest.get("resource_mappings", {}) or {})
    existing_values = {
        "AWS::ECS::Cluster": set((mappings.get("ecs_cluster_names", {}) or {}).values()),
        "AWS::SQS::Queue": set((mappings.get("queue_names", {}) or {}).values()),
        "AWS::DynamoDB::Table": set((mappings.get("dynamodb_table_names", {}) or {}).values()),
        "AWS::IAM::Role": set((mappings.get("role_names", {}) or {}).values()),
        "AWS::Lambda::Function": set((mappings.get("function_names", {}) or {}).values()),
        "AWS::SecretsManager::Secret": set((mappings.get("secret_names", {}) or {}).values()),
    }
    prop_keys = {
        "AWS::ECS::Cluster": "ClusterName",
        "AWS::SQS::Queue": "QueueName",
        "AWS::DynamoDB::Table": "TableName",
        "AWS::IAM::Role": "RoleName",
        "AWS::Lambda::Function": "FunctionName",
        "AWS::SecretsManager::Secret": "Name",
        "AWS::Logs::LogGroup": "LogGroupName",
    }
    type_labels = {
        "AWS::ECS::Cluster": "ECS cluster",
        "AWS::SQS::Queue": "SQS queue",
        "AWS::DynamoDB::Table": "DynamoDB table",
        "AWS::IAM::Role": "IAM role",
        "AWS::Lambda::Function": "Lambda function",
        "AWS::SecretsManager::Secret": "Secret",
        "AWS::Logs::LogGroup": "Log group",
    }
    for resource in (template.get("Resources", {}) or {}).values():
        resource_type = resource.get("Type")
        prop_key = prop_keys.get(resource_type)
        if not prop_key:
            continue
        props = resource.get("Properties", {}) or {}
        resource_name = props.get(prop_key)
        if isinstance(resource_name, dict) and resource_name.get("Ref"):
            resource_name = parameter_value(parameters, resource_name["Ref"], "")
        if resource_type == "AWS::Logs::LogGroup" and resource_name:
            return {
                "import_required": True,
                "reason": f"{type_labels[resource_type]} {resource_name} should be imported if it already exists in the target environment.",
            }
        if resource_name and resource_name in existing_values.get(resource_type, set()):
            return {
                "import_required": True,
                "reason": f"{type_labels[resource_type]} {resource_name} already exists in the target environment and is currently managed by the direct deploy flow.",
            }
    return {"import_required": False, "reason": ""}


def stack_exists(cf_client, stack_name):
    try:
        cf_client.describe_stacks(StackName=stack_name)
        return True
    except ClientError as exc:
        if "does not exist" in str(exc):
            return False
        raise


def describe_stack_status(cf_client, stack_name):
    try:
        stacks = cf_client.describe_stacks(StackName=stack_name).get("Stacks", [])
    except Exception:
        return ""
    if not stacks:
        return ""
    return stacks[0].get("StackStatus", "")


def wait_for_stack(cf_client, stack_name, expected_prefix):
    deadline = time.time() + 900
    while time.time() < deadline:
        stack = cf_client.describe_stacks(StackName=stack_name)["Stacks"][0]
        status = stack.get("StackStatus", "")
        if status.endswith("_COMPLETE"):
            return status
        if status.endswith("_FAILED") or "ROLLBACK" in status:
            raise RuntimeError(f"{stack_name} failed with status {status}")
        time.sleep(10)
    raise TimeoutError(f"Timed out waiting for {stack_name} to reach {expected_prefix}")


def deploy_stack(cf_client, stack_name, template_body, parameters):
    capabilities = ["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"]
    if stack_exists(cf_client, stack_name):
        try:
            cf_client.update_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=parameters,
                Capabilities=capabilities,
            )
            status = wait_for_stack(cf_client, stack_name, "UPDATE")
            return "updated", status
        except ClientError as exc:
            if "No updates are to be performed" in str(exc):
                return "no-op", "UPDATE_COMPLETE"
            raise
    cf_client.create_stack(
        StackName=stack_name,
        TemplateBody=template_body,
        Parameters=parameters,
        Capabilities=capabilities,
    )
    status = wait_for_stack(cf_client, stack_name, "CREATE")
    return "created", status


def recent_stack_failure_reason(cf_client, stack_name):
    try:
        events = cf_client.describe_stack_events(StackName=stack_name).get("StackEvents", [])
    except Exception:
        return ""
    fallback = ""
    for event in events:
        reason = event.get("ResourceStatusReason", "")
        if not reason:
            continue
        if "already exists" in reason.lower():
            return reason
        if not fallback:
            fallback = reason
    return fallback


def main():
    args = parse_args()
    source_env = args.source_env or "full-account-scan"
    config = load_transfer_config(args.config)
    client_slug = resolve_client_slug(args.client_slug, config, source_env=source_env, target_env=args.target_env)
    target_external_id = args.target_external_id or config_override(config, "target_external_id", "")
    inventory_dir, exports_manifest = load_template_exports(source_env, args.inventory_key, client_slug)
    deployment_manifest = load_deployment_manifest(args.target_env, args.deployment_key, client_slug)
    session = session_for(args.region, args.target_role_arn, external_id=target_external_id)
    cf_client = session.client("cloudformation")
    snapshot_manifest = build_snapshot_manifest(
        source_env=source_env,
        inventory_key=args.inventory_key,
        target_env=args.target_env,
        client_slug=client_slug,
        config_path=args.config,
    )

    results = []
    for item in exports_manifest.get("stacks", []):
        template_path = item.get("template_path")
        if not template_path:
            results.append({"source_stack": item.get("stack_name", ""), "error": item.get("error", "missing template_path")})
            continue
        source_stack_name = item.get("stack_name", "")
        target_name_value = target_stack_name(source_stack_name, args.target_env)
        template_body = load_template_body(template_path)
        parameters = build_parameter_overrides(template_body, deployment_manifest, args.target_env)
        conflict = classify_existing_resource_conflict(template_body, parameters, deployment_manifest)
        if conflict["import_required"]:
            results.append({
                "source_stack": source_stack_name,
                "target_stack": target_name_value,
                "operation": "skipped",
                "stack_status": describe_stack_status(cf_client, target_name_value) or "IMPORT_REQUIRED",
                "parameter_overrides": parameters,
                "import_required": True,
                "failure_reason": conflict["reason"],
            })
            continue
        try:
            operation, status = deploy_stack(cf_client, target_name_value, template_body, parameters)
        except Exception:
            operation = "failed"
            status = describe_stack_status(cf_client, target_name_value) or "FAILED"
        failure_reason = ""
        if "ROLLBACK" in status or status.endswith("FAILED"):
            failure_reason = recent_stack_failure_reason(cf_client, target_name_value)
        results.append({
            "source_stack": source_stack_name,
            "target_stack": target_name_value,
            "operation": operation,
            "stack_status": status,
            "parameter_overrides": parameters,
            "import_required": bool("already exists" in failure_reason.lower()),
            "failure_reason": failure_reason,
        })

    result_path = inventory_dir / f"cloudformation_deploy_result_{sanitize_name(args.target_env)}.json"
    result_path.write_text(json.dumps({"results": results, "pre_migration_snapshot": snapshot_manifest}, indent=2), encoding="utf-8")
    append_audit_event(
        "deploy_cloudformation_templates",
        "ok",
        {"result_path": str(result_path), "stack_count": len(results), "pre_migration_snapshot": snapshot_manifest["report_path"]},
        source_env=source_env,
        target_env=args.target_env,
        client_slug=client_slug,
    )
    print(json.dumps({"status": "ok", "result_path": str(result_path), "stack_count": len(results)}, indent=2))


if __name__ == "__main__":
    main()
