import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from executor.scripts.audit_log import append_audit_event
from executor.scripts.transfer_common import inventory_dir_path, resolve_client_slug


TEST_NAME_HINTS = ("test", "demo", "sandbox", "dev", "staging", "hello-world", "tmp", "temp", "playground")
GRAVITON_RUNTIMES = ("python", "nodejs")


def parse_args():
    parser = argparse.ArgumentParser(description="Analyze a discovered AWS snapshot for cost-saving opportunities.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def _matches_any_hint(value, hints):
    lowered = str(value or "").lower()
    return any(hint in lowered for hint in hints)


def _add_opportunity(opportunities, impact, confidence, category, title, resource_id="", rationale="", recommendation="", automation_ready=False):
    opportunities.append({
        "impact": impact,
        "confidence": confidence,
        "category": category,
        "title": title,
        "resource_id": resource_id,
        "rationale": rationale,
        "recommendation": recommendation,
        "automation_ready": automation_ready,
    })


def _service_is_fargate(service):
    strategies = service.get("capacityProviderStrategy", []) or []
    if any(item.get("capacityProvider") == "FARGATE" for item in strategies):
        return True
    launch_type = str(service.get("launchType", "")).upper()
    return launch_type == "FARGATE"


def _service_uses_fargate_spot(service):
    strategies = service.get("capacityProviderStrategy", []) or []
    return any(item.get("capacityProvider") == "FARGATE_SPOT" for item in strategies)


def _count_restart_like_events(service):
    total = 0
    for event in service.get("events", []):
        message = str(event.get("message", "")).lower()
        if "unable to place a task" in message or "failed to start" in message or "cannotpullcontainererror" in message:
            total += 1
    return total


def _find_task_definition(task_definitions, task_definition_arn):
    for item in task_definitions:
        if item.get("taskDefinitionArn") == task_definition_arn:
            return item
    return {}


def build_cost_report(snapshot):
    opportunities = []
    strengths = []

    ecs = snapshot.get("ecs", {})
    task_definitions = ecs.get("task_definitions", [])
    for service in ecs.get("services", []):
        service_name = service.get("serviceName", "")
        restart_events = _count_restart_like_events(service)
        if restart_events >= 2 and int(service.get("desiredCount", 0) or 0) > 0 and int(service.get("runningCount", 0) or 0) == 0:
            _add_opportunity(
                opportunities,
                "high",
                "high",
                "ecs-waste",
                "Pause failing ECS service until its image or task definition is fixed",
                service.get("serviceArn", ""),
                (
                    f"{service_name} keeps retrying failed task launches "
                    f"({restart_events} recent failure events) while desiredCount remains > 0."
                ),
                "Set desiredCount to 0 or disable the service until the container image and startup path are healthy.",
                automation_ready=True,
            )

        if _matches_any_hint(service_name, TEST_NAME_HINTS) and _service_is_fargate(service) and not _service_uses_fargate_spot(service):
            _add_opportunity(
                opportunities,
                "medium",
                "medium",
                "ecs-spot",
                "Move non-production ECS service to Fargate Spot when interruption is acceptable",
                service.get("serviceArn", ""),
                f"{service_name} looks like a non-production workload and currently uses standard Fargate capacity.",
                "Switch the capacity provider strategy to FARGATE_SPOT for interrupt-tolerant environments.",
                automation_ready=False,
            )

        task_definition = _find_task_definition(task_definitions, service.get("taskDefinition"))
        cpu = int(task_definition.get("cpu", "0") or "0")
        memory = int(task_definition.get("memory", "0") or "0")
        if _matches_any_hint(service_name, TEST_NAME_HINTS) and cpu >= 1024 and memory >= 2048:
            _add_opportunity(
                opportunities,
                "medium",
                "medium",
                "ecs-rightsizing",
                "Review ECS task size for non-production workload",
                service.get("serviceArn", ""),
                f"{service_name} uses a task definition sized at {cpu} CPU units and {memory} MiB memory.",
                "Validate actual CPU and memory usage and reduce the Fargate task size if headroom is consistently high.",
                automation_ready=False,
            )

    for fn in snapshot.get("lambda_functions", []):
        function_name = fn.get("FunctionName", "")
        runtime = str(fn.get("Runtime", "")).lower()
        architectures = [str(item).lower() for item in fn.get("Architectures", [])]
        if any(runtime.startswith(prefix) for prefix in GRAVITON_RUNTIMES) and "x86_64" in architectures:
            _add_opportunity(
                opportunities,
                "low",
                "medium",
                "lambda-graviton",
                "Review Lambda for arm64 migration",
                fn.get("FunctionArn", ""),
                f"{function_name} runs on {fn.get('Runtime', '')} with x86_64 architecture.",
                "Benchmark the function on arm64 and migrate if dependencies are compatible.",
                automation_ready=False,
            )
        if _matches_any_hint(function_name, TEST_NAME_HINTS):
            _add_opportunity(
                opportunities,
                "medium",
                "medium",
                "resource-cleanup",
                "Review test or temporary Lambda for removal",
                fn.get("FunctionArn", ""),
                f"{function_name} looks like a test or temporary function that may no longer be needed.",
                "Confirm recent usage and delete the function if it is no longer serving a workflow.",
                automation_ready=True,
            )

    for bucket in snapshot.get("s3_buckets", []):
        lifecycle_rules = bucket.get("LifecycleRules", []) or []
        if not lifecycle_rules:
            _add_opportunity(
                opportunities,
                "medium",
                "medium",
                "s3-lifecycle",
                "Review S3 lifecycle policies",
                bucket.get("Name", ""),
                f"{bucket.get('Name', '')} has no lifecycle rules in the snapshot.",
                "Add lifecycle policies or review Intelligent-Tiering for stale objects and incomplete multipart uploads.",
                automation_ready=False,
            )

    dynamodb_tables = snapshot.get("dynamodb_tables", [])
    pay_per_request_count = 0
    for table in dynamodb_tables:
        billing_mode = (
            table.get("Table", {})
            .get("BillingModeSummary", {})
            .get("BillingMode", "")
        )
        if billing_mode == "PAY_PER_REQUEST":
            pay_per_request_count += 1
    if dynamodb_tables and pay_per_request_count == len(dynamodb_tables):
        strengths.append("All discovered DynamoDB tables already use PAY_PER_REQUEST, which avoids idle provisioned capacity waste.")

    summary = {
        "opportunity_count": len(opportunities),
        "automation_ready_count": sum(1 for item in opportunities if item["automation_ready"]),
        "high_impact_count": sum(1 for item in opportunities if item["impact"] == "high"),
        "medium_impact_count": sum(1 for item in opportunities if item["impact"] == "medium"),
        "low_impact_count": sum(1 for item in opportunities if item["impact"] == "low"),
    }

    next_level_requirements = [
        "Cost Explorer or CUR data to attach dollar impact instead of heuristic ranking.",
        "CloudWatch utilization history to support rightsizing with evidence.",
        "Tag coverage by team, env, and owner to attribute spend and automate cleanup safely.",
        "Approval workflow for stop, delete, and resize actions.",
    ]

    opportunities.sort(key=lambda item: ("high", "medium", "low").index(item["impact"]))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_env": snapshot.get("source_env", ""),
        "region": snapshot.get("region", ""),
        "account_id": snapshot.get("account_id", ""),
        "summary": summary,
        "strengths": strengths,
        "opportunities": opportunities,
        "next_level_requirements": next_level_requirements,
        "disclaimer": "This report ranks likely optimization candidates from infrastructure metadata. Dollar estimates require billing data.",
    }


def main():
    args = parse_args()
    source_env = args.source_env or "full-account-scan"
    client_slug = resolve_client_slug(args.client_slug, source_env=source_env)
    inventory_dir = inventory_dir_path(source_env, client_slug=client_slug)
    snapshot = json.loads((inventory_dir / "source_snapshot.json").read_text(encoding="utf-8"))
    report = build_cost_report(snapshot)
    report_path = inventory_dir / "cost_opportunities_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    append_audit_event("analyze_cost_opportunities", "ok", {"report_path": str(report_path)}, source_env=source_env, client_slug=client_slug)
    print(json.dumps({"status": "ok", "report_path": str(report_path), "opportunity_count": report["summary"]["opportunity_count"]}, indent=2))


if __name__ == "__main__":
    main()
