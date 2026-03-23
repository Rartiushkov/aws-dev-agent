import argparse
import json
import re
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import boto3
from botocore.exceptions import ClientError
from executor.scripts.transfer_common import (
    config_override,
    deployment_dir_name,
    deployment_dir_path,
    ensure_target_scope_safe,
    inventory_dir_name,
    inventory_dir_path,
    load_transfer_config,
    resolve_client_slug,
    session_for,
    should_exclude,
)
from executor.scripts.agent_memory import suggest_known_fixes
from executor.scripts.audit_log import append_audit_event


def sanitize_name(value):
    cleaned = re.sub(r"[^a-zA-Z0-9-_]+", "-", value.strip().lower())
    return re.sub(r"-{2,}", "-", cleaned).strip("-_")


def parse_args():
    parser = argparse.ArgumentParser(description="Deploy a new environment from a discovered snapshot.")
    parser.add_argument("--source-env", required=True)
    parser.add_argument("--target-env", required=True)
    parser.add_argument("--team", default="")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--source-region", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--source-role-arn", default="")
    parser.add_argument("--target-role-arn", default="")
    parser.add_argument("--source-external-id", default="")
    parser.add_argument("--target-external-id", default="")
    parser.add_argument("--inventory-key", default="")
    parser.add_argument("--deployment-key", default="")
    parser.add_argument("--read-only-plan", action="store_true")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def load_snapshot(source_env, inventory_key="", client_slug=""):
    snapshot_path = inventory_dir_path(source_env, inventory_key, client_slug) / "source_snapshot.json"
    if not snapshot_path.exists():
        raise FileNotFoundError(f"Snapshot not found: {snapshot_path}")
    return snapshot_path, json.loads(snapshot_path.read_text(encoding="utf-8"))


def collect_string_references(value, needle, path="$"):
    matches = []
    if isinstance(value, dict):
        for key, nested in value.items():
            matches.extend(collect_string_references(nested, needle, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            matches.extend(collect_string_references(nested, needle, f"{path}[{index}]"))
    elif isinstance(value, str) and needle and needle in value:
        matches.append({"path": path, "value": value[:200]})
    return matches


def build_preflight_assessment(snapshot, config, source_account_id="", target_account_id="", source_region="", target_region="", client_slug=""):
    checks = []
    same_account = bool(source_account_id and target_account_id and source_account_id == target_account_id)
    same_region = bool(source_region and target_region and source_region == target_region)
    hardcoded_refs = collect_string_references(snapshot, source_account_id) if source_account_id else []

    scope_check = {
        "name": "scope",
        "status": "ok" if not (same_account and same_region) else "warning",
        "details": {
            "source_account_id": source_account_id,
            "target_account_id": target_account_id,
            "source_region": source_region,
            "target_region": target_region,
            "same_account": same_account,
            "same_region": same_region,
        },
    }
    if scope_check["status"] != "ok":
        scope_check["known_fixes"] = suggest_known_fixes(
            "same account same region target writes refused allow_same_scope override",
            client_slug=client_slug,
        )
    checks.append(scope_check)

    kms_mapping = (config or {}).get("overrides", {}).get("kms_key_mapping", {})
    kms_refs = []
    for queue in snapshot.get("sqs_queues", []):
        key_id = queue.get("Attributes", {}).get("KmsMasterKeyId")
        if key_id:
            kms_refs.append(key_id)
    for secret in snapshot.get("secrets", []):
        key_id = secret.get("KmsKeyId")
        if key_id:
            kms_refs.append(key_id)
    for project in snapshot.get("codebuild_projects", []):
        key_id = project.get("encryptionKey")
        if key_id:
            kms_refs.append(key_id)
    for bucket in snapshot.get("s3_buckets", []):
        encryption = bucket.get("BucketEncryption", {})
        for rule in encryption.get("Rules", []):
            key_id = rule.get("ApplyServerSideEncryptionByDefault", {}).get("KMSMasterKeyID")
            if key_id:
                kms_refs.append(key_id)
    unresolved_kms = [
        key_id for key_id in kms_refs
        if isinstance(key_id, str)
        and source_region
        and target_region
        and source_region != target_region
        and f":{source_region}:" in key_id
        and key_id not in kms_mapping
        and not (key_id.startswith("alias/") and key_id in kms_mapping)
    ]
    kms_check = {
        "name": "kms-remap",
        "status": "ok" if not unresolved_kms else "warning",
        "details": {
            "unresolved_reference_count": len(unresolved_kms),
            "sample": unresolved_kms[:10],
        },
    }
    if kms_check["status"] != "ok":
        kms_check["known_fixes"] = suggest_known_fixes(
            "kms target region mapping unresolved alias aws s3 cross region",
            client_slug=client_slug,
        )
    checks.append(kms_check)

    ecs_services = snapshot.get("ecs", {}).get("services", [])
    network_dependencies = [
        service.get("serviceName")
        for service in ecs_services
        if service.get("networkConfiguration", {}).get("awsvpcConfiguration", {}).get("subnets")
    ]
    ecs_network_check = {
        "name": "ecs-network-dependencies",
        "status": "ok" if not network_dependencies or snapshot.get("vpcs") else "warning",
        "details": {
            "service_count": len(network_dependencies),
            "services": network_dependencies[:10],
            "vpc_count": len(snapshot.get("vpcs", [])),
            "subnet_count": len(snapshot.get("subnets", [])),
        },
    }
    if ecs_network_check["status"] != "ok":
        ecs_network_check["known_fixes"] = suggest_known_fixes(
            "ecs source subnet ids reused adopt existing vpc subnets missing target vpc",
            client_slug=client_slug,
        )
    checks.append(ecs_network_check)

    hardcoded_ref_check = {
        "name": "hardcoded-source-account-references",
        "status": "ok" if not hardcoded_refs or same_account else "warning",
        "details": {
            "count": len(hardcoded_refs),
            "sample": hardcoded_refs[:10],
        },
    }
    if hardcoded_ref_check["status"] != "ok":
        hardcoded_ref_check["known_fixes"] = suggest_known_fixes(
            "hardcoded source account arn rewrite target account cross account migration",
            client_slug=client_slug,
        )
    checks.append(hardcoded_ref_check)
    return checks


def build_read_only_plan(snapshot, source_env, target_env, team, config=None, source_account_id="", target_account_id="", source_region="", target_region="", client_slug=""):
    return {
        "mode": "read-only-assessment",
        "source_env": sanitize_name(source_env),
        "target_env": sanitize_name(target_env),
        "team": sanitize_name(team) if team else "",
        "planned_actions": {
            "roles": len(snapshot.get("iam_roles", [])),
            "sqs_queues": len(snapshot.get("sqs_queues", [])),
            "sns_topics": len(snapshot.get("sns_topics", [])),
            "secrets": len(snapshot.get("secrets", [])),
            "dynamodb_tables": len(snapshot.get("dynamodb_tables", [])),
            "lambda_functions": len(snapshot.get("lambda_functions", [])),
            "lambda_event_source_mappings": len(snapshot.get("lambda_event_source_mappings", [])),
            "lambda_permissions": len(snapshot.get("lambda_permissions", [])),
            "sns_subscriptions": sum(len(topic.get("Subscriptions", [])) for topic in snapshot.get("sns_topics", [])),
            "api_gateways": len(snapshot.get("api_gateways", [])),
            "codebuild_projects": len(snapshot.get("codebuild_projects", [])),
            "ecs_clusters": len(snapshot.get("ecs", {}).get("clusters", [])),
            "ecs_task_definitions": len(snapshot.get("ecs", {}).get("task_definitions", [])),
            "ecs_services": len(snapshot.get("ecs", {}).get("services", [])),
        },
        "manual_review": {
            "cloudformation_stacks": len(snapshot.get("cloudformation_stacks", [])),
            "s3_buckets": len(snapshot.get("s3_buckets", [])),
            "load_balancers": len(snapshot.get("load_balancers", [])),
            "rds_instances": len(snapshot.get("rds", {}).get("instances", [])),
            "rds_clusters": len(snapshot.get("rds", {}).get("clusters", [])),
        },
        "preflight_checks": build_preflight_assessment(
            snapshot,
            config or {},
            source_account_id=source_account_id,
            target_account_id=target_account_id,
            source_region=source_region,
            target_region=target_region,
            client_slug=client_slug,
        ),
    }


def target_name(original_name, source_env, target_env, team, preserve_names=False):
    if preserve_names:
        return original_name[:64]
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


def queue_target_name(queue_name, source_env, target_env, team, preserve_names=False):
    suffix = ".fifo" if queue_name.endswith(".fifo") else ""
    base_name = queue_name[:-5] if suffix else queue_name
    return f"{target_name(base_name, source_env, target_env, team, preserve_names=preserve_names)}{suffix}"[:80]


def role_name_from_arn(role_arn):
    return role_arn.split("/")[-1]


def function_name_from_arn(function_arn):
    return function_arn.split(":")[-1]


def should_skip_recloning(resource_name, target_env, team):
    target_prefix = sanitize_name(target_env)
    team_suffix = f"-{sanitize_name(team)}" if team else ""
    lowered = resource_name.lower()
    if lowered.startswith(f"{target_prefix}-"):
        return True
    if team_suffix and lowered.endswith(team_suffix):
        return True
    return False


def rewrite_string_value(value, mappings, source_env, target_env, team):
    if not isinstance(value, str):
        return value
    updated = value
    source_account_id = mappings.get("source_account_id", "")
    target_account_id = mappings.get("target_account_id", "")
    for original, replacement in mappings.get("queue_urls", {}).items():
        updated = updated.replace(original, replacement)
    for original, replacement in mappings.get("queue_arns", {}).items():
        updated = updated.replace(original, replacement)
    for original, replacement in mappings.get("topic_arns", {}).items():
        updated = updated.replace(original, replacement)
    for original, replacement in mappings.get("secret_arns", {}).items():
        updated = updated.replace(original, replacement)
    for original, replacement in mappings.get("secret_names", {}).items():
        updated = updated.replace(original, replacement)
    for original, replacement in mappings.get("dynamodb_table_arns", {}).items():
        updated = updated.replace(original, replacement)
    for original, replacement in mappings.get("dynamodb_table_names", {}).items():
        updated = updated.replace(original, replacement)
    for original, replacement in mappings.get("dynamodb_stream_arns", {}).items():
        updated = updated.replace(original, replacement)
    for original, replacement in mappings.get("function_arns", {}).items():
        updated = updated.replace(original, replacement)
    for original, replacement in mappings.get("role_arns", {}).items():
        updated = updated.replace(original, replacement)
    for original, replacement in mappings.get("s3_bucket_names", {}).items():
        updated = updated.replace(original, replacement)
    for original, replacement in mappings.get("s3_bucket_arns", {}).items():
        updated = updated.replace(original, replacement)
    for original, replacement in mappings.get("kms_key_ids", {}).items():
        updated = updated.replace(original, replacement)
    for original, replacement in mappings.get("kms_aliases", {}).items():
        updated = updated.replace(original, replacement)
    if source_account_id and target_account_id and source_account_id != target_account_id:
        updated = updated.replace(f":{source_account_id}:", f":{target_account_id}:")
        updated = updated.replace(f"/{source_account_id}/", f"/{target_account_id}/")
        if updated == source_account_id:
            updated = target_account_id
    if source_env:
        updated = updated.replace(source_env, target_env)
    if team:
        updated = updated.replace("{team}", team)
    return updated


def rewrite_structure(value, mappings, source_env, target_env, team):
    if isinstance(value, dict):
        return {
            key: rewrite_structure(nested, mappings, source_env, target_env, team)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [rewrite_structure(item, mappings, source_env, target_env, team) for item in value]
    if isinstance(value, str):
        return rewrite_string_value(value, mappings, source_env, target_env, team)
    return value


def update_env_values(variables, mappings, source_env, target_env, team):
    updated = {}
    target_region = mappings.get("target_region", "")
    for key, value in variables.items():
        if key in {"REGION", "AWS_REGION", "AWS_DEFAULT_REGION"} and target_region:
            updated[key] = target_region
            continue
        updated[key] = rewrite_string_value(value, mappings, source_env, target_env, team)
    return updated


def download_lambda_zip(location_url):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        with urllib.request.urlopen(location_url, timeout=60) as response:
            tmp.write(response.read())
        return Path(tmp.name)


def ensure_zip_is_readable(zip_path):
    with zipfile.ZipFile(zip_path, "r") as archive:
        archive.testzip()


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


def trim_create_payload(create_payload):
    payload = dict(create_payload)
    for key in ["PackageType", "Architectures", "EphemeralStorage"]:
        if not payload.get(key):
            payload.pop(key, None)
    if not payload.get("Environment", {}).get("Variables"):
        payload.pop("Environment", None)
    return payload


def rewrite_kms_reference(value, resource_mappings):
    if not value:
        return value
    if value.startswith("alias/"):
        return resource_mappings.get("kms_aliases", {}).get(value, value)
    return resource_mappings.get("kms_key_ids", {}).get(value, value)


def tag_value(tags, key):
    for tag in tags or []:
        if tag.get("Key") == key:
            return tag.get("Value")
    return ""


def build_tag_list(existing_tags, name):
    tags = [tag for tag in (existing_tags or []) if tag.get("Key") != "Name"]
    tags.append({"Key": "Name", "Value": name})
    return tags


def network_target_name(source_name, source_env, target_env, team, preserve_names=False):
    return target_name(source_name or "resource", source_env, target_env, team, preserve_names=preserve_names)


def find_vpc_by_name_or_cidr(ec2_client, target_vpc_name, cidr_block):
    candidates = []
    try:
        candidates.extend(
            ec2_client.describe_vpcs(
                Filters=[{"Name": "tag:Name", "Values": [target_vpc_name]}]
            ).get("Vpcs", [])
        )
    except Exception:
        pass
    try:
        candidates.extend(
            ec2_client.describe_vpcs(
                Filters=[{"Name": "cidr-block", "Values": [cidr_block]}]
            ).get("Vpcs", [])
        )
    except Exception:
        pass
    seen = set()
    for vpc in candidates:
        vpc_id = vpc.get("VpcId")
        if vpc_id and vpc_id not in seen:
            seen.add(vpc_id)
            return vpc
    return None


def find_subnet_by_name_or_cidr(ec2_client, target_vpc_id, target_subnet_name, cidr_block):
    candidates = []
    try:
        candidates.extend(
            ec2_client.describe_subnets(
                Filters=[
                    {"Name": "vpc-id", "Values": [target_vpc_id]},
                    {"Name": "tag:Name", "Values": [target_subnet_name]},
                ]
            ).get("Subnets", [])
        )
    except Exception:
        pass
    try:
        candidates.extend(
            ec2_client.describe_subnets(
                Filters=[
                    {"Name": "vpc-id", "Values": [target_vpc_id]},
                    {"Name": "cidr-block", "Values": [cidr_block]},
                ]
            ).get("Subnets", [])
        )
    except Exception:
        pass
    seen = set()
    for subnet in candidates:
        subnet_id = subnet.get("SubnetId")
        if subnet_id and subnet_id not in seen:
            seen.add(subnet_id)
            return subnet
    return None


def remap_vpc_config(vpc_config, resource_mappings):
    subnet_ids = [
        resource_mappings.get("subnet_ids", {}).get(subnet_id, subnet_id)
        for subnet_id in vpc_config.get("SubnetIds", [])
    ]
    security_group_ids = [
        resource_mappings.get("security_group_ids", {}).get(group_id, group_id)
        for group_id in vpc_config.get("SecurityGroupIds", [])
    ]
    if subnet_ids and security_group_ids:
        return {"SubnetIds": subnet_ids, "SecurityGroupIds": security_group_ids}
    return {}


def build_kms_mappings(snapshot, config):
    configured = config.get("overrides", {}).get("kms_key_mapping", {}) if config else {}
    key_ids = dict(configured)
    aliases = {}
    for secret in snapshot.get("secrets", []):
        key_id = secret.get("KmsKeyId")
        if key_id and key_id.startswith("alias/"):
            aliases[key_id] = configured.get(key_id, key_id)
    for queue in snapshot.get("sqs_queues", []):
        key_id = queue.get("Attributes", {}).get("KmsMasterKeyId")
        if key_id and key_id.startswith("alias/"):
            aliases[key_id] = configured.get(key_id, key_id)
    for bucket in snapshot.get("s3_buckets", []):
        encryption = bucket.get("BucketEncryption", {})
        for rule in encryption.get("Rules", []):
            key_id = rule.get("ApplyServerSideEncryptionByDefault", {}).get("KMSMasterKeyID")
            if key_id and key_id.startswith("alias/"):
                aliases[key_id] = configured.get(key_id, key_id)
    return {"kms_key_ids": key_ids, "kms_aliases": aliases}


def rewrite_bucket_name(bucket_name, source_env, target_env, source_account_id="", target_account_id="", preserve_names=False):
    if not bucket_name:
        return bucket_name
    if preserve_names:
        return bucket_name
    updated = bucket_name
    if source_account_id and target_account_id and source_account_id in updated:
        updated = updated.replace(source_account_id, target_account_id)
    sanitized_source = sanitize_name(source_env) if source_env else ""
    sanitized_target = sanitize_name(target_env) if target_env else ""
    if sanitized_source and sanitized_source in updated:
        updated = updated.replace(sanitized_source, sanitized_target)
    elif sanitized_target and updated == bucket_name and source_account_id == target_account_id:
        updated = f"{sanitized_target}-{updated}"
    updated = re.sub(r"[^a-z0-9.-]+", "-", updated.lower())
    updated = re.sub(r"-{2,}", "-", updated).strip("-.")
    return updated[:63]


def build_s3_bucket_mappings(snapshot, source_env, target_env, source_account_id="", target_account_id="", preserve_names=False):
    bucket_names = {}
    bucket_arns = {}
    for bucket in snapshot.get("s3_buckets", []):
        source_bucket = bucket.get("Name", "")
        if not source_bucket:
            continue
        target_bucket = rewrite_bucket_name(
            source_bucket,
            source_env,
            target_env,
            source_account_id=source_account_id,
            target_account_id=target_account_id,
            preserve_names=preserve_names,
        )
        bucket_names[source_bucket] = target_bucket
        bucket_arns[f"arn:aws:s3:::{source_bucket}"] = f"arn:aws:s3:::{target_bucket}"
        bucket_arns[f"arn:aws:s3:::{source_bucket}/*"] = f"arn:aws:s3:::{target_bucket}/*"
    return {"s3_bucket_names": bucket_names, "s3_bucket_arns": bucket_arns}


def build_synthetic_lambda_roles(snapshot):
    roles = []
    seen = set()

    def add_role(role_arn, service_principal, managed_policies, description):
        if not role_arn or role_arn in seen:
            return
        seen.add(role_arn)
        role_name = role_arn.split("/")[-1]
        roles.append({
            "RoleName": role_name,
            "Arn": role_arn,
            "AssumeRolePolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": service_principal},
                        "Action": "sts:AssumeRole",
                    }
                ],
            },
            "Description": description,
            "Path": "/",
            "ManagedPolicies": [{"PolicyArn": arn} for arn in managed_policies],
            "InlinePolicies": [],
        })

    for fn in snapshot.get("lambda_functions", []):
        add_role(
            fn.get("Role"),
            "lambda.amazonaws.com",
            [
                "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                "arn:aws:iam::aws:policy/service-role/AWSLambdaSQSQueueExecutionRole",
            ],
            "Synthesized Lambda execution role because source role metadata was unavailable.",
        )

    for project in snapshot.get("codebuild_projects", []):
        add_role(
            project.get("serviceRole"),
            "codebuild.amazonaws.com",
            [
                "arn:aws:iam::aws:policy/AdministratorAccess",
            ],
            "Synthesized CodeBuild service role because source role metadata was unavailable.",
        )

    for task_def in snapshot.get("ecs", {}).get("task_definitions", []):
        add_role(
            task_def.get("executionRoleArn"),
            "ecs-tasks.amazonaws.com",
            [
                "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
            ],
            "Synthesized ECS execution role because source role metadata was unavailable.",
        )
        add_role(
            task_def.get("taskRoleArn"),
            "ecs-tasks.amazonaws.com",
            [
                "arn:aws:iam::aws:policy/AdministratorAccess",
            ],
            "Synthesized ECS task role because source role metadata was unavailable.",
        )

    return roles


def remap_ecs_network_configuration(network_configuration, resource_mappings):
    if not network_configuration:
        return network_configuration
    updated = dict(network_configuration)
    awsvpc = dict(updated.get("awsvpcConfiguration", {}))
    if awsvpc:
        awsvpc["subnets"] = [resource_mappings.get("subnet_ids", {}).get(item, item) for item in awsvpc.get("subnets", [])]
        awsvpc["securityGroups"] = [resource_mappings.get("security_group_ids", {}).get(item, item) for item in awsvpc.get("securityGroups", [])]
        updated["awsvpcConfiguration"] = awsvpc
    return updated


def rewrite_queue_attributes(attributes, resource_mappings, source_env, target_env, team):
    rewritten = dict(attributes)
    for key in ["Policy", "RedriveAllowPolicy"]:
        if key in rewritten:
            rewritten[key] = rewrite_string_value(
                rewritten[key],
                resource_mappings,
                source_env,
                target_env,
                team,
            )
    if "RedrivePolicy" in rewritten:
        try:
            policy = json.loads(rewritten["RedrivePolicy"])
            dead_letter_arn = policy.get("deadLetterTargetArn")
            if dead_letter_arn:
                policy["deadLetterTargetArn"] = resource_mappings.get("queue_arns", {}).get(dead_letter_arn, dead_letter_arn)
            rewritten["RedrivePolicy"] = json.dumps(policy, separators=(",", ":"))
        except json.JSONDecodeError:
            rewritten["RedrivePolicy"] = rewrite_string_value(
                rewritten["RedrivePolicy"],
                resource_mappings,
                source_env,
                target_env,
                team,
            )
    if "KmsMasterKeyId" in rewritten:
        rewritten["KmsMasterKeyId"] = rewrite_kms_reference(rewritten["KmsMasterKeyId"], resource_mappings)
    return rewritten


def required_queue_visibility_by_source(snapshot):
    requirements = {}
    timeout_by_function = {
        fn["FunctionName"]: fn.get("Timeout", 30)
        for fn in snapshot.get("lambda_functions", [])
    }
    for mapping in snapshot.get("lambda_event_source_mappings", []):
        source_arn = mapping.get("EventSourceArn", "")
        if ":sqs:" not in source_arn:
            continue
        function_name = function_name_from_arn(mapping.get("FunctionArn", ""))
        function_timeout = timeout_by_function.get(function_name, 30)
        requirements[source_arn] = max(
            requirements.get(source_arn, 0),
            max(function_timeout, 30) + 30,
        )
    return requirements


def queue_dependencies_resolved(attributes, resource_mappings):
    redrive_policy = attributes.get("RedrivePolicy")
    if not redrive_policy:
        return True
    try:
        policy = json.loads(redrive_policy)
    except json.JSONDecodeError:
        return True
    dead_letter_arn = policy.get("deadLetterTargetArn")
    if not dead_letter_arn:
        return True
    mapped_queue_arns = resource_mappings.get("queue_arns", {})
    if dead_letter_arn in mapped_queue_arns or dead_letter_arn in mapped_queue_arns.values():
        return True
    return ":sqs:" not in dead_letter_arn


def create_or_update_roles(snapshot, source_env, target_env, team, iam_client, preserve_names=False, config=None):
    mappings = {"role_arns": {}, "role_names": {}}
    deployed = []
    failed = []
    sanitized_source = sanitize_name(source_env)
    sanitized_target = sanitize_name(target_env)
    sanitized_team = sanitize_name(team) if team else ""
    for role in snapshot.get("iam_roles", []):
        source_role_name = role["RoleName"]
        if should_exclude("iam_roles", source_role_name, config or {}):
            continue
        if should_skip_recloning(source_role_name, target_env, team):
            continue
        target_role_name = target_name(source_role_name, source_env, target_env, team, preserve_names=preserve_names)
        assume_role_policy = rewrite_structure(
            role.get("AssumeRolePolicyDocument", {}),
            mappings,
            sanitized_source,
            sanitized_target,
            sanitized_team,
        )
        try:
            try:
                response = iam_client.create_role(
                    RoleName=target_role_name,
                    AssumeRolePolicyDocument=json.dumps(assume_role_policy),
                    Description=f"Cloned from {source_role_name}",
                    Path=role.get("Path", "/"),
                )
                target_role_arn = response["Role"]["Arn"]
                operation = "created"
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "EntityAlreadyExists":
                    raise
                target_role_arn = iam_client.get_role(RoleName=target_role_name)["Role"]["Arn"]
                iam_client.update_assume_role_policy(
                    RoleName=target_role_name,
                    PolicyDocument=json.dumps(assume_role_policy),
                )
                operation = "updated"
            attached = iam_client.list_attached_role_policies(RoleName=target_role_name).get("AttachedPolicies", [])
            attached_arns = {item["PolicyArn"] for item in attached}
            for policy in role.get("ManagedPolicies", []):
                target_policy_arn = rewrite_string_value(
                    policy["PolicyArn"],
                    mappings,
                    sanitized_source,
                    sanitized_target,
                    sanitized_team,
                )
                if target_policy_arn not in attached_arns:
                    iam_client.attach_role_policy(RoleName=target_role_name, PolicyArn=target_policy_arn)
            for policy in role.get("InlinePolicies", []):
                iam_client.put_role_policy(
                    RoleName=target_role_name,
                    PolicyName=policy["PolicyName"],
                    PolicyDocument=json.dumps(
                        rewrite_structure(
                            policy["PolicyDocument"],
                            mappings,
                            sanitized_source,
                            sanitized_target,
                            sanitized_team,
                        )
                    ),
                )
            mappings["role_arns"][role["Arn"]] = target_role_arn
            mappings["role_names"][source_role_name] = target_role_name
            deployed.append({
                "source_role": source_role_name,
                "target_role": target_role_name,
                "target_role_arn": target_role_arn,
                "operation": operation,
            })
        except Exception as exc:
            failed.append({"source_role": source_role_name, "target_role": target_role_name, "error": str(exc)})
    return mappings, deployed, failed


def create_or_update_sqs_queues(snapshot, source_env, target_env, team, sqs_client, resource_mappings, preserve_names=False, config=None):
    mappings = {"queue_urls": {}, "queue_arns": {}, "queue_names": {}}
    deployed = []
    failed = []
    allowed_attributes = {
        "DelaySeconds", "MaximumMessageSize", "MessageRetentionPeriod", "Policy",
        "ReceiveMessageWaitTimeSeconds", "RedrivePolicy", "RedriveAllowPolicy",
        "VisibilityTimeout", "FifoQueue", "ContentBasedDeduplication",
        "DeduplicationScope", "FifoThroughputLimit", "KmsMasterKeyId",
        "KmsDataKeyReusePeriodSeconds", "SqsManagedSseEnabled",
    }
    required_visibility = required_queue_visibility_by_source(snapshot)
    queue_items = []
    for queue in snapshot.get("sqs_queues", []):
        source_name = queue["QueueName"]
        if should_exclude("sqs_queues", source_name, config or {}):
            continue
        if should_skip_recloning(source_name, target_env, team):
            continue
        queue_items.append(queue)

    pending = list(queue_items)
    while pending:
        progressed = False
        next_pending = []
        for queue in pending:
            source_name = queue["QueueName"]
            target_queue_name = queue_target_name(source_name, source_env, target_env, team, preserve_names=preserve_names)
            attributes = {k: v for k, v in queue.get("Attributes", {}).items() if k in allowed_attributes}
            attributes = rewrite_queue_attributes(
                attributes,
                resource_mappings,
                sanitize_name(source_env),
                sanitize_name(target_env),
                sanitize_name(team),
            )
            required_timeout = required_visibility.get(queue.get("Attributes", {}).get("QueueArn"))
            current_timeout = int(attributes.get("VisibilityTimeout", "30"))
            if required_timeout and current_timeout < required_timeout:
                attributes["VisibilityTimeout"] = str(required_timeout)
            if not queue_dependencies_resolved(attributes, resource_mappings):
                next_pending.append(queue)
                continue
            try:
                response = sqs_client.create_queue(QueueName=target_queue_name, Attributes=attributes, tags=queue.get("Tags", {}))
                queue_url = response["QueueUrl"]
                operation = "created"
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "QueueAlreadyExists":
                    failed.append({"source_queue": source_name, "target_queue": target_queue_name, "error": str(exc)})
                    continue
                queue_url = sqs_client.get_queue_url(QueueName=target_queue_name)["QueueUrl"]
                if attributes:
                    sqs_client.set_queue_attributes(QueueUrl=queue_url, Attributes=attributes)
                operation = "updated"
            queue_arn = sqs_client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
            mappings["queue_urls"][queue["QueueUrl"]] = queue_url
            mappings["queue_arns"][queue["Attributes"]["QueueArn"]] = queue_arn
            mappings["queue_names"][source_name] = target_queue_name
            resource_mappings.setdefault("queue_urls", {}).update(mappings["queue_urls"])
            resource_mappings.setdefault("queue_arns", {}).update(mappings["queue_arns"])
            resource_mappings.setdefault("queue_names", {}).update(mappings["queue_names"])
            deployed.append({
                "source_queue": source_name,
                "target_queue": target_queue_name,
                "target_queue_url": queue_url,
                "target_queue_arn": queue_arn,
                "operation": operation,
            })
            progressed = True
        if not progressed:
            for queue in next_pending:
                source_name = queue["QueueName"]
                target_queue_name = queue_target_name(source_name, source_env, target_env, team, preserve_names=preserve_names)
                failed.append({
                    "source_queue": source_name,
                    "target_queue": target_queue_name,
                    "error": "Queue dependencies could not be resolved for cross-region recreation",
                })
            break
        pending = next_pending
    return mappings, deployed, failed


def create_or_update_sns_topics(snapshot, source_env, target_env, team, sns_client, preserve_names=False, config=None):
    mappings = {"topic_arns": {}, "topic_names": {}}
    deployed = []
    failed = []
    for topic in snapshot.get("sns_topics", []):
        source_name = topic["TopicName"]
        if should_exclude("sns_topics", source_name, config or {}):
            continue
        if should_skip_recloning(source_name, target_env, team):
            continue
        target_topic_name = target_name(source_name, source_env, target_env, team, preserve_names=preserve_names)
        attributes = {}
        if topic.get("Attributes", {}).get("FifoTopic") == "true":
            if not target_topic_name.endswith(".fifo"):
                target_topic_name = f"{target_topic_name}.fifo"
            attributes["FifoTopic"] = "true"
        try:
            response = sns_client.create_topic(Name=target_topic_name, Attributes=attributes)
            target_topic_arn = response["TopicArn"]
            mappings["topic_arns"][topic["TopicArn"]] = target_topic_arn
            mappings["topic_names"][source_name] = target_topic_name
            deployed.append({
                "source_topic": source_name,
                "target_topic": target_topic_name,
                "target_topic_arn": target_topic_arn,
                "operation": "created_or_verified",
            })
        except Exception as exc:
            failed.append({"source_topic": source_name, "target_topic": target_topic_name, "error": str(exc)})
    return mappings, deployed, failed


def create_or_update_secrets(snapshot, source_env, target_env, team, source_secrets_client, target_secrets_client, resource_mappings, preserve_names=False, config=None):
    mappings = {"secret_arns": {}, "secret_names": {}}
    deployed = []
    failed = []
    for secret in snapshot.get("secrets", []):
        source_name = secret["Name"]
        if should_exclude("secrets", source_name, config or {}):
            continue
        if should_skip_recloning(source_name, target_env, team):
            continue
        target_secret_name = target_name(source_name, source_env, target_env, team, preserve_names=preserve_names)
        try:
            source_value = source_secrets_client.get_secret_value(SecretId=source_name)
            kwargs = {
                "Name": target_secret_name,
                "Description": secret.get("Description", ""),
                "Tags": secret.get("Tags", []),
            }
            if "SecretString" in source_value:
                kwargs["SecretString"] = rewrite_string_value(
                    source_value["SecretString"],
                    resource_mappings,
                    sanitize_name(source_env),
                    sanitize_name(target_env),
                    sanitize_name(team),
                )
            elif "SecretBinary" in source_value:
                kwargs["SecretBinary"] = source_value["SecretBinary"]
            if secret.get("KmsKeyId"):
                kwargs["KmsKeyId"] = rewrite_kms_reference(secret["KmsKeyId"], resource_mappings)
            response = target_secrets_client.create_secret(**kwargs)
            target_secret_arn = response["ARN"]
            operation = "created"
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceExistsException":
                failed.append({"source_secret": source_name, "target_secret": target_secret_name, "error": str(exc)})
                continue
            update_kwargs = {"SecretId": target_secret_name}
            source_value = source_secrets_client.get_secret_value(SecretId=source_name)
            if "SecretString" in source_value:
                update_kwargs["SecretString"] = rewrite_string_value(
                    source_value["SecretString"],
                    resource_mappings,
                    sanitize_name(source_env),
                    sanitize_name(target_env),
                    sanitize_name(team),
                )
            elif "SecretBinary" in source_value:
                update_kwargs["SecretBinary"] = source_value["SecretBinary"]
            target_secrets_client.put_secret_value(**update_kwargs)
            target_secret_arn = target_secrets_client.describe_secret(SecretId=target_secret_name)["ARN"]
            operation = "updated"
        mappings["secret_arns"][secret["ARN"]] = target_secret_arn
        mappings["secret_names"][source_name] = target_secret_name
        deployed.append({
            "source_secret": source_name,
            "target_secret": target_secret_name,
            "target_secret_arn": target_secret_arn,
            "operation": operation,
        })
    return mappings, deployed, failed


def create_or_update_dynamodb_tables(snapshot, source_env, target_env, team, dynamodb_client, preserve_names=False, config=None):
    mappings = {"dynamodb_table_names": {}, "dynamodb_table_arns": {}, "dynamodb_stream_arns": {}}
    deployed = []
    failed = []
    for table_entry in snapshot.get("dynamodb_tables", []):
        table = table_entry["Table"]
        source_name = table["TableName"]
        if should_exclude("dynamodb_tables", source_name, config or {}):
            continue
        if should_skip_recloning(source_name, target_env, team):
            continue
        target_table_name = target_name(source_name, source_env, target_env, team, preserve_names=preserve_names)
        billing_mode = table.get("BillingModeSummary", {}).get("BillingMode", "PAY_PER_REQUEST")
        create_kwargs = {
            "TableName": target_table_name,
            "AttributeDefinitions": table.get("AttributeDefinitions", []),
            "KeySchema": table.get("KeySchema", []),
            "BillingMode": billing_mode,
        }
        if billing_mode == "PROVISIONED":
            create_kwargs["ProvisionedThroughput"] = {
                "ReadCapacityUnits": table["ProvisionedThroughput"]["ReadCapacityUnits"],
                "WriteCapacityUnits": table["ProvisionedThroughput"]["WriteCapacityUnits"],
            }
        if table.get("GlobalSecondaryIndexes"):
            gsis = []
            for gsi in table["GlobalSecondaryIndexes"]:
                gsi_payload = {
                    "IndexName": gsi["IndexName"],
                    "KeySchema": gsi["KeySchema"],
                    "Projection": gsi["Projection"],
                }
                if billing_mode == "PROVISIONED":
                    gsi_payload["ProvisionedThroughput"] = {
                        "ReadCapacityUnits": gsi["ProvisionedThroughput"]["ReadCapacityUnits"],
                        "WriteCapacityUnits": gsi["ProvisionedThroughput"]["WriteCapacityUnits"],
                    }
                gsis.append(gsi_payload)
            create_kwargs["GlobalSecondaryIndexes"] = gsis
        if table.get("StreamSpecification"):
            create_kwargs["StreamSpecification"] = table["StreamSpecification"]
        try:
            dynamodb_client.create_table(**create_kwargs)
            dynamodb_client.get_waiter("table_exists").wait(TableName=target_table_name)
            operation = "created"
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceInUseException":
                failed.append({"source_table": source_name, "target_table": target_table_name, "error": str(exc)})
                continue
            operation = "existing"
        description = dynamodb_client.describe_table(TableName=target_table_name)["Table"]
        ttl = table_entry.get("TimeToLiveDescription", {})
        if ttl.get("AttributeName") and ttl.get("TimeToLiveStatus") in {"ENABLED", "ENABLING"}:
            try:
                dynamodb_client.update_time_to_live(
                    TableName=target_table_name,
                    TimeToLiveSpecification={"Enabled": True, "AttributeName": ttl["AttributeName"]},
                )
            except ClientError:
                pass
        pitr = table_entry.get("ContinuousBackupsDescription", {}).get("PointInTimeRecoveryDescription", {})
        if pitr.get("PointInTimeRecoveryStatus") == "ENABLED":
            try:
                dynamodb_client.update_continuous_backups(
                    TableName=target_table_name,
                    PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
                )
            except ClientError:
                pass
        if table_entry.get("Tags"):
            try:
                dynamodb_client.tag_resource(ResourceArn=description["TableArn"], Tags=table_entry["Tags"])
            except ClientError:
                pass
        mappings["dynamodb_table_names"][source_name] = target_table_name
        mappings["dynamodb_table_arns"][table["TableArn"]] = description["TableArn"]
        if table.get("LatestStreamArn") and description.get("LatestStreamArn"):
            mappings["dynamodb_stream_arns"][table["LatestStreamArn"]] = description["LatestStreamArn"]
        deployed.append({
            "source_table": source_name,
            "target_table": target_table_name,
            "target_table_arn": description["TableArn"],
            "target_stream_arn": description.get("LatestStreamArn"),
            "operation": operation,
        })
    return mappings, deployed, failed


def copy_dynamodb_table_items(snapshot, source_dynamodb_resource, target_dynamodb_resource, resource_mappings, config=None):
    copied = []
    failed = []
    for table_entry in snapshot.get("dynamodb_tables", []):
        source_table_name = table_entry["Table"]["TableName"]
        if should_exclude("dynamodb_tables", source_table_name, config or {}):
            continue
        target_table_name = resource_mappings.get("dynamodb_table_names", {}).get(source_table_name)
        if not target_table_name:
            continue

        source_table = source_dynamodb_resource.Table(source_table_name)
        target_table = target_dynamodb_resource.Table(target_table_name)
        copied_count = 0

        try:
            scan_kwargs = {}
            with target_table.batch_writer() as batch:
                while True:
                    response = source_table.scan(**scan_kwargs)
                    for item in response.get("Items", []):
                        batch.put_item(Item=item)
                        copied_count += 1
                    if "LastEvaluatedKey" not in response:
                        break
                    scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

            copied.append({
                "source_table": source_table_name,
                "target_table": target_table_name,
                "copied_item_count": copied_count,
            })
        except Exception as exc:
            failed.append({
                "source_table": source_table_name,
                "target_table": target_table_name,
                "error": str(exc),
            })

    return copied, failed


def resolve_execution_role(snapshot, resource_mappings, source_role_arn):
    if source_role_arn in resource_mappings["role_arns"]:
        return resource_mappings["role_arns"][source_role_arn], resource_mappings["role_arns"][source_role_arn] != source_role_arn
    for role in snapshot.get("iam_roles", []):
        if role["Arn"] == source_role_arn and role_allows_lambda_assume(role.get("AssumeRolePolicyDocument", {})):
            return source_role_arn, False
    fallback_arn = next(iter(resource_mappings["role_arns"].values()), source_role_arn)
    return fallback_arn, fallback_arn != source_role_arn


def deploy_lambda_functions(snapshot, source_env, target_env, team, source_lambda_client, target_lambda_client, resource_mappings, preserve_names=False, config=None):
    deployed = []
    failed = []
    for source_fn in snapshot.get("lambda_functions", []):
        source_name = source_fn["FunctionName"]
        if should_exclude("lambda_functions", source_name, config or {}):
            continue
        if should_skip_recloning(source_name, target_env, team):
            continue
        target_fn = target_name(source_name, source_env, target_env, team, preserve_names=preserve_names)
        try:
            function_details = source_lambda_client.get_function(FunctionName=source_name)
            configuration = function_details["Configuration"]
            zip_path = download_lambda_zip(function_details["Code"]["Location"])
            ensure_zip_is_readable(zip_path)
            zip_bytes = zip_path.read_bytes()
            environment = update_env_values(
                configuration.get("Environment", {}).get("Variables", {}),
                resource_mappings,
                sanitize_name(source_env),
                sanitize_name(target_env),
                sanitize_name(team),
            )
            role_arn, fallback_used = resolve_execution_role(snapshot, resource_mappings, configuration.get("Role"))
            create_payload = trim_create_payload({
                "FunctionName": target_fn,
                "Runtime": configuration.get("Runtime"),
                "Role": role_arn,
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
            })
            vpc_config = configuration.get("VpcConfig") or {}
            remapped_vpc_config = remap_vpc_config(vpc_config, resource_mappings)
            if remapped_vpc_config.get("SubnetIds") and remapped_vpc_config.get("SecurityGroupIds"):
                create_payload["VpcConfig"] = remapped_vpc_config
            try:
                target_lambda_client.create_function(**create_payload)
                operation = "created"
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceConflictException":
                    raise
                target_lambda_client.update_function_code(FunctionName=target_fn, ZipFile=zip_bytes, Publish=False)
                target_lambda_client.get_waiter("function_updated").wait(FunctionName=target_fn)
                update_kwargs = {
                    "FunctionName": target_fn,
                    "Role": role_arn,
                    "Handler": configuration.get("Handler"),
                    "Description": f"Cloned from {source_name}",
                    "Timeout": configuration.get("Timeout", 3),
                    "MemorySize": configuration.get("MemorySize", 128),
                    "Environment": {"Variables": environment},
                }
                if "VpcConfig" in create_payload:
                    update_kwargs["VpcConfig"] = create_payload["VpcConfig"]
                target_lambda_client.update_function_configuration(**update_kwargs)
                operation = "updated"
            target_lambda_client.get_waiter("function_active_v2").wait(FunctionName=target_fn)
            target_lambda_client.get_waiter("function_updated").wait(FunctionName=target_fn)
            target_config = target_lambda_client.get_function(FunctionName=target_fn)["Configuration"]
            resource_mappings["function_arns"][source_fn["FunctionArn"]] = target_config["FunctionArn"]
            resource_mappings["function_names"][source_name] = target_fn
            deployed.append({
                "source_function": source_name,
                "target_function": target_fn,
                "target_function_arn": target_config["FunctionArn"],
                "operation": operation,
                "execution_role": role_arn,
                "fallback_role_used": fallback_used,
            })
        except Exception as exc:
            failed.append({"source_function": source_name, "target_function": target_fn, "error": str(exc)})
    return deployed, failed


def ensure_sqs_mapping_permissions(iam_client, role_arn):
    role_name = role_name_from_arn(role_arn)
    policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaSQSQueueExecutionRole"
    attached = iam_client.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", [])
    if policy_arn not in {item["PolicyArn"] for item in attached}:
        iam_client.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)


def ensure_dynamodb_stream_mapping_permissions(iam_client, role_arn, stream_arn):
    role_name = role_name_from_arn(role_arn)
    iam_client.put_role_policy(
        RoleName=role_name,
        PolicyName="aws-dev-agent-dynamodb-stream-access",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "dynamodb:GetRecords",
                            "dynamodb:GetShardIterator",
                            "dynamodb:DescribeStream",
                            "dynamodb:ListStreams",
                        ],
                        "Resource": [stream_arn],
                    }
                ],
            }
        ),
    )


def queue_url_for_target_arn(resource_mappings, target_source_arn):
    for source_arn, mapped_arn in resource_mappings.get("queue_arns", {}).items():
        if mapped_arn == target_source_arn:
            return resource_mappings.get("queue_urls", {}).get(source_arn)
    if target_source_arn and ":sqs:" in target_source_arn:
        parts = target_source_arn.split(":")
        if len(parts) >= 6:
            region = parts[3]
            account_id = parts[4]
            queue_name = parts[5]
            return f"https://sqs.{region}.amazonaws.com/{account_id}/{queue_name}"
    return None


def ensure_queue_visibility_timeout(lambda_client, sqs_client, resource_mappings, target_function_name, target_source_arn):
    queue_url = queue_url_for_target_arn(resource_mappings, target_source_arn)
    if not queue_url:
        return None
    function_timeout = lambda_client.get_function_configuration(FunctionName=target_function_name).get("Timeout", 30)
    required_timeout = str(max(function_timeout, 30) + 30)
    try:
        current_timeout = sqs_client.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=["VisibilityTimeout"],
        )["Attributes"].get("VisibilityTimeout", "30")
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"AWS.SimpleQueueService.NonExistentQueue", "QueueDoesNotExist"}:
            return None
        raise
    if int(current_timeout) < int(required_timeout):
        sqs_client.set_queue_attributes(
            QueueUrl=queue_url,
            Attributes={"VisibilityTimeout": required_timeout},
        )
    return {
        "target_function": target_function_name,
        "queue_url": queue_url,
        "visibility_timeout": required_timeout,
    }


def normalize_queue_visibility_for_mappings(snapshot, lambda_client, sqs_client, resource_mappings):
    adjusted = []
    for mapping in snapshot.get("lambda_event_source_mappings", []):
        source_function_name = function_name_from_arn(mapping["FunctionArn"])
        target_function_name = resource_mappings.get("function_names", {}).get(source_function_name)
        target_source_arn = resource_mappings.get("queue_arns", {}).get(mapping.get("EventSourceArn"), mapping.get("EventSourceArn"))
        if not target_function_name or ":sqs:" not in str(target_source_arn):
            continue
        adjustment = ensure_queue_visibility_timeout(
            lambda_client,
            sqs_client,
            resource_mappings,
            target_function_name,
            target_source_arn,
        )
        if adjustment:
            adjusted.append(adjustment)
    return adjusted


def create_event_source_mappings(snapshot, lambda_client, iam_client, sqs_client, resource_mappings, target_region, config=None):
    deployed = []
    failed = []
    for mapping in snapshot.get("lambda_event_source_mappings", []):
        if should_exclude("lambda_event_source_mappings", mapping.get("UUID", ""), config or {}):
            continue
        source_function_name = function_name_from_arn(mapping["FunctionArn"])
        if should_skip_recloning(source_function_name, resource_mappings.get("target_env", ""), resource_mappings.get("team", "")):
            continue
        target_function_name = resource_mappings["function_names"].get(source_function_name)
        original_source_arn = mapping.get("EventSourceArn")
        target_source_arn = resource_mappings["queue_arns"].get(original_source_arn, original_source_arn)
        target_source_arn = resource_mappings["dynamodb_stream_arns"].get(target_source_arn, target_source_arn)
        if not target_function_name or not target_source_arn:
            failed.append({"source_mapping": mapping.get("UUID"), "error": "Missing target function or event source"})
            continue
        if ":dynamodb:" in target_source_arn and f":{target_region}:" not in target_source_arn:
            deployed.append({
                "source_uuid": mapping.get("UUID"),
                "target_function": target_function_name,
                "target_event_source_arn": target_source_arn,
                "operation": "skipped-unsupported-cross-region-source",
            })
            continue
        if ":sqs:" not in target_source_arn and ":dynamodb:" not in target_source_arn and f":{target_region}:" not in target_source_arn:
            deployed.append({
                "source_uuid": mapping.get("UUID"),
                "target_function": target_function_name,
                "target_event_source_arn": target_source_arn,
                "operation": "skipped-unsupported-cross-region-source",
            })
            continue
        if f":{target_region}:" not in target_source_arn and (":sqs:" in target_source_arn or ":dynamodb:" in target_source_arn):
            failed.append({"source_uuid": mapping.get("UUID"), "target_function": target_function_name, "error": "Target event source was not recreated in target region"})
            continue
        params = {
            "FunctionName": target_function_name,
            "EventSourceArn": target_source_arn,
            "Enabled": mapping.get("State") != "Disabled",
            "BatchSize": mapping.get("BatchSize", 10),
        }
        if mapping.get("MaximumBatchingWindowInSeconds") is not None:
            params["MaximumBatchingWindowInSeconds"] = mapping["MaximumBatchingWindowInSeconds"]
        if mapping.get("StartingPosition"):
            params["StartingPosition"] = mapping["StartingPosition"]
        if mapping.get("StartingPositionTimestamp"):
            params["StartingPositionTimestamp"] = mapping["StartingPositionTimestamp"]
        try:
            target_role_arn = lambda_client.get_function_configuration(FunctionName=target_function_name).get("Role")
            if ":sqs:" in target_source_arn:
                if target_role_arn:
                    ensure_sqs_mapping_permissions(iam_client, target_role_arn)
                ensure_queue_visibility_timeout(
                    lambda_client,
                    sqs_client,
                    resource_mappings,
                    target_function_name,
                    target_source_arn,
                )
                # Give SQS attribute updates a brief moment to settle before mapping creation.
                time.sleep(5)
            elif ":dynamodb:" in target_source_arn and target_role_arn:
                ensure_dynamodb_stream_mapping_permissions(iam_client, target_role_arn, target_source_arn)
                time.sleep(5)
            response = lambda_client.create_event_source_mapping(**params)
            deployed.append({"source_uuid": mapping.get("UUID"), "target_uuid": response.get("UUID"), "target_function": target_function_name, "target_event_source_arn": target_source_arn, "operation": "created"})
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "InvalidParameterValueException" and "ReceiveMessage" in str(exc) and ":sqs:" in target_source_arn:
                time.sleep(10)
                try:
                    response = lambda_client.create_event_source_mapping(**params)
                    deployed.append({"source_uuid": mapping.get("UUID"), "target_uuid": response.get("UUID"), "target_function": target_function_name, "target_event_source_arn": target_source_arn, "operation": "created-after-retry"})
                    continue
                except ClientError as retry_exc:
                    exc = retry_exc
            if exc.response["Error"]["Code"] == "InvalidParameterValueException" and "Queue visibility timeout" in str(exc) and ":sqs:" in target_source_arn:
                adjustment = ensure_queue_visibility_timeout(
                    lambda_client,
                    sqs_client,
                    resource_mappings,
                    target_function_name,
                    target_source_arn,
                )
                if adjustment:
                    time.sleep(5)
                    try:
                        response = lambda_client.create_event_source_mapping(**params)
                        deployed.append({"source_uuid": mapping.get("UUID"), "target_uuid": response.get("UUID"), "target_function": target_function_name, "target_event_source_arn": target_source_arn, "operation": "created-after-visibility-update"})
                        continue
                    except ClientError as retry_exc:
                        exc = retry_exc
            if exc.response["Error"]["Code"] == "InvalidParameterValueException" and "GetRecords" in str(exc) and ":dynamodb:" in target_source_arn and target_role_arn:
                ensure_dynamodb_stream_mapping_permissions(iam_client, target_role_arn, target_source_arn)
                time.sleep(10)
                try:
                    response = lambda_client.create_event_source_mapping(**params)
                    deployed.append({"source_uuid": mapping.get("UUID"), "target_uuid": response.get("UUID"), "target_function": target_function_name, "target_event_source_arn": target_source_arn, "operation": "created-after-stream-policy-update"})
                    continue
                except ClientError as retry_exc:
                    exc = retry_exc
            if exc.response["Error"]["Code"] == "ResourceConflictException":
                deployed.append({"source_uuid": mapping.get("UUID"), "target_function": target_function_name, "target_event_source_arn": target_source_arn, "operation": "existing"})
            else:
                failed.append({"source_uuid": mapping.get("UUID"), "target_function": target_function_name, "error": str(exc)})
    return deployed, failed


def apply_lambda_permissions(snapshot, lambda_client, resource_mappings, source_env, target_env, team, config=None):
    deployed = []
    failed = []
    for permission in snapshot.get("lambda_permissions", []):
        if should_skip_recloning(permission["FunctionName"], target_env, team):
            continue
        if should_exclude("lambda_permissions", permission["FunctionName"], config or {}):
            continue
        target_function_name = resource_mappings["function_names"].get(permission["FunctionName"])
        if not target_function_name:
            continue
        for index, statement in enumerate(permission.get("Policy", {}).get("Statement", []), start=1):
            principal = statement.get("Principal", {})
            if not principal:
                continue
            params = {
                "FunctionName": target_function_name,
                "StatementId": f"cloned-{index}-{sanitize_name(target_function_name)[:40]}",
                "Action": statement.get("Action", "lambda:InvokeFunction"),
                "Principal": principal.get("Service") or principal.get("AWS"),
            }
            source_arn = statement.get("Condition", {}).get("ArnLike", {}).get("AWS:SourceArn")
            if source_arn:
                params["SourceArn"] = rewrite_string_value(source_arn, resource_mappings, sanitize_name(source_env), sanitize_name(target_env), sanitize_name(team))
            try:
                lambda_client.add_permission(**params)
                deployed.append({"target_function": target_function_name, "statement_id": params["StatementId"], "principal": params["Principal"]})
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceConflictException":
                    failed.append({"target_function": target_function_name, "statement_id": params["StatementId"], "error": str(exc)})
    return deployed, failed


def create_sns_subscriptions(snapshot, sns_client, resource_mappings, config=None):
    deployed = []
    failed = []
    for topic in snapshot.get("sns_topics", []):
        if should_skip_recloning(topic["TopicName"], resource_mappings.get("target_env", ""), resource_mappings.get("team", "")):
            continue
        if should_exclude("sns_topics", topic["TopicName"], config or {}):
            continue
        target_topic_arn = resource_mappings["topic_arns"].get(topic["TopicArn"])
        if not target_topic_arn:
            continue
        for subscription in topic.get("Subscriptions", []):
            protocol = subscription.get("Protocol")
            endpoint = subscription.get("Endpoint")
            if protocol == "lambda":
                endpoint = resource_mappings["function_arns"].get(endpoint, endpoint)
            elif protocol == "sqs":
                endpoint = resource_mappings["queue_arns"].get(endpoint, endpoint)
            try:
                response = sns_client.subscribe(TopicArn=target_topic_arn, Protocol=protocol, Endpoint=endpoint)
                deployed.append({"target_topic_arn": target_topic_arn, "protocol": protocol, "endpoint": endpoint, "subscription_arn": response.get("SubscriptionArn")})
            except Exception as exc:
                failed.append({"target_topic_arn": target_topic_arn, "protocol": protocol, "endpoint": endpoint, "error": str(exc)})
    return deployed, failed


def create_api_gateways(snapshot, apigw_client, resource_mappings, source_env, target_env, team, preserve_names=False, config=None):
    deployed = []
    failed = []
    api_ids = {}
    for api in snapshot.get("api_gateways", []):
        source_api_name = api["name"]
        if should_exclude("api_gateways", source_api_name, config or {}):
            continue
        if should_skip_recloning(source_api_name, target_env, team):
            continue
        target_api_name = target_name(source_api_name, source_env, target_env, team, preserve_names=preserve_names)
        try:
            export_body = api.get("export_body", "")
            if export_body:
                imported = apigw_client.import_rest_api(
                    failOnWarnings=False,
                    body=export_body.encode("utf-8"),
                    parameters={"endpointConfigurationTypes": "REGIONAL"},
                )
                target_api_id = imported["id"]
                apigw_client.update_rest_api(
                    restApiId=target_api_id,
                    patchOperations=[{"op": "replace", "path": "/name", "value": target_api_name}],
                )
                operation = "imported"
            else:
                created = apigw_client.create_rest_api(name=target_api_name, description=f"Cloned from {source_api_name}")
                target_api_id = created["id"]
                operation = "created"
            api_ids[api["id"]] = target_api_id
            deploy_api_gateway_extras(
                apigw_client,
                api,
                target_api_id,
                resource_mappings,
                source_env,
                target_env,
                team,
                preserve_names=preserve_names,
            )
            deployed.append({"source_api": source_api_name, "target_api": target_api_name, "target_api_id": target_api_id, "operation": operation})
        except Exception as exc:
            failed.append({"source_api": source_api_name, "target_api": target_api_name, "error": str(exc)})
    return api_ids, deployed, failed


def deploy_api_gateway_extras(apigw_client, api, target_api_id, resource_mappings, source_env, target_env, team, preserve_names=False):
    for validator in api.get("request_validators", []):
        validator_name = validator.get("name")
        if not validator_name:
            continue
        payload = {
            "restApiId": target_api_id,
            "name": target_name(validator_name, source_env, target_env, team, preserve_names=preserve_names),
            "validateRequestBody": validator.get("validateRequestBody", False),
            "validateRequestParameters": validator.get("validateRequestParameters", False),
        }
        try:
            apigw_client.create_request_validator(**payload)
        except Exception:
            continue

    for authorizer in api.get("authorizers", []):
        authorizer_name = authorizer.get("name")
        if not authorizer_name:
            continue
        payload = {
            "restApiId": target_api_id,
            "name": target_name(authorizer_name, source_env, target_env, team, preserve_names=preserve_names),
            "type": authorizer.get("type"),
            "providerARNs": rewrite_structure(
                authorizer.get("providerARNs", []),
                resource_mappings,
                sanitize_name(source_env),
                sanitize_name(target_env),
                sanitize_name(team),
            ),
            "authType": authorizer.get("authType"),
            "authorizerUri": rewrite_string_value(
                authorizer.get("authorizerUri"),
                resource_mappings,
                sanitize_name(source_env),
                sanitize_name(target_env),
                sanitize_name(team),
            ) if authorizer.get("authorizerUri") else None,
            "authorizerCredentials": rewrite_string_value(
                authorizer.get("authorizerCredentials"),
                resource_mappings,
                sanitize_name(source_env),
                sanitize_name(target_env),
                sanitize_name(team),
            ) if authorizer.get("authorizerCredentials") else None,
            "identitySource": authorizer.get("identitySource"),
            "identityValidationExpression": authorizer.get("identityValidationExpression"),
            "authorizerResultTtlInSeconds": authorizer.get("authorizerResultTtlInSeconds"),
        }
        payload = {key: value for key, value in payload.items() if value not in (None, [], {}, "")}
        try:
            apigw_client.create_authorizer(**payload)
        except Exception:
            continue

    for stage in api.get("stages", []):
        stage_name = stage.get("stageName")
        if not stage_name:
            continue
        variables = {
            key: rewrite_string_value(value, resource_mappings, sanitize_name(source_env), sanitize_name(target_env), sanitize_name(team))
            for key, value in (stage.get("variables") or {}).items()
        }
        try:
            apigw_client.create_deployment(
                restApiId=target_api_id,
                stageName=stage_name,
                description=stage.get("description", ""),
                variables=variables or None,
            )
        except Exception:
            continue
        method_settings = stage.get("methodSettings") or {}
        patch_operations = []
        field_mappings = {
            "metricsEnabled": "metrics/enabled",
            "loggingLevel": "logging/loglevel",
            "dataTraceEnabled": "logging/dataTrace",
            "throttlingBurstLimit": "throttling/burstLimit",
            "throttlingRateLimit": "throttling/rateLimit",
            "cachingEnabled": "caching/enabled",
            "cacheTtlInSeconds": "caching/ttlInSeconds",
            "cacheDataEncrypted": "caching/dataEncrypted",
            "requireAuthorizationForCacheControl": "caching/requireAuthorizationForCacheControl",
            "unauthorizedCacheControlHeaderStrategy": "caching/unauthorizedCacheControlHeaderStrategy",
        }
        for method_path, settings in method_settings.items():
            escaped_method_path = method_path.replace("/", "~1")
            for source_key, target_key in field_mappings.items():
                if source_key not in settings:
                    continue
                patch_operations.append({
                    "op": "replace",
                    "path": f"/{escaped_method_path}/{target_key}",
                    "value": str(settings[source_key]).lower() if isinstance(settings[source_key], bool) else str(settings[source_key]),
                })
        if patch_operations:
            try:
                apigw_client.update_stage(
                    restApiId=target_api_id,
                    stageName=stage_name,
                    patchOperations=patch_operations,
                )
            except Exception:
                pass

    for usage_plan in api.get("usage_plans", []):
        target_usage_plan_name = target_name(usage_plan.get("name", "usage-plan"), source_env, target_env, team, preserve_names=preserve_names)
        api_stages = []
        for item in usage_plan.get("apiStages", []):
            if item.get("apiId") == api.get("id"):
                api_stages.append({"apiId": target_api_id, "stage": item.get("stage")})
        payload = {
            "name": target_usage_plan_name,
            "description": usage_plan.get("description", ""),
            "apiStages": api_stages,
            "throttle": usage_plan.get("throttle"),
            "quota": usage_plan.get("quota"),
            "tags": usage_plan.get("tags", {}),
        }
        payload = {key: value for key, value in payload.items() if value not in (None, [], {}, "")}
        try:
            created_usage_plan = apigw_client.create_usage_plan(**payload)
        except Exception:
            continue
        usage_plan_id = created_usage_plan.get("id")
        for api_key in usage_plan.get("apiKeys", []):
            key_name = api_key.get("name")
            if not key_name or not usage_plan_id:
                continue
            try:
                created_key = apigw_client.create_api_key(
                    name=target_name(key_name, source_env, target_env, team, preserve_names=preserve_names),
                    description=api_key.get("description", ""),
                    enabled=api_key.get("enabled", True),
                    value=api_key.get("value"),
                    generateDistinctId=False,
                )
                apigw_client.create_usage_plan_key(
                    usagePlanId=usage_plan_id,
                    keyId=created_key["id"],
                    keyType="API_KEY",
                )
            except Exception:
                continue

    for response in api.get("gateway_responses", []):
        response_type = response.get("responseType")
        if not response_type:
            continue
        payload = {
            "restApiId": target_api_id,
            "responseType": response_type,
        }
        if response.get("statusCode"):
            payload["statusCode"] = str(response.get("statusCode"))
        if response.get("responseParameters"):
            payload["responseParameters"] = rewrite_structure(
                response.get("responseParameters", {}),
                resource_mappings,
                sanitize_name(source_env),
                sanitize_name(target_env),
                sanitize_name(team),
            )
        if response.get("responseTemplates"):
            payload["responseTemplates"] = rewrite_structure(
                response.get("responseTemplates", {}),
                resource_mappings,
                sanitize_name(source_env),
                sanitize_name(target_env),
                sanitize_name(team),
            )
        try:
            apigw_client.put_gateway_response(**payload)
        except Exception:
            continue

    for domain in api.get("domain_mappings", []):
        certificate_arn = domain.get("regionalCertificateArn") or domain.get("certificateArn")
        endpoint_types = domain.get("endpointConfiguration", {}).get("types", ["REGIONAL"])
        try:
            kwargs = {
                "domainName": domain.get("domainName"),
                "endpointConfiguration": {"types": endpoint_types},
            }
            if certificate_arn:
                if "REGIONAL" in endpoint_types:
                    kwargs["regionalCertificateArn"] = certificate_arn
                else:
                    kwargs["certificateArn"] = certificate_arn
            if domain.get("securityPolicy"):
                kwargs["securityPolicy"] = domain["securityPolicy"]
            apigw_client.create_domain_name(**kwargs)
        except Exception:
            pass
        for mapping in domain.get("mappings", []):
            try:
                kwargs = {
                    "domainName": domain.get("domainName"),
                    "restApiId": target_api_id,
                    "stage": mapping.get("stage"),
                }
                if mapping.get("basePath") not in (None, "", "(none)"):
                    kwargs["basePath"] = mapping.get("basePath")
                apigw_client.create_base_path_mapping(**kwargs)
            except Exception:
                continue


def rewrite_security_group_permissions(permissions, mappings):
    rewritten_permissions = []
    for permission in permissions or []:
        updated = dict(permission)
        updated["UserIdGroupPairs"] = []
        for pair in permission.get("UserIdGroupPairs", []):
            source_group_id = pair.get("GroupId")
            target_group_id = mappings.get("security_group_ids", {}).get(source_group_id)
            if not target_group_id:
                continue
            updated_pair = dict(pair)
            updated_pair["GroupId"] = target_group_id
            updated["UserIdGroupPairs"].append(updated_pair)
        rewritten_permissions.append(updated)
    return rewritten_permissions


def create_or_update_network(snapshot, ec2_client, source_env, target_env, team, resource_mappings, preserve_names=False, config=None):
    mappings = {"vpc_ids": {}, "subnet_ids": {}, "route_table_ids": {}, "security_group_ids": {}}
    deployed = {"vpcs": [], "subnets": [], "route_tables": [], "security_groups": []}
    failed = {"vpcs": [], "subnets": [], "route_tables": [], "security_groups": []}

    for vpc in snapshot.get("vpcs", []):
        source_vpc_id = vpc.get("VpcId")
        source_name = tag_value(vpc.get("Tags"), "Name") or source_vpc_id
        if should_exclude("vpcs", source_name, config or {}):
            continue
        target_vpc_name = network_target_name(source_name, source_env, target_env, team, preserve_names=preserve_names)
        try:
            response = ec2_client.create_vpc(CidrBlock=vpc.get("CidrBlock"))
            target_vpc_id = response["Vpc"]["VpcId"]
            ec2_client.create_tags(Resources=[target_vpc_id], Tags=build_tag_list(vpc.get("Tags"), target_vpc_name))
            mappings["vpc_ids"][source_vpc_id] = target_vpc_id
            deployed["vpcs"].append({"source_vpc": source_vpc_id, "target_vpc": target_vpc_id, "target_name": target_vpc_name, "operation": "created"})
        except Exception as exc:
            existing_vpc = find_vpc_by_name_or_cidr(ec2_client, target_vpc_name, vpc.get("CidrBlock"))
            if existing_vpc:
                target_vpc_id = existing_vpc["VpcId"]
                mappings["vpc_ids"][source_vpc_id] = target_vpc_id
                deployed["vpcs"].append({"source_vpc": source_vpc_id, "target_vpc": target_vpc_id, "target_name": target_vpc_name, "operation": "adopted-existing"})
                continue
            failed["vpcs"].append({"source_vpc": source_vpc_id, "target_name": target_vpc_name, "error": str(exc)})

    for subnet in snapshot.get("subnets", []):
        source_subnet_id = subnet.get("SubnetId")
        target_vpc_id = mappings["vpc_ids"].get(subnet.get("VpcId"))
        if not target_vpc_id:
            failed["subnets"].append({"source_subnet": source_subnet_id, "error": "missing-target-vpc"})
            continue
        source_name = tag_value(subnet.get("Tags"), "Name") or source_subnet_id
        target_subnet_name = network_target_name(source_name, source_env, target_env, team, preserve_names=preserve_names)
        try:
            response = ec2_client.create_subnet(
                VpcId=target_vpc_id,
                CidrBlock=subnet.get("CidrBlock"),
            )
            target_subnet_id = response["Subnet"]["SubnetId"]
            ec2_client.create_tags(Resources=[target_subnet_id], Tags=build_tag_list(subnet.get("Tags"), target_subnet_name))
            mappings["subnet_ids"][source_subnet_id] = target_subnet_id
            deployed["subnets"].append({"source_subnet": source_subnet_id, "target_subnet": target_subnet_id, "target_name": target_subnet_name, "operation": "created"})
        except Exception as exc:
            existing_subnet = find_subnet_by_name_or_cidr(ec2_client, target_vpc_id, target_subnet_name, subnet.get("CidrBlock"))
            if existing_subnet:
                target_subnet_id = existing_subnet["SubnetId"]
                mappings["subnet_ids"][source_subnet_id] = target_subnet_id
                deployed["subnets"].append({"source_subnet": source_subnet_id, "target_subnet": target_subnet_id, "target_name": target_subnet_name, "operation": "adopted-existing"})
                continue
            failed["subnets"].append({"source_subnet": source_subnet_id, "target_name": target_subnet_name, "error": str(exc)})

    for route_table in snapshot.get("route_tables", []):
        source_route_table_id = route_table.get("RouteTableId")
        target_vpc_id = mappings["vpc_ids"].get(route_table.get("VpcId"))
        if not target_vpc_id:
            failed["route_tables"].append({"source_route_table": source_route_table_id, "error": "missing-target-vpc"})
            continue
        source_name = tag_value(route_table.get("Tags"), "Name") or source_route_table_id
        target_route_table_name = network_target_name(source_name, source_env, target_env, team, preserve_names=preserve_names)
        try:
            response = ec2_client.create_route_table(VpcId=target_vpc_id)
            target_route_table_id = response["RouteTable"]["RouteTableId"]
            ec2_client.create_tags(Resources=[target_route_table_id], Tags=build_tag_list(route_table.get("Tags"), target_route_table_name))
            for association in route_table.get("Associations", []):
                subnet_id = mappings["subnet_ids"].get(association.get("SubnetId"))
                if subnet_id and not association.get("Main"):
                    try:
                        ec2_client.associate_route_table(RouteTableId=target_route_table_id, SubnetId=subnet_id)
                    except Exception:
                        pass
            mappings["route_table_ids"][source_route_table_id] = target_route_table_id
            deployed["route_tables"].append({"source_route_table": source_route_table_id, "target_route_table": target_route_table_id, "target_name": target_route_table_name, "operation": "created"})
        except Exception as exc:
            failed["route_tables"].append({"source_route_table": source_route_table_id, "target_name": target_route_table_name, "error": str(exc)})

    pending_permissions = []
    for sg in snapshot.get("security_groups", []):
        source_group_id = sg.get("GroupId")
        target_vpc_id = mappings["vpc_ids"].get(sg.get("VpcId"))
        if not target_vpc_id:
            if sg.get("GroupName") == "default":
                continue
            failed["security_groups"].append({"source_group": source_group_id, "error": "missing-target-vpc"})
            continue
        target_group_name = "default" if sg.get("GroupName") == "default" else network_target_name(sg.get("GroupName", source_group_id), source_env, target_env, team, preserve_names=preserve_names)
        if sg.get("GroupName") == "default":
            try:
                described = ec2_client.describe_security_groups(
                    Filters=[{"Name": "group-name", "Values": ["default"]}, {"Name": "vpc-id", "Values": [target_vpc_id]}]
                ).get("SecurityGroups", [])
                if described:
                    mappings["security_group_ids"][source_group_id] = described[0]["GroupId"]
                    deployed["security_groups"].append({"source_group": source_group_id, "target_group": described[0]["GroupId"], "target_name": "default", "operation": "mapped-default"})
                    pending_permissions.append((sg, described[0]["GroupId"]))
                    continue
            except Exception:
                pass
        try:
            response = ec2_client.create_security_group(
                GroupName=target_group_name,
                Description=sg.get("Description", target_group_name),
                VpcId=target_vpc_id,
                TagSpecifications=[{"ResourceType": "security-group", "Tags": build_tag_list(sg.get("Tags"), target_group_name)}],
            )
            target_group_id = response["GroupId"]
            mappings["security_group_ids"][source_group_id] = target_group_id
            deployed["security_groups"].append({"source_group": source_group_id, "target_group": target_group_id, "target_name": target_group_name, "operation": "created"})
            pending_permissions.append((sg, target_group_id))
        except Exception as exc:
            failed["security_groups"].append({"source_group": source_group_id, "target_name": target_group_name, "error": str(exc)})

    for sg, target_group_id in pending_permissions:
        try:
            ingress_permissions = rewrite_security_group_permissions(sg.get("IpPermissions", []), mappings)
            if ingress_permissions:
                ec2_client.authorize_security_group_ingress(GroupId=target_group_id, IpPermissions=ingress_permissions)
        except Exception:
            pass
        try:
            egress_permissions = rewrite_security_group_permissions(sg.get("IpPermissionsEgress", []), mappings)
            if egress_permissions:
                ec2_client.authorize_security_group_egress(GroupId=target_group_id, IpPermissions=egress_permissions)
        except Exception:
            pass

    return mappings, deployed, failed


def build_codebuild_project_payload(project, resource_mappings, source_env, target_env, team, preserve_names=False):
    source_name = project.get("name", "")
    target_project_name = target_name(source_name, source_env, target_env, team, preserve_names=preserve_names)
    environment = dict(project.get("environment", {}))
    env_vars = []
    for env_var in environment.get("environmentVariables", []):
        updated = dict(env_var)
        updated["value"] = rewrite_string_value(
            env_var.get("value"),
            resource_mappings,
            sanitize_name(source_env),
            sanitize_name(target_env),
            sanitize_name(team),
        )
        env_vars.append(updated)
    if env_vars:
        environment["environmentVariables"] = env_vars

    service_role = project.get("serviceRole", "")
    mapped_role = resource_mappings.get("role_arns", {}).get(service_role, service_role)

    vpc_config = project.get("vpcConfig")
    if vpc_config:
        vpc_config = dict(vpc_config)
        vpc_config["subnets"] = [resource_mappings.get("subnet_ids", {}).get(item, item) for item in vpc_config.get("subnets", [])]
        vpc_config["securityGroupIds"] = [resource_mappings.get("security_group_ids", {}).get(item, item) for item in vpc_config.get("securityGroupIds", [])]
        if not vpc_config.get("subnets") or not vpc_config.get("securityGroupIds"):
            vpc_config = None

    encryption_key = rewrite_kms_reference(project.get("encryptionKey"), resource_mappings)
    target_region = resource_mappings.get("target_region", "")
    if isinstance(encryption_key, str) and encryption_key.startswith("arn:aws:kms:") and target_region:
        parts = encryption_key.split(":")
        if len(parts) > 3 and parts[3] != target_region:
            encryption_key = None

    payload = {
        "name": target_project_name,
        "description": project.get("description", ""),
        "source": rewrite_structure(project.get("source", {}), resource_mappings, sanitize_name(source_env), sanitize_name(target_env), sanitize_name(team)),
        "artifacts": rewrite_structure(project.get("artifacts", {}), resource_mappings, sanitize_name(source_env), sanitize_name(target_env), sanitize_name(team)),
        "environment": environment,
        "serviceRole": mapped_role,
        "timeoutInMinutes": project.get("timeoutInMinutes"),
        "queuedTimeoutInMinutes": project.get("queuedTimeoutInMinutes"),
        "encryptionKey": encryption_key,
        "tags": project.get("tags", []),
        "vpcConfig": vpc_config,
        "badgeEnabled": project.get("badge", {}).get("badgeEnabled"),
        "logsConfig": rewrite_structure(project.get("logsConfig"), resource_mappings, sanitize_name(source_env), sanitize_name(target_env), sanitize_name(team)),
        "cache": rewrite_structure(project.get("cache"), resource_mappings, sanitize_name(source_env), sanitize_name(target_env), sanitize_name(team)),
        "fileSystemLocations": rewrite_structure(project.get("fileSystemLocations"), resource_mappings, sanitize_name(source_env), sanitize_name(target_env), sanitize_name(team)),
        "secondarySources": rewrite_structure(project.get("secondarySources"), resource_mappings, sanitize_name(source_env), sanitize_name(target_env), sanitize_name(team)),
        "secondaryArtifacts": rewrite_structure(project.get("secondaryArtifacts"), resource_mappings, sanitize_name(source_env), sanitize_name(target_env), sanitize_name(team)),
        "secondarySourceVersions": rewrite_structure(project.get("secondarySourceVersions"), resource_mappings, sanitize_name(source_env), sanitize_name(target_env), sanitize_name(team)),
        "buildBatchConfig": project.get("buildBatchConfig"),
        "sourceVersion": project.get("sourceVersion"),
    }
    payload = {key: value for key, value in payload.items() if value not in (None, [], {}, "")}
    return target_project_name, payload


def deploy_codebuild_projects(snapshot, codebuild_client, resource_mappings, source_env, target_env, team, preserve_names=False, config=None):
    deployed = []
    failed = []
    mappings = {"codebuild_project_arns": {}, "codebuild_project_names": {}}
    for project in snapshot.get("codebuild_projects", []):
        source_name = project.get("name", "")
        if not source_name:
            continue
        if should_exclude("codebuild_projects", source_name, config or {}):
            continue
        if should_skip_recloning(source_name, target_env, team):
            continue
        target_project_name, payload = build_codebuild_project_payload(
            project,
            resource_mappings,
            source_env,
            target_env,
            team,
            preserve_names=preserve_names,
        )
        try:
            response = codebuild_client.create_project(**payload)
            target_project = response.get("project", {})
            operation = "created"
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceAlreadyExistsException":
                failed.append({"source_project": source_name, "target_project": target_project_name, "error": str(exc)})
                continue
            try:
                response = codebuild_client.update_project(**payload)
                target_project = response.get("project", {})
                operation = "updated"
            except Exception as update_exc:
                failed.append({"source_project": source_name, "target_project": target_project_name, "error": str(update_exc)})
                continue
        source_arn = project.get("arn")
        target_arn = target_project.get("arn")
        if source_arn and target_arn:
            mappings["codebuild_project_arns"][source_arn] = target_arn
        mappings["codebuild_project_names"][source_name] = target_project_name
        deployed.append({
            "source_project": source_name,
            "target_project": target_project_name,
            "target_project_arn": target_arn,
            "operation": operation,
        })
    return mappings, deployed, failed


def deploy_ecs_clusters(snapshot, ecs_client, source_env, target_env, team, preserve_names=False, config=None):
    mappings = {"ecs_cluster_arns": {}, "ecs_cluster_names": {}}
    deployed = []
    failed = []
    for cluster in snapshot.get("ecs", {}).get("clusters", []):
        source_name = cluster.get("clusterName")
        if not source_name:
            continue
        if should_exclude("ecs_clusters", source_name, config or {}):
            continue
        target_name_value = target_name(source_name, source_env, target_env, team, preserve_names=preserve_names)
        settings = cluster.get("settings", [])
        configuration = cluster.get("configuration", {})
        try:
            response = ecs_client.create_cluster(
                clusterName=target_name_value,
                settings=settings,
                configuration=configuration,
                tags=cluster.get("tags", []),
            )
            target_cluster = response["cluster"]
            operation = "created"
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ClusterAlreadyExistsException":
                failed.append({"source_cluster": source_name, "target_cluster": target_name_value, "error": str(exc)})
                continue
            target_cluster = ecs_client.describe_clusters(clusters=[target_name_value]).get("clusters", [{}])[0]
            operation = "existing"
        mappings["ecs_cluster_arns"][cluster["clusterArn"]] = target_cluster.get("clusterArn")
        mappings["ecs_cluster_names"][source_name] = target_name_value
        deployed.append({
            "source_cluster": source_name,
            "target_cluster": target_name_value,
            "target_cluster_arn": target_cluster.get("clusterArn"),
            "operation": operation,
        })
    return mappings, deployed, failed


def deploy_ecs_task_definitions(snapshot, ecs_client, resource_mappings, source_env, target_env, team, preserve_names=False, config=None):
    mappings = {"ecs_task_definition_arns": {}, "ecs_task_definition_families": {}}
    deployed = []
    failed = []
    sanitized_source = sanitize_name(source_env)
    sanitized_target = sanitize_name(target_env)
    sanitized_team = sanitize_name(team) if team else ""
    for task_def in snapshot.get("ecs", {}).get("task_definitions", []):
        source_family = task_def.get("family")
        if not source_family:
            continue
        if should_exclude("ecs_task_definitions", source_family, config or {}):
            continue
        target_family = target_name(source_family, source_env, target_env, team, preserve_names=preserve_names)
        container_definitions = rewrite_structure(
            task_def.get("containerDefinitions", []),
            resource_mappings,
            sanitized_source,
            sanitized_target,
            sanitized_team,
        )
        target_region = resource_mappings.get("target_region", "")
        if target_region:
            for container in container_definitions:
                log_config = container.get("logConfiguration", {})
                if log_config.get("logDriver") != "awslogs":
                    continue
                options = log_config.get("options")
                if isinstance(options, dict):
                    options["awslogs-region"] = target_region
        payload = {
            "family": target_family,
            "taskRoleArn": resource_mappings.get("role_arns", {}).get(task_def.get("taskRoleArn"), task_def.get("taskRoleArn")),
            "executionRoleArn": resource_mappings.get("role_arns", {}).get(task_def.get("executionRoleArn"), task_def.get("executionRoleArn")),
            "networkMode": task_def.get("networkMode"),
            "containerDefinitions": container_definitions,
            "volumes": rewrite_structure(task_def.get("volumes", []), resource_mappings, sanitized_source, sanitized_target, sanitized_team),
            "placementConstraints": task_def.get("placementConstraints", []),
            "requiresCompatibilities": task_def.get("requiresCompatibilities", []),
            "cpu": task_def.get("cpu"),
            "memory": task_def.get("memory"),
            "runtimePlatform": task_def.get("runtimePlatform"),
            "tags": task_def.get("tags", []),
        }
        payload = {key: value for key, value in payload.items() if value not in (None, [], {})}
        try:
            response = ecs_client.register_task_definition(**payload)
            target_task_def = response["taskDefinition"]
            mappings["ecs_task_definition_arns"][task_def["taskDefinitionArn"]] = target_task_def["taskDefinitionArn"]
            mappings["ecs_task_definition_families"][source_family] = target_family
            deployed.append({
                "source_task_definition": task_def["taskDefinitionArn"],
                "target_task_definition": target_task_def["taskDefinitionArn"],
                "operation": "registered",
            })
        except Exception as exc:
            failed.append({"source_task_definition": task_def.get("taskDefinitionArn"), "target_family": target_family, "error": str(exc)})
    return mappings, deployed, failed


def deploy_ecs_services(snapshot, ecs_client, resource_mappings, source_env, target_env, team, preserve_names=False, config=None):
    deployed = []
    failed = []
    for service in snapshot.get("ecs", {}).get("services", []):
        source_name = service.get("serviceName")
        if not source_name:
            continue
        if should_exclude("ecs_services", source_name, config or {}):
            continue
        target_service_name = target_name(source_name, source_env, target_env, team, preserve_names=preserve_names)
        source_cluster_arn = service.get("clusterArn")
        target_cluster_arn = resource_mappings.get("ecs_cluster_arns", {}).get(source_cluster_arn)
        source_task_def = service.get("taskDefinition")
        target_task_def = resource_mappings.get("ecs_task_definition_arns", {}).get(source_task_def, source_task_def)
        if not target_cluster_arn or not target_task_def:
            failed.append({"source_service": source_name, "target_service": target_service_name, "error": "Missing target cluster or task definition"})
            continue
        payload = {
            "cluster": target_cluster_arn,
            "serviceName": target_service_name,
            "taskDefinition": target_task_def,
            "desiredCount": service.get("desiredCount", 1),
            "launchType": service.get("launchType"),
            "capacityProviderStrategy": service.get("capacityProviderStrategy", []),
            "platformVersion": service.get("platformVersion"),
            "propagateTags": service.get("propagateTags"),
            "enableECSManagedTags": service.get("enableECSManagedTags", False),
            "deploymentConfiguration": service.get("deploymentConfiguration"),
            "networkConfiguration": remap_ecs_network_configuration(service.get("networkConfiguration"), resource_mappings),
            "schedulingStrategy": service.get("schedulingStrategy", "REPLICA"),
            "tags": service.get("tags", []),
        }
        payload = {key: value for key, value in payload.items() if value not in (None, [], {})}
        network_configuration = payload.get("networkConfiguration", {})
        awsvpc = network_configuration.get("awsvpcConfiguration", {})
        if awsvpc:
            unresolved_subnets = [item for item in awsvpc.get("subnets", []) if item not in resource_mappings.get("subnet_ids", {}).values()]
            unresolved_groups = [item for item in awsvpc.get("securityGroups", []) if item not in resource_mappings.get("security_group_ids", {}).values()]
            if unresolved_subnets or unresolved_groups:
                failed.append({
                    "source_service": source_name,
                    "target_service": target_service_name,
                    "error": "Missing remapped network resources for ECS service",
                })
                continue
        try:
            response = ecs_client.create_service(**payload)
            target_service = response["service"]
            operation = "created"
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            error_message = exc.response["Error"].get("Message", "")
            is_idempotent_retry = (
                error_code == "InvalidParameterException"
                and "Creation of service was not idempotent" in error_message
            )
            if error_code != "ServiceAlreadyExistsException" and not is_idempotent_retry:
                failed.append({"source_service": source_name, "target_service": target_service_name, "error": str(exc)})
                continue
            try:
                ecs_client.update_service(
                    cluster=target_cluster_arn,
                    service=target_service_name,
                    taskDefinition=target_task_def,
                    desiredCount=service.get("desiredCount", 1),
                )
                target_service = ecs_client.describe_services(cluster=target_cluster_arn, services=[target_service_name]).get("services", [{}])[0]
                operation = "updated"
            except Exception as update_exc:
                failed.append({"source_service": source_name, "target_service": target_service_name, "error": str(update_exc)})
                continue
        deployed.append({
            "source_service": source_name,
            "target_service": target_service_name,
            "target_cluster_arn": target_cluster_arn,
            "target_service_arn": target_service.get("serviceArn"),
            "operation": operation,
        })
    return deployed, failed


def main():
    args = parse_args()
    config = load_transfer_config(args.config)
    client_slug = resolve_client_slug(args.client_slug, config, source_env=args.source_env, target_env=args.target_env)
    snapshot_path, snapshot = load_snapshot(args.source_env, args.inventory_key, client_slug)
    deployment_dir = deployment_dir_path(args.target_env, args.deployment_key, client_slug)
    deployment_dir.mkdir(parents=True, exist_ok=True)
    if args.read_only_plan:
        plan = build_read_only_plan(
            snapshot,
            args.source_env,
            args.target_env,
            args.team,
            config=config,
            source_account_id=snapshot.get("account_id", ""),
            target_account_id="",
            source_region=snapshot.get("region", args.source_region or args.region),
            target_region=config_override(config, "target_region", args.region),
            client_slug=client_slug,
        )
        plan["source_snapshot"] = str(snapshot_path)
        plan["region"] = config_override(config, "target_region", args.region)
        plan_path = deployment_dir / "deployment_plan.json"
        plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        append_audit_event(
            "deploy_discovered_env",
            "read-only-plan",
            {"plan_path": str(plan_path), "planned_actions": plan["planned_actions"]},
            target_env=args.target_env,
            source_env=args.source_env,
            client_slug=client_slug,
        )
        print(json.dumps({"status": "ok", "mode": "read-only-plan", "plan_path": str(plan_path)}, indent=2))
        return
    source_region = config_override(config, "source_region", args.source_region or snapshot.get("region") or args.region)
    target_region = config_override(config, "target_region", args.region)
    preserve_names = bool(config_override(config, "preserve_names", source_region != target_region))
    source_external_id = args.source_external_id or config_override(config, "source_external_id", "")
    target_external_id = args.target_external_id or config_override(config, "target_external_id", "")
    source_session = session_for(source_region, args.source_role_arn, external_id=source_external_id)
    target_session = session_for(target_region, args.target_role_arn, external_id=target_external_id)
    source_account_id, target_account_id = ensure_target_scope_safe(
        source_session,
        target_session,
        source_region,
        target_region,
        config,
    )
    iam_client = target_session.client("iam")
    sqs_client = target_session.client("sqs")
    sns_client = target_session.client("sns")
    ecs_client = target_session.client("ecs")
    dynamodb_client = target_session.client("dynamodb")
    source_dynamodb_resource = source_session.resource("dynamodb")
    target_dynamodb_resource = target_session.resource("dynamodb")
    source_lambda_client = source_session.client("lambda")
    source_secrets_client = source_session.client("secretsmanager")
    target_lambda_client = target_session.client("lambda")
    target_secrets_client = target_session.client("secretsmanager")
    apigw_client = target_session.client("apigateway")
    codebuild_client = target_session.client("codebuild")
    ec2_client = target_session.client("ec2")

    resource_mappings = {
        "role_arns": {}, "role_names": {}, "queue_urls": {}, "queue_arns": {}, "queue_names": {},
        "topic_arns": {}, "topic_names": {}, "secret_arns": {}, "secret_names": {},
        "dynamodb_table_names": {}, "dynamodb_table_arns": {}, "dynamodb_stream_arns": {},
        "function_arns": {}, "function_names": {}, "api_ids": {},
        "vpc_ids": {}, "subnet_ids": {}, "route_table_ids": {}, "security_group_ids": {},
        "kms_key_ids": {}, "kms_aliases": {},
        "s3_bucket_names": {}, "s3_bucket_arns": {},
        "codebuild_project_arns": {}, "codebuild_project_names": {},
        "ecs_cluster_arns": {}, "ecs_cluster_names": {}, "ecs_task_definition_arns": {}, "ecs_task_definition_families": {},
        "source_account_id": source_account_id,
        "target_account_id": target_account_id,
        "target_env": sanitize_name(args.target_env), "team": sanitize_name(args.team) if args.team else "",
        "target_region": target_region,
    }
    if not snapshot.get("iam_roles"):
        snapshot["iam_roles"] = build_synthetic_lambda_roles(snapshot)
    resource_mappings.update(build_kms_mappings(snapshot, config))
    resource_mappings.update(
        build_s3_bucket_mappings(
            snapshot,
            args.source_env,
            args.target_env,
            source_account_id=source_account_id,
            target_account_id=target_account_id,
            preserve_names=preserve_names,
        )
    )
    role_mappings, deployed_roles, failed_roles = create_or_update_roles(snapshot, args.source_env, args.target_env, args.team, iam_client, preserve_names=preserve_names, config=config)
    resource_mappings.update(role_mappings)
    network_mappings, deployed_network, failed_network = create_or_update_network(
        snapshot,
        ec2_client,
        args.source_env,
        args.target_env,
        args.team,
        resource_mappings,
        preserve_names=preserve_names,
        config=config,
    )
    resource_mappings.update(network_mappings)
    queue_mappings, deployed_queues, failed_queues = create_or_update_sqs_queues(snapshot, args.source_env, args.target_env, args.team, sqs_client, resource_mappings, preserve_names=preserve_names, config=config)
    resource_mappings.update(queue_mappings)
    topic_mappings, deployed_topics, failed_topics = create_or_update_sns_topics(snapshot, args.source_env, args.target_env, args.team, sns_client, preserve_names=preserve_names, config=config)
    resource_mappings.update(topic_mappings)
    secret_mappings, deployed_secrets, failed_secrets = create_or_update_secrets(
        snapshot,
        args.source_env,
        args.target_env,
        args.team,
        source_secrets_client,
        target_secrets_client,
        resource_mappings,
        preserve_names=preserve_names,
        config=config,
    )
    resource_mappings.update(secret_mappings)
    dynamodb_mappings, deployed_tables, failed_tables = create_or_update_dynamodb_tables(snapshot, args.source_env, args.target_env, args.team, dynamodb_client, preserve_names=preserve_names, config=config)
    resource_mappings.update(dynamodb_mappings)
    copied_table_items, failed_table_item_copies = copy_dynamodb_table_items(
        snapshot,
        source_dynamodb_resource,
        target_dynamodb_resource,
        resource_mappings,
        config=config,
    )
    deployed_lambdas, failed_lambdas = deploy_lambda_functions(
        snapshot,
        args.source_env,
        args.target_env,
        args.team,
        source_lambda_client,
        target_lambda_client,
        resource_mappings,
        preserve_names=preserve_names,
        config=config,
    )
    adjusted_queue_visibilities = normalize_queue_visibility_for_mappings(snapshot, target_lambda_client, sqs_client, resource_mappings)
    deployed_event_mappings, failed_event_mappings = create_event_source_mappings(
        snapshot,
        target_lambda_client,
        iam_client,
        sqs_client,
        resource_mappings,
        target_region,
        config=config,
    )
    deployed_permissions, failed_permissions = apply_lambda_permissions(snapshot, target_lambda_client, resource_mappings, args.source_env, args.target_env, args.team, config=config)
    deployed_subscriptions, failed_subscriptions = create_sns_subscriptions(snapshot, sns_client, resource_mappings, config=config)
    api_ids, deployed_apis, failed_apis = create_api_gateways(snapshot, apigw_client, resource_mappings, args.source_env, args.target_env, args.team, preserve_names=preserve_names, config=config)
    resource_mappings["api_ids"] = api_ids
    codebuild_mappings, deployed_codebuild_projects, failed_codebuild_projects = deploy_codebuild_projects(
        snapshot,
        codebuild_client,
        resource_mappings,
        args.source_env,
        args.target_env,
        args.team,
        preserve_names=preserve_names,
        config=config,
    )
    resource_mappings.update(codebuild_mappings)
    ecs_cluster_mappings, deployed_ecs_clusters, failed_ecs_clusters = deploy_ecs_clusters(
        snapshot,
        ecs_client,
        args.source_env,
        args.target_env,
        args.team,
        preserve_names=preserve_names,
        config=config,
    )
    resource_mappings.update(ecs_cluster_mappings)
    ecs_task_mappings, deployed_ecs_task_definitions, failed_ecs_task_definitions = deploy_ecs_task_definitions(
        snapshot,
        ecs_client,
        resource_mappings,
        args.source_env,
        args.target_env,
        args.team,
        preserve_names=preserve_names,
        config=config,
    )
    resource_mappings.update(ecs_task_mappings)
    deployed_ecs_services, failed_ecs_services = deploy_ecs_services(
        snapshot,
        ecs_client,
        resource_mappings,
        args.source_env,
        args.target_env,
        args.team,
        preserve_names=preserve_names,
        config=config,
    )

    failures = {
        "roles": failed_roles,
        "queues": failed_queues,
        "topics": failed_topics,
        "secrets": failed_secrets,
        "dynamodb_tables": failed_tables,
        "dynamodb_table_items": failed_table_item_copies,
        "lambda_functions": failed_lambdas,
        "event_source_mappings": failed_event_mappings,
        "lambda_permissions": failed_permissions,
        "sns_subscriptions": failed_subscriptions,
        "api_gateways": failed_apis,
        "codebuild_projects": failed_codebuild_projects,
        "vpcs": failed_network["vpcs"],
        "subnets": failed_network["subnets"],
        "route_tables": failed_network["route_tables"],
        "security_groups": failed_network["security_groups"],
        "ecs_clusters": failed_ecs_clusters,
        "ecs_task_definitions": failed_ecs_task_definitions,
        "ecs_services": failed_ecs_services,
    }
    total_failures = sum(len(items) for items in failures.values())

    manifest = {
        "source_snapshot": str(snapshot_path),
        "inventory_key": inventory_dir_name(args.source_env, args.inventory_key),
        "source_env": sanitize_name(args.source_env),
        "deployment_key": deployment_dir_name(args.target_env, args.deployment_key),
        "target_env": sanitize_name(args.target_env),
        "team": sanitize_name(args.team) if args.team else "",
        "source_account_id": source_account_id,
        "target_account_id": target_account_id,
        "source_region": source_region,
        "region": target_region,
        "config_path": args.config,
        "source_role_arn": args.source_role_arn,
        "target_role_arn": args.target_role_arn,
        "roles": deployed_roles,
        "sqs_queues": deployed_queues,
        "sns_topics": deployed_topics,
        "secrets": deployed_secrets,
        "dynamodb_tables": deployed_tables,
        "dynamodb_table_items": copied_table_items,
        "lambda_functions": deployed_lambdas,
        "lambda_event_source_mappings": deployed_event_mappings,
        "lambda_permissions": deployed_permissions,
        "sns_subscriptions": deployed_subscriptions,
        "api_gateways": deployed_apis,
        "codebuild_projects": deployed_codebuild_projects,
        "vpcs": deployed_network["vpcs"],
        "subnets": deployed_network["subnets"],
        "route_tables": deployed_network["route_tables"],
        "security_groups": deployed_network["security_groups"],
        "ecs_clusters": deployed_ecs_clusters,
        "ecs_task_definitions": deployed_ecs_task_definitions,
        "ecs_services": deployed_ecs_services,
        "queue_visibility_adjustments": adjusted_queue_visibilities,
        "resource_mappings": resource_mappings,
        "preserve_names": preserve_names,
        "preflight_checks": build_preflight_assessment(
            snapshot,
            config,
            source_account_id=source_account_id,
            target_account_id=target_account_id,
            source_region=source_region,
            target_region=target_region,
            client_slug=client_slug,
        ),
        "failures": failures,
        "follow_up": {
            "manual_review_required": total_failures > 0,
            "cloudformation_stacks_review_required": len(snapshot.get("cloudformation_stacks", [])) > 0,
            "load_balancer_review_required": len(snapshot.get("load_balancers", [])) > 0,
            "s3_review_required": len(snapshot.get("s3_buckets", [])) > 0,
            "ecs_review_required": len(snapshot.get("ecs", {}).get("services", [])) > len(deployed_ecs_services),
        },
    }

    manifest_path = deployment_dir / "deployment_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    append_audit_event(
        "deploy_discovered_env",
        "ok" if total_failures == 0 else "partial",
        {
            "manifest_path": str(manifest_path),
            "failed_resource_count": total_failures,
            "deployed_lambda_count": len(deployed_lambdas),
            "deployed_dynamodb_count": len(deployed_tables),
        },
        target_env=args.target_env,
        source_env=args.source_env,
        client_slug=client_slug,
    )
    print(json.dumps({
        "status": "ok" if total_failures == 0 else "partial",
        "manifest_path": str(manifest_path),
        "deployed_role_count": len(deployed_roles),
        "deployed_queue_count": len(deployed_queues),
        "deployed_topic_count": len(deployed_topics),
        "deployed_secret_count": len(deployed_secrets),
        "deployed_dynamodb_count": len(deployed_tables),
        "copied_dynamodb_item_total": sum(item["copied_item_count"] for item in copied_table_items),
        "deployed_lambda_count": len(deployed_lambdas),
        "deployed_mapping_count": len(deployed_event_mappings),
        "deployed_permission_count": len(deployed_permissions),
        "deployed_subscription_count": len(deployed_subscriptions),
        "deployed_api_count": len(deployed_apis),
        "deployed_codebuild_project_count": len(deployed_codebuild_projects),
        "deployed_ecs_cluster_count": len(deployed_ecs_clusters),
        "deployed_ecs_task_definition_count": len(deployed_ecs_task_definitions),
        "deployed_ecs_service_count": len(deployed_ecs_services),
        "failed_resource_count": total_failures,
    }, indent=2))


if __name__ == "__main__":
    main()
