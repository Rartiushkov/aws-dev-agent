import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from executor.scripts.audit_log import append_audit_event
from executor.scripts.deploy_cloudformation_templates import (
    build_parameter_overrides,
    load_deployment_manifest,
    parse_template_body,
    target_stack_name,
)
from executor.scripts.transfer_common import inventory_dir_path, resolve_client_slug


def parse_args():
    parser = argparse.ArgumentParser(description="Build an import-ready CloudFormation plan for existing migrated resources.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--target-env", required=True)
    parser.add_argument("--inventory-key", default="")
    parser.add_argument("--deployment-key", default="")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def argsafe(target_env=""):
    return target_env.strip().lower().replace(" ", "-")


def parameter_map(parameters):
    return {item.get("ParameterKey"): item.get("ParameterValue") for item in parameters or [] if item.get("ParameterKey")}


def resolve_value(value, parameters):
    if isinstance(value, dict) and value.get("Ref"):
        return parameters.get(value["Ref"], "")
    return value


def identify_resource(logical_id, resource, parameters, mappings):
    resource_type = resource.get("Type")
    props = resource.get("Properties", {}) or {}
    ecs_cluster_arns = list((mappings.get("ecs_cluster_arns", {}) or {}).values())
    queue_arns = mappings.get("queue_arns", {}) or {}
    table_arns = mappings.get("dynamodb_table_arns", {}) or {}
    role_arns = mappings.get("role_arns", {}) or {}
    function_arns = mappings.get("function_arns", {}) or {}
    secret_arns = mappings.get("secret_arns", {}) or {}
    identifier_values = {}
    physical_id = ""

    if resource_type == "AWS::ECS::Cluster":
        cluster_name = resolve_value(props.get("ClusterName"), parameters)
        if cluster_name:
            identifier_values["ClusterName"] = cluster_name
            physical_id = next((arn for arn in ecs_cluster_arns if arn.endswith(f"/{cluster_name}")), "")
    elif resource_type == "AWS::SQS::Queue":
        queue_name = resolve_value(props.get("QueueName"), parameters)
        if queue_name:
            identifier_values["QueueName"] = queue_name
            physical_id = next((arn for arn in queue_arns.values() if arn.endswith(f":{queue_name}")), "")
    elif resource_type == "AWS::DynamoDB::Table":
        table_name = resolve_value(props.get("TableName"), parameters)
        if table_name:
            identifier_values["TableName"] = table_name
            physical_id = next((arn for arn in table_arns.values() if arn.endswith(f"/{table_name}")), "")
    elif resource_type == "AWS::IAM::Role":
        role_name = resolve_value(props.get("RoleName"), parameters)
        if role_name:
            identifier_values["RoleName"] = role_name
            physical_id = next((arn for arn in role_arns.values() if arn.endswith(f"/{role_name}")), "")
    elif resource_type == "AWS::Lambda::Function":
        function_name = resolve_value(props.get("FunctionName"), parameters)
        if function_name:
            identifier_values["FunctionName"] = function_name
            physical_id = next((arn for arn in function_arns.values() if arn.endswith(f":function:{function_name}")), "")
    elif resource_type == "AWS::Logs::LogGroup":
        log_group_name = resolve_value(props.get("LogGroupName"), parameters)
        if log_group_name:
            identifier_values["LogGroupName"] = log_group_name
            physical_id = log_group_name
    elif resource_type == "AWS::SecretsManager::Secret":
        secret_name = resolve_value(props.get("Name"), parameters)
        if secret_name:
            identifier_values["Name"] = secret_name
            physical_id = next((arn for arn in secret_arns.values() if f":secret:{secret_name}" in arn), "")

    return {
        "logical_id": logical_id,
        "resource_type": resource_type,
        "identifier_values": identifier_values,
        "physical_id": physical_id,
        "importable": bool(identifier_values),
    }


def build_import_plan(exports, deployment_manifest, target_env):
    mappings = (deployment_manifest.get("resource_mappings", {}) or {})
    results = []
    for item in exports.get("stacks", []):
        template_path = item.get("template_path", "")
        if not template_path:
            continue
        template = parse_template_body(Path(template_path).read_text(encoding="utf-8"))
        parameters = build_parameter_overrides(json.dumps(template), deployment_manifest, target_env)
        parameters_by_key = parameter_map(parameters)
        resources = []
        for logical_id, resource in (template.get("Resources", {}) or {}).items():
            resources.append(identify_resource(logical_id, resource, parameters_by_key, mappings))
        results.append({
            "source_stack": item.get("stack_name", ""),
            "target_stack": target_stack_name(item.get("stack_name", ""), target_env),
            "template_path": template_path,
            "parameter_overrides": parameters,
            "resources": resources,
            "import_required": any(item.get("importable") for item in resources),
        })
    return {"stacks": results}


def main():
    args = parse_args()
    source_env = args.source_env or "full-account-scan"
    client_slug = resolve_client_slug(args.client_slug, source_env=source_env, target_env=args.target_env)
    inventory_dir = inventory_dir_path(source_env, args.inventory_key, client_slug)
    exports = json.loads((inventory_dir / "cloudformation_template_exports.json").read_text(encoding="utf-8"))
    deployment_manifest = load_deployment_manifest(args.target_env, args.deployment_key, client_slug=client_slug)
    plan = build_import_plan(exports, deployment_manifest, args.target_env)
    plan_path = inventory_dir / f"cloudformation_import_plan_{argsafe(args.target_env)}.json"
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    append_audit_event(
        "build_cloudformation_import_plan",
        "ok",
        {"plan_path": str(plan_path), "stack_count": len(plan["stacks"])},
        source_env=source_env,
        target_env=args.target_env,
        client_slug=client_slug,
    )
    print(json.dumps({"status": "ok", "plan_path": str(plan_path), "stack_count": len(plan["stacks"])}, indent=2))


if __name__ == "__main__":
    main()
