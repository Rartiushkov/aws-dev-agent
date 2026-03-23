import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from executor.scripts.audit_log import append_audit_event
from executor.scripts.deploy_discovered_env import rewrite_bucket_name
from executor.scripts.transfer_common import account_id_for, config_override, inventory_dir_path, load_transfer_config, resolve_client_slug, session_for


def parse_args():
    parser = argparse.ArgumentParser(description="Plan or execute S3 object transfer for discovered buckets.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--source-region", default="us-east-1")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--config", default="")
    parser.add_argument("--source-role-arn", default="")
    parser.add_argument("--target-role-arn", default="")
    parser.add_argument("--source-external-id", default="")
    parser.add_argument("--target-external-id", default="")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def build_s3_transfer_plan(snapshot, config=None, target_region="", target_account_id=""):
    config = config or {}
    overrides = config.get("overrides", {})
    source_env = snapshot.get("source_env", "") or overrides.get("source_env", "")
    target_env = overrides.get("target_env", "")
    preserve_names = bool(overrides.get("preserve_names", False))
    source_account_id = snapshot.get("account_id", "")
    buckets = []
    for bucket in snapshot.get("s3_buckets", []):
        name = bucket.get("Name", "")
        target_bucket = rewrite_bucket_name(
            name,
            source_env,
            target_env,
            source_account_id=source_account_id,
            target_account_id=target_account_id,
            preserve_names=preserve_names,
        )
        buckets.append({
            "source_bucket": name,
            "target_bucket": target_bucket,
            "source_region": bucket.get("Region", snapshot.get("region", "")),
            "object_count_status": "unknown",
            "mode": "copy-objects",
            "tags": bucket.get("Tags", []),
            "versioning": bucket.get("Versioning", {}),
            "bucket_encryption": bucket.get("BucketEncryption", {}),
            "lifecycle_rules": bucket.get("LifecycleRules", []),
            "cors_rules": bucket.get("CorsRules", []),
            "policy": bucket.get("Policy", ""),
            "notification_configuration": bucket.get("NotificationConfiguration", {}),
            "notification_arn_mapping": overrides.get("notification_arn_mapping", {}),
            "kms_key_mapping": overrides.get("kms_key_mapping", {}),
            "source_account_id": source_account_id,
            "target_account_id": target_account_id,
            "source_env": source_env,
            "target_env": target_env,
            "target_region": target_region or snapshot.get("region", ""),
            "manual_review": False,
        })
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_env": source_env,
        "target_env": target_env,
        "account_id": snapshot.get("account_id", ""),
        "target_account_id": target_account_id,
        "region": snapshot.get("region", ""),
        "target_region": target_region or snapshot.get("region", ""),
        "bucket_count": len(buckets),
        "buckets": buckets,
    }


def collect_bucket_keys(s3_client, bucket_name):
    paginator = s3_client.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket_name):
        for item in page.get("Contents", []):
            keys.append({"Key": item["Key"], "Size": item.get("Size", 0), "ETag": item.get("ETag", "")})
    return keys


def ensure_bucket(target_s3_client, bucket_name, region):
    existing = {item["Name"] for item in target_s3_client.list_buckets().get("Buckets", [])}
    if bucket_name in existing:
        return "existing"
    kwargs = {"Bucket": bucket_name}
    if region and region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    target_s3_client.create_bucket(**kwargs)
    return "created"


def rewrite_bucket_policy(policy_text, bucket_plan, target_bucket):
    if not policy_text:
        return ""
    updated = policy_text
    source_bucket = bucket_plan.get("source_bucket", target_bucket)
    source_region = bucket_plan.get("source_region", "")
    target_region = bucket_plan.get("target_region", "")
    source_account_id = bucket_plan.get("source_account_id", "")
    target_account_id = bucket_plan.get("target_account_id", "")
    source_env = bucket_plan.get("source_env", "")
    target_env = bucket_plan.get("target_env", "")
    updated = updated.replace(source_bucket, target_bucket)
    if source_region and target_region and source_region != target_region:
        updated = updated.replace(f":{source_region}:", f":{target_region}:")
    if source_account_id and target_account_id and source_account_id != target_account_id:
        updated = updated.replace(f":{source_account_id}:", f":{target_account_id}:")
        updated = updated.replace(f"/{source_account_id}/", f"/{target_account_id}/")
    if source_env and target_env:
        updated = updated.replace(source_env, target_env)
    return updated


def normalize_bucket_notification_configuration(notification_configuration):
    if not notification_configuration:
        return {}
    normalized = {}
    for key in [
        "TopicConfigurations",
        "QueueConfigurations",
        "LambdaFunctionConfigurations",
        "EventBridgeConfiguration",
    ]:
        value = notification_configuration.get(key)
        if value:
            normalized[key] = value
    return normalized


def rewrite_bucket_encryption(bucket_encryption, bucket_plan):
    if not bucket_encryption:
        return bucket_encryption
    kms_key_mapping = bucket_plan.get("kms_key_mapping", {})
    rewritten = {"Rules": []}
    for rule in bucket_encryption.get("Rules", []):
        updated_rule = dict(rule)
        default = dict(updated_rule.get("ApplyServerSideEncryptionByDefault", {}))
        key_id = default.get("KMSMasterKeyID")
        if key_id:
            default["KMSMasterKeyID"] = kms_key_mapping.get(key_id, key_id)
        if default:
            updated_rule["ApplyServerSideEncryptionByDefault"] = default
        rewritten["Rules"].append(updated_rule)
    return rewritten if rewritten["Rules"] else bucket_encryption


def rewrite_notification_arn(value, bucket_plan):
    if not value:
        return value
    updated = value
    mapping = bucket_plan.get("notification_arn_mapping", {})
    updated = mapping.get(updated, updated)
    source_region = bucket_plan.get("source_region", "")
    target_region = bucket_plan.get("target_region", "")
    source_account_id = bucket_plan.get("source_account_id", "")
    target_account_id = bucket_plan.get("target_account_id", "")
    source_env = bucket_plan.get("source_env", "")
    target_env = bucket_plan.get("target_env", "")
    if source_region and target_region and source_region != target_region:
        updated = updated.replace(f":{source_region}:", f":{target_region}:")
    if source_account_id and target_account_id and source_account_id != target_account_id:
        updated = updated.replace(f":{source_account_id}:", f":{target_account_id}:")
    if source_env and target_env:
        updated = updated.replace(source_env, target_env)
    return updated


def rewrite_notification_configuration(notification_configuration, bucket_plan):
    normalized = normalize_bucket_notification_configuration(notification_configuration)
    if not normalized:
        return {}
    rewritten = {}
    for key in ["QueueConfigurations", "TopicConfigurations", "LambdaFunctionConfigurations"]:
        values = []
        for item in normalized.get(key, []):
            updated = dict(item)
            arn_key = {
                "QueueConfigurations": "QueueArn",
                "TopicConfigurations": "TopicArn",
                "LambdaFunctionConfigurations": "LambdaFunctionArn",
            }[key]
            if updated.get(arn_key):
                updated[arn_key] = rewrite_notification_arn(updated[arn_key], bucket_plan)
            values.append(updated)
        if values:
            rewritten[key] = values
    if normalized.get("EventBridgeConfiguration"):
        rewritten["EventBridgeConfiguration"] = normalized["EventBridgeConfiguration"]
    return rewritten


def apply_bucket_configuration(target_s3_client, bucket_plan, bucket_name):
    issues = []
    applied = []

    tags = bucket_plan.get("tags", [])
    if tags:
        try:
            target_s3_client.put_bucket_tagging(Bucket=bucket_name, Tagging={"TagSet": tags})
            applied.append("tags")
        except Exception as exc:
            issues.append(f"tags: {exc}")

    versioning = bucket_plan.get("versioning", {})
    if versioning.get("Status"):
        try:
            target_s3_client.put_bucket_versioning(
                Bucket=bucket_name,
                VersioningConfiguration={"Status": versioning["Status"]},
            )
            applied.append("versioning")
        except Exception as exc:
            issues.append(f"versioning: {exc}")

    encryption = rewrite_bucket_encryption(bucket_plan.get("bucket_encryption", {}), bucket_plan)
    if encryption:
        try:
            target_s3_client.put_bucket_encryption(
                Bucket=bucket_name,
                ServerSideEncryptionConfiguration=encryption,
            )
            applied.append("encryption")
        except Exception as exc:
            issues.append(f"encryption: {exc}")

    lifecycle_rules = bucket_plan.get("lifecycle_rules", [])
    if lifecycle_rules:
        try:
            target_s3_client.put_bucket_lifecycle_configuration(
                Bucket=bucket_name,
                LifecycleConfiguration={"Rules": lifecycle_rules},
            )
            applied.append("lifecycle")
        except Exception as exc:
            issues.append(f"lifecycle: {exc}")

    cors_rules = bucket_plan.get("cors_rules", [])
    if cors_rules:
        try:
            target_s3_client.put_bucket_cors(
                Bucket=bucket_name,
                CORSConfiguration={"CORSRules": cors_rules},
            )
            applied.append("cors")
        except Exception as exc:
            issues.append(f"cors: {exc}")

    policy = rewrite_bucket_policy(bucket_plan.get("policy", ""), bucket_plan, bucket_name)
    if policy:
        try:
            target_s3_client.put_bucket_policy(Bucket=bucket_name, Policy=policy)
            applied.append("policy")
        except Exception as exc:
            issues.append(f"policy: {exc}")

    notification_configuration = rewrite_notification_configuration(
        bucket_plan.get("notification_configuration", {}),
        bucket_plan,
    )
    if notification_configuration:
        try:
            target_s3_client.put_bucket_notification_configuration(
                Bucket=bucket_name,
                NotificationConfiguration=notification_configuration,
            )
            applied.append("notifications")
        except Exception as exc:
            issues.append(f"notifications: {exc}")

    return applied, issues


def execute_s3_transfer(plan, source_s3_client, target_s3_client, target_region):
    results = []
    for bucket in plan.get("buckets", []):
        source_bucket = bucket["source_bucket"]
        target_bucket = bucket["target_bucket"]
        bucket_result = {
            "source_bucket": source_bucket,
            "target_bucket": target_bucket,
            "bucket_status": "",
            "copied_objects": 0,
            "issues": [],
        }
        try:
            bucket_result["bucket_status"] = ensure_bucket(target_s3_client, target_bucket, target_region)
            bucket.setdefault("source_region", bucket.get("source_region", plan.get("region", "")))
            bucket.setdefault("target_region", plan.get("target_region", target_region))
            bucket.setdefault("source_account_id", plan.get("account_id", ""))
            bucket.setdefault("target_account_id", plan.get("target_account_id", ""))
            bucket.setdefault("source_env", plan.get("source_env", ""))
            bucket.setdefault("target_env", plan.get("target_env", ""))
            applied, config_issues = apply_bucket_configuration(target_s3_client, bucket, target_bucket)
            bucket_result["applied_bucket_configuration"] = applied
            bucket_result["issues"].extend(config_issues)
            keys = collect_bucket_keys(source_s3_client, source_bucket)
            target_keys = {item["Key"]: item for item in collect_bucket_keys(target_s3_client, target_bucket)}
            bucket_result["source_object_count"] = len(keys)
            bucket_result["skipped_objects"] = 0
            for item in keys:
                existing = target_keys.get(item["Key"])
                if existing and existing.get("Size") == item.get("Size") and existing.get("ETag") == item.get("ETag"):
                    bucket_result["skipped_objects"] += 1
                    continue
                response = source_s3_client.get_object(Bucket=source_bucket, Key=item["Key"])
                extra_args = {}
                metadata = response.get("Metadata")
                if metadata:
                    extra_args["Metadata"] = metadata
                for field in ["ContentType", "CacheControl", "ContentDisposition", "ContentEncoding", "ContentLanguage"]:
                    if response.get(field):
                        extra_args[field] = response[field]
                target_s3_client.put_object(
                    Bucket=target_bucket,
                    Key=item["Key"],
                    Body=response["Body"].read(),
                    **extra_args,
                )
                try:
                    tags = source_s3_client.get_object_tagging(Bucket=source_bucket, Key=item["Key"]).get("TagSet", [])
                    if tags:
                        target_s3_client.put_object_tagging(Bucket=target_bucket, Key=item["Key"], Tagging={"TagSet": tags})
                except Exception:
                    pass
                bucket_result["copied_objects"] += 1
        except Exception as exc:
            bucket_result["issues"].append(str(exc))
        results.append(bucket_result)
    return results


def main():
    args = parse_args()
    source_env = args.source_env or "full-account-scan"
    config = load_transfer_config(args.config)
    client_slug = resolve_client_slug(args.client_slug, config, source_env=source_env)
    inventory_dir = inventory_dir_path(source_env, client_slug=client_slug)
    snapshot = json.loads((inventory_dir / "source_snapshot.json").read_text(encoding="utf-8"))
    target_account_id = ""
    if args.execute:
        target_external_id = args.target_external_id or config_override(config, "target_external_id", "")
        target_session = session_for(args.region, args.target_role_arn, external_id=target_external_id)
        target_account_id = account_id_for(target_session)
    plan = build_s3_transfer_plan(snapshot, config=config, target_region=args.region, target_account_id=target_account_id)
    plan_path = inventory_dir / "s3_transfer_plan.json"
    execution_results = []
    if args.execute and plan["bucket_count"] > 0:
        source_external_id = args.source_external_id or config_override(config, "source_external_id", "")
        source_session = session_for(
            args.source_region or snapshot.get("region") or args.region,
            args.source_role_arn,
            external_id=source_external_id,
        )
        target_session = session_for(args.region, args.target_role_arn, external_id=target_external_id)
        execution_results = execute_s3_transfer(
            plan,
            source_session.client("s3"),
            target_session.client("s3"),
            args.region,
        )
        plan["execution_results"] = execution_results
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    append_audit_event(
        "transfer_s3_objects",
        "ok",
        {"plan_path": str(plan_path), "bucket_count": plan["bucket_count"], "executed": args.execute},
        source_env=source_env,
        client_slug=client_slug,
    )
    print(json.dumps({
        "status": "ok",
        "plan_path": str(plan_path),
        "bucket_count": plan["bucket_count"],
        "executed": args.execute,
    }, indent=2))


if __name__ == "__main__":
    main()
