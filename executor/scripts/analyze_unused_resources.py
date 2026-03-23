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


TEST_NAME_HINTS = ("test", "demo", "sandbox", "dev", "staging", "hello-world", "tmp", "temp", "playground")


def parse_args():
    parser = argparse.ArgumentParser(description="Analyze a discovered AWS snapshot for likely unused or wasteful resources.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def _matches_hint(value):
    lowered = str(value or "").lower()
    return any(hint in lowered for hint in TEST_NAME_HINTS)


def _add_finding(findings, category, title, resource_id="", confidence="medium", waste_type="likely-unused", why="", recommended_action=""):
    findings.append({
        "category": category,
        "title": title,
        "resource_id": resource_id,
        "confidence": confidence,
        "waste_type": waste_type,
        "why": why,
        "recommended_action": recommended_action,
    })


def build_unused_resource_report(snapshot):
    findings = []
    lambda_mappings_by_function = {}

    for mapping in snapshot.get("lambda_event_source_mappings", []):
        lambda_mappings_by_function.setdefault(mapping.get("FunctionArn"), []).append(mapping)
        if mapping.get("State") == "Disabled":
            _add_finding(
                findings,
                "disabled-trigger",
                "Disabled event source mapping may indicate a paused or unused workflow",
                mapping.get("EventSourceMappingArn", ""),
                confidence="high",
                waste_type="paused-unused-path",
                why=f"Mapping from {mapping.get('EventSourceArn', '')} to {mapping.get('FunctionArn', '')} is disabled.",
                recommended_action="Confirm whether this flow is still needed. Remove the mapping or retire the upstream queue/stream if the workflow is gone.",
            )
        if "PROBLEM:" in str(mapping.get("LastProcessingResult", "")):
            _add_finding(
                findings,
                "broken-trigger",
                "Enabled event source mapping is failing to process",
                mapping.get("EventSourceMappingArn", ""),
                confidence="high",
                waste_type="broken-cost-path",
                why=str(mapping.get("LastProcessingResult", "")),
                recommended_action="Fix the execution role or disable the mapping until the downstream path is healthy.",
            )

    for fn in snapshot.get("lambda_functions", []):
        name = fn.get("FunctionName", "")
        arn = fn.get("FunctionArn", "")
        mappings = lambda_mappings_by_function.get(arn, [])
        if _matches_hint(name):
            _add_finding(
                findings,
                "test-lambda",
                "Lambda function looks like test or temporary code",
                arn,
                confidence="medium",
                waste_type="likely-unused",
                why=f"{name} matches common test or temporary naming patterns.",
                recommended_action="Check whether it is still invoked in a real workflow and remove it if not needed.",
            )
        if not mappings and _matches_hint(name):
            _add_finding(
                findings,
                "orphan-lambda",
                "Lambda has no discovered event source mappings and appears non-production",
                arn,
                confidence="medium",
                waste_type="likely-unused",
                why=f"{name} has no discovered event source mappings in the snapshot.",
                recommended_action="Confirm there are no schedules or manual invocations keeping it alive; if not, retire it.",
            )

    for queue in snapshot.get("sqs_queues", []):
        attrs = queue.get("Attributes", {})
        visible = int(attrs.get("ApproximateNumberOfMessages", "0") or "0")
        not_visible = int(attrs.get("ApproximateNumberOfMessagesNotVisible", "0") or "0")
        delayed = int(attrs.get("ApproximateNumberOfMessagesDelayed", "0") or "0")
        name = queue.get("QueueName", "")
        if visible == 0 and not_visible == 0 and delayed == 0 and _matches_hint(name):
            _add_finding(
                findings,
                "idle-queue",
                "Queue is empty and looks like non-production infrastructure",
                attrs.get("QueueArn", queue.get("QueueUrl", "")),
                confidence="medium",
                waste_type="likely-unused",
                why=f"{name} is empty in the snapshot and matches test/dev naming patterns.",
                recommended_action="Validate that no producer or consumer still depends on it, then delete it if safe.",
            )

    for bucket in snapshot.get("s3_buckets", []):
        if not (bucket.get("LifecycleRules", []) or []):
            _add_finding(
                findings,
                "bucket-lifecycle",
                "Bucket has no lifecycle rules and may grow storage cost over time",
                bucket.get("Name", ""),
                confidence="medium",
                waste_type="storage-growth-risk",
                why=f"{bucket.get('Name', '')} has no lifecycle rules in the snapshot.",
                recommended_action="Add lifecycle rules for retention, archival, or incomplete multipart cleanup.",
            )

    for table in snapshot.get("dynamodb_tables", []):
        table_data = table.get("Table", {})
        size_bytes = int(table_data.get("TableSizeBytes", 0) or 0)
        name = table_data.get("TableName", "")
        if size_bytes == 0 and _matches_hint(name):
            _add_finding(
                findings,
                "empty-table",
                "DynamoDB table is empty and looks non-production",
                table_data.get("TableArn", name),
                confidence="medium",
                waste_type="likely-unused",
                why=f"{name} has TableSizeBytes=0 and matches test/dev naming patterns.",
                recommended_action="Confirm there are no active readers/writers and delete it if abandoned.",
            )

    for service in snapshot.get("ecs", {}).get("services", []):
        name = service.get("serviceName", "")
        desired = int(service.get("desiredCount", 0) or 0)
        running = int(service.get("runningCount", 0) or 0)
        failure_events = sum(
            1
            for event in service.get("events", [])
            if "unable to place a task" in str(event.get("message", "")).lower()
            or "failed to start" in str(event.get("message", "")).lower()
            or "cannotpullcontainererror" in str(event.get("message", "")).lower()
        )
        if desired > 0 and running == 0 and failure_events >= 2:
            _add_finding(
                findings,
                "failing-ecs-service",
                "ECS service is retrying but has no healthy running tasks",
                service.get("serviceArn", ""),
                confidence="high",
                waste_type="active-waste",
                why=f"{name} has desiredCount={desired}, runningCount={running}, and {failure_events} recent failure events.",
                recommended_action="Set desiredCount to 0 until the image and startup path are fixed.",
            )

    findings.sort(key=lambda item: {"high": 3, "medium": 2, "low": 1}.get(item["confidence"], 0), reverse=True)
    summary = {
        "finding_count": len(findings),
        "high_confidence_count": sum(1 for item in findings if item["confidence"] == "high"),
        "medium_confidence_count": sum(1 for item in findings if item["confidence"] == "medium"),
        "low_confidence_count": sum(1 for item in findings if item["confidence"] == "low"),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_env": snapshot.get("source_env", ""),
        "region": snapshot.get("region", ""),
        "account_id": snapshot.get("account_id", ""),
        "summary": summary,
        "findings": findings,
    }


def main():
    args = parse_args()
    source_env = args.source_env or "full-account-scan"
    client_slug = resolve_client_slug(args.client_slug, source_env=source_env)
    inventory_dir = inventory_dir_path(source_env, client_slug=client_slug)
    snapshot = json.loads((inventory_dir / "source_snapshot.json").read_text(encoding="utf-8"))
    report = build_unused_resource_report(snapshot)
    report_path = inventory_dir / "unused_resource_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    append_audit_event("analyze_unused_resources", "ok", {"report_path": str(report_path)}, source_env=source_env, client_slug=client_slug)
    print(json.dumps({"status": "ok", "report_path": str(report_path), "finding_count": report["summary"]["finding_count"]}, indent=2))


if __name__ == "__main__":
    main()
