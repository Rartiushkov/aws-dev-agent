import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from executor.scripts.audit_log import append_audit_event
from executor.scripts.transfer_common import inventory_dir_path, resolve_client_slug


def parse_args():
    parser = argparse.ArgumentParser(description="Analyze KMS usage references in a discovered snapshot.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def record_kms_usage(keys, key_id, usage_type, name):
    if not key_id:
        return
    keys.setdefault(key_id, {"key_id": key_id, "used_by": []})
    keys[key_id]["used_by"].append({"type": usage_type, "name": name})


def build_kms_report(snapshot, config=None):
    configured = (config or {}).get("overrides", {}).get("kms_key_mapping", {})
    keys = {}
    for secret in snapshot.get("secrets", []):
        record_kms_usage(keys, secret.get("KmsKeyId"), "secret", secret.get("Name", ""))
    for queue in snapshot.get("sqs_queues", []):
        record_kms_usage(keys, queue.get("Attributes", {}).get("KmsMasterKeyId"), "sqs", queue.get("QueueName", ""))
    for project in snapshot.get("codebuild_projects", []):
        record_kms_usage(keys, project.get("encryptionKey"), "codebuild", project.get("name", ""))
    for bucket in snapshot.get("s3_buckets", []):
        encryption = bucket.get("BucketEncryption", {})
        for rule in encryption.get("Rules", []):
            default = rule.get("ApplyServerSideEncryptionByDefault", {})
            record_kms_usage(keys, default.get("KMSMasterKeyID"), "s3", bucket.get("Name", ""))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_env": snapshot.get("source_env", ""),
        "region": snapshot.get("region", ""),
        "kms_key_count": len(keys),
        "keys": [
            {
                **item,
                "suggested_target_key": configured.get(item["key_id"], item["key_id"] if item["key_id"].startswith("alias/") else ""),
            }
            for item in keys.values()
        ],
        "recommended_next_step": "Map customer-managed KMS keys and key policies before production migration.",
    }


def main():
    args = parse_args()
    source_env = args.source_env or "full-account-scan"
    client_slug = resolve_client_slug(args.client_slug, source_env=source_env)
    inventory_dir = inventory_dir_path(source_env, client_slug=client_slug)
    snapshot = json.loads((inventory_dir / "source_snapshot.json").read_text(encoding="utf-8"))
    report = build_kms_report(snapshot)
    report_path = inventory_dir / "kms_usage_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    append_audit_event("analyze_kms_usage", "ok", {"report_path": str(report_path)}, source_env=source_env, client_slug=client_slug)
    print(json.dumps({"status": "ok", "report_path": str(report_path), "kms_key_count": report["kms_key_count"]}, indent=2))


if __name__ == "__main__":
    main()
