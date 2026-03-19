import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3


def sanitize_name(value):
    return value.strip().lower().replace(" ", "-")


def parse_args():
    parser = argparse.ArgumentParser(description="Validate a deployed cloned environment.")
    parser.add_argument("--target-env", required=True)
    parser.add_argument("--region", default="us-east-1")
    return parser.parse_args()


def load_manifest(target_env):
    manifest_path = Path("state") / "deployments" / sanitize_name(target_env) / "deployment_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Deployment manifest not found: {manifest_path}")
    return manifest_path, json.loads(manifest_path.read_text(encoding="utf-8"))


def lambda_log_errors(logs_client, function_name):
    start_time = int((datetime.now(timezone.utc) - timedelta(minutes=15)).timestamp() * 1000)
    group_name = f"/aws/lambda/{function_name}"
    try:
        response = logs_client.filter_log_events(
            logGroupName=group_name,
            startTime=start_time,
            filterPattern='?ERROR ?Error ?Exception ?Task timed out ?AccessDenied',
        )
        return [event["message"] for event in response.get("events", [])][:20]
    except Exception as exc:
        return [f"log-check-failed: {exc}"]


def main():
    args = parse_args()
    manifest_path, manifest = load_manifest(args.target_env)
    lambda_client = boto3.client("lambda", region_name=args.region)
    logs_client = boto3.client("logs", region_name=args.region)

    report = {
        "manifest_path": str(manifest_path),
        "target_env": sanitize_name(args.target_env),
        "region": args.region,
        "functions": [],
        "issues_found": False,
    }

    for item in manifest.get("lambda_functions", []):
        function_name = item["target_function"]
        config = lambda_client.get_function_configuration(FunctionName=function_name)
        issues = []
        if config.get("State") != "Active":
            issues.append(f"state={config.get('State')}")
        if config.get("LastUpdateStatus") not in {"Successful", None}:
            issues.append(f"last_update_status={config.get('LastUpdateStatus')}")
        log_issues = lambda_log_errors(logs_client, function_name)
        if log_issues:
            issues.extend(log_issues)

        report["functions"].append({
            "function_name": function_name,
            "state": config.get("State"),
            "last_update_status": config.get("LastUpdateStatus"),
            "issues": issues,
        })

        if issues:
            report["issues_found"] = True

    report_dir = Path("state") / "deployments" / sanitize_name(args.target_env)
    report_path = report_dir / "validation_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps({
        "status": "ok",
        "report_path": str(report_path),
        "issues_found": report["issues_found"],
    }, indent=2))


if __name__ == "__main__":
    main()
