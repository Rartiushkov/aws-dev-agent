import argparse
import json
import re
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def sanitize_name(value):
    cleaned = re.sub(r"[^a-zA-Z0-9-_]+", "-", value.strip().lower())
    return re.sub(r"-{2,}", "-", cleaned).strip("-_")


def parse_args():
    parser = argparse.ArgumentParser(description="Deploy a new environment from a discovered snapshot.")
    parser.add_argument("--source-env", required=True)
    parser.add_argument("--target-env", required=True)
    parser.add_argument("--team", default="")
    parser.add_argument("--region", default="us-east-1")
    return parser.parse_args()


def load_snapshot(source_env):
    snapshot_path = Path("state") / "aws_inventory" / sanitize_name(source_env) / "source_snapshot.json"
    if not snapshot_path.exists():
        raise FileNotFoundError(f"Snapshot not found: {snapshot_path}")
    return snapshot_path, json.loads(snapshot_path.read_text(encoding="utf-8"))


def target_name(original_name, source_env, target_env, team):
    sanitized_source = sanitize_name(source_env)
    sanitized_target = sanitize_name(target_env)
    sanitized_team = sanitize_name(team) if team else ""

    updated = original_name
    if sanitized_source and sanitized_source in updated:
        updated = updated.replace(sanitized_source, sanitized_target, 1)
    else:
        updated = f"{sanitized_target}-{updated}"

    if sanitized_team and sanitized_team not in updated:
        updated = f"{updated}-{sanitized_team}"

    return updated[:64]


def update_env_values(variables, source_env, target_env, team):
    updated = {}
    for key, value in variables.items():
        if not isinstance(value, str):
            updated[key] = value
            continue
        new_value = value.replace(source_env, target_env) if source_env else value
        if team:
            new_value = new_value.replace("{team}", team)
        updated[key] = new_value
    return updated


def download_lambda_zip(location_url):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        with urllib.request.urlopen(location_url, timeout=60) as response:
            tmp.write(response.read())
        return Path(tmp.name)


def ensure_zip_is_readable(zip_path):
    with zipfile.ZipFile(zip_path, "r") as archive:
        archive.testzip()


def role_name_from_arn(role_arn):
    return role_arn.split("/")[-1]


def role_allows_lambda_assume(role_document):
    for statement in role_document.get("Statement", []):
        principal = statement.get("Principal", {})
        service = principal.get("Service")
        services = service if isinstance(service, list) else [service]
        if statement.get("Effect") != "Allow":
            continue
        if "sts:AssumeRole" not in str(statement.get("Action", "")):
            continue
        if any(item == "lambda.amazonaws.com" for item in services if item):
            return True
    return False


def resolve_execution_role(iam_client, source_role_arn):
    candidate_arns = []
    if source_role_arn:
        candidate_arns.append(source_role_arn)
    fallback_role_name = "lambda-basic-role"
    fallback_role_arn = f"arn:aws:iam::{iam_client.get_user()['User']['Arn'].split(':')[4]}:role/{fallback_role_name}"
    if fallback_role_arn not in candidate_arns:
        candidate_arns.append(fallback_role_arn)

    inspected = []
    for candidate_arn in candidate_arns:
        role_name = role_name_from_arn(candidate_arn)
        try:
            role = iam_client.get_role(RoleName=role_name)["Role"]
        except ClientError as exc:
            inspected.append({"role_arn": candidate_arn, "status": "missing", "details": str(exc)})
            continue

        assume_document = role.get("AssumeRolePolicyDocument", {})
        if role_allows_lambda_assume(assume_document):
            return {
                "selected_role_arn": role["Arn"],
                "fallback_used": role["Arn"] != source_role_arn,
                "inspected_roles": inspected,
            }

        inspected.append({
            "role_arn": role["Arn"],
            "status": "rejected",
            "details": "Role trust policy does not allow lambda.amazonaws.com",
        })

    raise RuntimeError(json.dumps({
        "message": "No Lambda-assumable execution role available",
        "inspected_roles": inspected,
    }))


def trim_create_payload(create_payload):
    payload = dict(create_payload)
    for key in ["PackageType", "Architectures", "EphemeralStorage"]:
        if not payload.get(key):
            payload.pop(key, None)
    if not payload.get("Environment", {}).get("Variables"):
        payload.pop("Environment", None)
    return payload


def deploy_lambda_functions(snapshot, source_env, target_env, team, region):
    session = boto3.session.Session(region_name=region)
    lambda_client = session.client("lambda")
    iam_client = session.client("iam")
    deployed = []
    failed = []

    for source_fn in snapshot.get("lambda_functions", []):
        source_name = source_fn["FunctionName"]
        target_fn = target_name(source_name, source_env, target_env, team)

        try:
            function_details = lambda_client.get_function(FunctionName=source_name)
            configuration = function_details["Configuration"]
            zip_path = download_lambda_zip(function_details["Code"]["Location"])
            ensure_zip_is_readable(zip_path)
            zip_bytes = zip_path.read_bytes()

            environment = configuration.get("Environment", {}).get("Variables", {})
            environment = update_env_values(environment, sanitize_name(source_env), sanitize_name(target_env), sanitize_name(team))
            role_resolution = resolve_execution_role(iam_client, configuration.get("Role"))

            create_payload = {
                "FunctionName": target_fn,
                "Runtime": configuration.get("Runtime"),
                "Role": role_resolution["selected_role_arn"],
                "Handler": configuration.get("Handler"),
                "Code": {"ZipFile": zip_bytes},
                "Description": f"Cloned from {source_name}",
                "Timeout": configuration.get("Timeout", 3),
                "MemorySize": configuration.get("MemorySize", 128),
                "Publish": False,
                "Environment": {"Variables": environment},
                "PackageType": configuration.get("PackageType", "Zip"),
                "Architectures": configuration.get("Architectures", ["x86_64"]),
                "EphemeralStorage": configuration.get("EphemeralStorage", {"Size": 512}),
            }

            vpc_config = configuration.get("VpcConfig") or {}
            if vpc_config.get("SubnetIds") and vpc_config.get("SecurityGroupIds"):
                create_payload["VpcConfig"] = {
                    "SubnetIds": vpc_config["SubnetIds"],
                    "SecurityGroupIds": vpc_config["SecurityGroupIds"],
                }

            create_payload = trim_create_payload(create_payload)

            try:
                lambda_client.create_function(**create_payload)
                operation = "created"
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceConflictException":
                    raise
                lambda_client.update_function_code(FunctionName=target_fn, ZipFile=zip_bytes, Publish=False)
                update_kwargs = {
                    "FunctionName": target_fn,
                    "Role": role_resolution["selected_role_arn"],
                    "Handler": configuration.get("Handler"),
                    "Description": f"Cloned from {source_name}",
                    "Timeout": configuration.get("Timeout", 3),
                    "MemorySize": configuration.get("MemorySize", 128),
                    "Environment": {"Variables": environment},
                }
                if "VpcConfig" in create_payload:
                    update_kwargs["VpcConfig"] = create_payload["VpcConfig"]
                lambda_client.update_function_configuration(**update_kwargs)
                operation = "updated"

            lambda_client.get_waiter("function_active_v2").wait(FunctionName=target_fn)
            lambda_client.get_waiter("function_updated").wait(FunctionName=target_fn)

            deployed.append({
                "source_function": source_name,
                "target_function": target_fn,
                "operation": operation,
                "execution_role": role_resolution["selected_role_arn"],
                "fallback_role_used": role_resolution["fallback_used"],
            })
        except Exception as exc:
            failed.append({
                "source_function": source_name,
                "target_function": target_fn,
                "error": str(exc),
            })

    return deployed, failed


def main():
    args = parse_args()
    snapshot_path, snapshot = load_snapshot(args.source_env)

    deployment_dir = Path("state") / "deployments" / sanitize_name(args.target_env)
    deployment_dir.mkdir(parents=True, exist_ok=True)

    deployed_lambdas, failed_lambdas = deploy_lambda_functions(snapshot, args.source_env, args.target_env, args.team, args.region)
    manifest = {
        "source_snapshot": str(snapshot_path),
        "source_env": sanitize_name(args.source_env),
        "target_env": sanitize_name(args.target_env),
        "team": sanitize_name(args.team) if args.team else "",
        "region": args.region,
        "lambda_functions": deployed_lambdas,
        "failed_lambda_functions": failed_lambdas,
        "follow_up": {
            "manual_review_required": bool(failed_lambdas),
            "cloudformation_stacks_review_required": len(snapshot.get("cloudformation_stacks", [])) > 0,
            "load_balancer_review_required": len(snapshot.get("load_balancers", [])) > 0,
            "ecs_review_required": len(snapshot.get("ecs", {}).get("services", [])) > 0,
        },
    }

    manifest_path = deployment_dir / "deployment_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(json.dumps({
        "status": "ok" if not failed_lambdas else "partial",
        "manifest_path": str(manifest_path),
        "deployed_lambda_count": len(deployed_lambdas),
        "failed_lambda_count": len(failed_lambdas),
    }, indent=2))


if __name__ == "__main__":
    main()
