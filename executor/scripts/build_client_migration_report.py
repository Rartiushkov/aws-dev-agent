import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from executor.scripts.audit_log import append_audit_event
from executor.scripts.transfer_common import deployment_dir_path, inventory_dir_path, resolve_client_slug


def parse_args():
    parser = argparse.ArgumentParser(description="Build a client-facing migration report.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--target-env", required=True)
    parser.add_argument("--inventory-key", default="")
    parser.add_argument("--deployment-key", default="")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def load_json(path):
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_validation(report):
    checks = report.get("smoke_checks", [])
    return {
        "issues_found": report.get("issues_found", False),
        "passed_checks": sum(1 for item in checks if item.get("status") == "ok"),
        "failed_checks": sum(1 for item in checks if item.get("status") != "ok"),
        "failed_check_names": [item.get("name") for item in checks if item.get("status") != "ok"],
    }


def summarize_cloudformation(deploy_result, import_result):
    deploy_items = deploy_result.get("results", [])
    import_items = import_result.get("results", [])
    return {
        "deploy_attempted": len(deploy_items),
        "import_attempted": len(import_items),
        "imported": sum(1 for item in import_items if item.get("operation") == "imported"),
        "import_required": sum(1 for item in deploy_items if item.get("import_required")),
        "failed": sum(1 for item in deploy_items + import_items if item.get("operation") == "failed"),
    }


def build_report(target_env, deployment_manifest, validation_report, cloudformation_deploy_result, cloudformation_import_result):
    cf_summary = summarize_cloudformation(cloudformation_deploy_result, cloudformation_import_result)
    validation_summary = summarize_validation(validation_report)
    return {
        "target_env": target_env,
        "summary": {
            "roles": len(deployment_manifest.get("roles", [])),
            "queues": len(deployment_manifest.get("sqs_queues", [])),
            "tables": len(deployment_manifest.get("dynamodb_tables", [])),
            "lambdas": len(deployment_manifest.get("lambda_functions", [])),
            "ecs_services": len(deployment_manifest.get("ecs_services", [])),
            "codebuild_projects": len(deployment_manifest.get("codebuild_projects", [])),
            "cloudformation": cf_summary,
            "validation": validation_summary,
        },
        "outcome": "ready" if not validation_summary["issues_found"] else "needs-review",
        "messages": [
            f"Deployed {len(deployment_manifest.get('lambda_functions', []))} Lambda functions and {len(deployment_manifest.get('dynamodb_tables', []))} DynamoDB tables.",
            f"CloudFormation imports completed: {cf_summary['imported']}.",
            f"Validation checks passed: {validation_summary['passed_checks']}/{validation_summary['passed_checks'] + validation_summary['failed_checks']}.",
        ],
        "cloudformation_deploy_result": cloudformation_deploy_result,
        "cloudformation_import_result": cloudformation_import_result,
    }


def main():
    args = parse_args()
    source_env = args.source_env or "full-account-scan"
    client_slug = resolve_client_slug(args.client_slug, source_env=source_env, target_env=args.target_env)
    inventory_dir = inventory_dir_path(source_env, args.inventory_key, client_slug)
    deployment_dir = deployment_dir_path(args.target_env, args.deployment_key, client_slug)

    deployment_manifest = load_json(deployment_dir / "deployment_manifest.json")
    validation_report = load_json(deployment_dir / "validation_report.json")
    cloudformation_deploy_result = load_json(inventory_dir / f"cloudformation_deploy_result_{args.target_env}.json")
    cloudformation_import_result = load_json(inventory_dir / f"cloudformation_import_result_{args.target_env}.json")

    report = build_report(
        args.target_env,
        deployment_manifest,
        validation_report,
        cloudformation_deploy_result,
        cloudformation_import_result,
    )
    report_path = inventory_dir / f"client_migration_report_{args.target_env}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    append_audit_event("build_client_migration_report", "ok", {"report_path": str(report_path)}, source_env=source_env, target_env=args.target_env, client_slug=client_slug)
    print(json.dumps({"status": "ok", "report_path": str(report_path)}, indent=2))


if __name__ == "__main__":
    main()
