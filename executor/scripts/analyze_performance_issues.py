import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from botocore.exceptions import ClientError, NoCredentialsError

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from executor.scripts.audit_log import append_audit_event
from executor.scripts.transfer_common import config_override, inventory_dir_path, load_transfer_config, resolve_client_slug, session_for


SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1}


def parse_args():
    parser = argparse.ArgumentParser(description="Analyze a discovered AWS snapshot for likely performance bottlenecks.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--config", default="")
    parser.add_argument("--source-role-arn", default="")
    parser.add_argument("--source-external-id", default="")
    parser.add_argument("--live-metrics", action="store_true")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def _resolve_inventory_dir(source_env, region, client_slug=""):
    base_dir = inventory_dir_path(client_slug=client_slug).parent
    direct = base_dir / source_env
    regional = base_dir / f"{source_env}-{region}"

    def has_signal_resources(path):
        summary_path = path / "summary.json"
        if not summary_path.exists():
            return False
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if summary.get("has_signal_resources"):
            return True
        return bool(summary.get("signal_resource_count", 0))

    if has_signal_resources(direct):
        return direct
    if has_signal_resources(regional):
        return regional
    if direct.exists():
        return direct
    return regional


def _add_finding(findings, severity, category, title, resource_id="", evidence="", probable_cause="", recommended_action=""):
    findings.append({
        "severity": severity,
        "category": category,
        "title": title,
        "resource_id": resource_id,
        "evidence": evidence,
        "probable_cause": probable_cause,
        "recommended_action": recommended_action,
    })


def _last_datapoint(cloudwatch_client, namespace, metric_name, dimensions, statistics, hours=24, period=300):
    try:
        response = cloudwatch_client.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=(datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0),
            EndTime=datetime.now(timezone.utc).replace(microsecond=0),
            Period=period,
            Statistics=statistics,
        )
    except Exception:
        return {}
    datapoints = response.get("Datapoints", [])
    datapoints.sort(key=lambda item: item.get("Timestamp", datetime.min.replace(tzinfo=timezone.utc)))
    return datapoints[-1] if datapoints else {}


def collect_live_performance_metrics(snapshot, cloudwatch_client):
    metrics = {"lambda": {}, "ecs": {}, "sqs": {}, "api_gateway": {}, "rds": {}, "dynamodb": {}, "alb": {}}

    for fn in snapshot.get("lambda_functions", []):
        name = fn.get("FunctionName", "")
        dimensions = [{"Name": "FunctionName", "Value": name}]
        metrics["lambda"][name] = {
            "duration": _last_datapoint(cloudwatch_client, "AWS/Lambda", "Duration", dimensions, ["Average", "Maximum"], hours=24, period=300),
            "errors": _last_datapoint(cloudwatch_client, "AWS/Lambda", "Errors", dimensions, ["Sum"], hours=24, period=300),
            "throttles": _last_datapoint(cloudwatch_client, "AWS/Lambda", "Throttles", dimensions, ["Sum"], hours=24, period=300),
            "invocations": _last_datapoint(cloudwatch_client, "AWS/Lambda", "Invocations", dimensions, ["Sum"], hours=24, period=300),
        }

    for service in snapshot.get("ecs", {}).get("services", []):
        cluster_name = str(service.get("clusterArn", "")).split("/")[-1]
        service_name = service.get("serviceName", "")
        dimensions = [
            {"Name": "ClusterName", "Value": cluster_name},
            {"Name": "ServiceName", "Value": service_name},
        ]
        metrics["ecs"][service_name] = {
            "cpu": _last_datapoint(cloudwatch_client, "AWS/ECS", "CPUUtilization", dimensions, ["Average", "Maximum"], hours=24, period=300),
            "memory": _last_datapoint(cloudwatch_client, "AWS/ECS", "MemoryUtilization", dimensions, ["Average", "Maximum"], hours=24, period=300),
        }

    for queue in snapshot.get("sqs_queues", []):
        queue_name = queue.get("QueueName", "")
        dimensions = [{"Name": "QueueName", "Value": queue_name}]
        metrics["sqs"][queue_name] = {
            "age_of_oldest": _last_datapoint(cloudwatch_client, "AWS/SQS", "ApproximateAgeOfOldestMessage", dimensions, ["Maximum"], hours=24, period=300),
            "messages_visible": _last_datapoint(cloudwatch_client, "AWS/SQS", "ApproximateNumberOfMessagesVisible", dimensions, ["Maximum"], hours=24, period=300),
        }

    for api in snapshot.get("api_gateways", []):
        api_name = api.get("name", "")
        for stage in api.get("stages", []):
            stage_name = stage.get("stageName")
            dimensions = [
                {"Name": "ApiName", "Value": api_name},
                {"Name": "Stage", "Value": stage_name},
            ]
            metrics["api_gateway"][f"{api_name}:{stage_name}"] = {
                "latency": _last_datapoint(cloudwatch_client, "AWS/ApiGateway", "Latency", dimensions, ["Average", "Maximum"], hours=24, period=300),
                "integration_latency": _last_datapoint(cloudwatch_client, "AWS/ApiGateway", "IntegrationLatency", dimensions, ["Average", "Maximum"], hours=24, period=300),
                "server_errors": _last_datapoint(cloudwatch_client, "AWS/ApiGateway", "5XXError", dimensions, ["Sum"], hours=24, period=300),
            }

    for db in snapshot.get("rds", {}).get("instances", []):
        identifier = db.get("DBInstanceIdentifier", "")
        dimensions = [{"Name": "DBInstanceIdentifier", "Value": identifier}]
        metrics["rds"][identifier] = {
            "cpu": _last_datapoint(cloudwatch_client, "AWS/RDS", "CPUUtilization", dimensions, ["Average", "Maximum"], hours=24, period=300),
            "connections": _last_datapoint(cloudwatch_client, "AWS/RDS", "DatabaseConnections", dimensions, ["Average", "Maximum"], hours=24, period=300),
            "read_latency": _last_datapoint(cloudwatch_client, "AWS/RDS", "ReadLatency", dimensions, ["Average", "Maximum"], hours=24, period=300),
            "write_latency": _last_datapoint(cloudwatch_client, "AWS/RDS", "WriteLatency", dimensions, ["Average", "Maximum"], hours=24, period=300),
            "freeable_memory": _last_datapoint(cloudwatch_client, "AWS/RDS", "FreeableMemory", dimensions, ["Average", "Minimum"], hours=24, period=300),
        }

    for table in snapshot.get("dynamodb_tables", []):
        table_name = table.get("Table", {}).get("TableName", "")
        dimensions = [{"Name": "TableName", "Value": table_name}]
        metrics["dynamodb"][table_name] = {
            "throttled_requests": _last_datapoint(cloudwatch_client, "AWS/DynamoDB", "ThrottledRequests", dimensions, ["Sum"], hours=24, period=300),
            "successful_request_latency": _last_datapoint(cloudwatch_client, "AWS/DynamoDB", "SuccessfulRequestLatency", dimensions, ["Average", "Maximum"], hours=24, period=300),
            "consumed_read": _last_datapoint(cloudwatch_client, "AWS/DynamoDB", "ConsumedReadCapacityUnits", dimensions, ["Sum", "Maximum"], hours=24, period=300),
            "consumed_write": _last_datapoint(cloudwatch_client, "AWS/DynamoDB", "ConsumedWriteCapacityUnits", dimensions, ["Sum", "Maximum"], hours=24, period=300),
        }

    for lb in snapshot.get("load_balancers", []):
        lb_arn = lb.get("LoadBalancerArn", "")
        lb_name = lb.get("LoadBalancerName", "")
        if not lb_arn:
            continue
        arn_suffix = lb_arn.split("loadbalancer/")[-1]
        dimensions = [{"Name": "LoadBalancer", "Value": arn_suffix}]
        metrics["alb"][lb_name or arn_suffix] = {
            "target_response_time": _last_datapoint(cloudwatch_client, "AWS/ApplicationELB", "TargetResponseTime", dimensions, ["Average", "Maximum"], hours=24, period=300),
            "http_5xx": _last_datapoint(cloudwatch_client, "AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", dimensions, ["Sum"], hours=24, period=300),
            "target_5xx": _last_datapoint(cloudwatch_client, "AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", dimensions, ["Sum"], hours=24, period=300),
        }

    return metrics


def _build_root_cause_summary(findings):
    if not findings:
        return "No clear performance bottlenecks were detected in the current snapshot."

    top = findings[:3]
    summary_parts = []
    for item in top:
        category = item.get("category", "")
        if category == "lambda-trigger-failure":
            summary_parts.append("an event-driven Lambda consumer is broken")
        elif category == "ecs-unavailable":
            summary_parts.append("an ECS service cannot start healthy tasks")
        elif category == "queue-latency":
            summary_parts.append("an SQS backlog is delaying async work")
        elif category == "api-gateway-latency":
            summary_parts.append("an API Gateway integration is slow")
        elif category == "ecs-saturation":
            summary_parts.append("an ECS service is resource-saturated")
        elif category == "lambda-near-timeout":
            summary_parts.append("a Lambda function is close to timing out")
        elif category == "dynamodb-throttling":
            summary_parts.append("a DynamoDB table is throttling")
        elif category == "rds-latency":
            summary_parts.append("an RDS database shows elevated latency")
        else:
            summary_parts.append(item.get("title", "").lower())
    return "Most likely slowdown drivers: " + "; ".join(summary_parts[:3]) + "."


def _dependency_chain_for_resource(resource_id, dependency_graph):
    if not resource_id or not dependency_graph:
        return []
    edges = dependency_graph.get("edges", [])
    nodes = {item.get("id"): item for item in dependency_graph.get("nodes", [])}
    outgoing_by_source = {}
    incoming_by_target = {}
    for edge in edges:
        outgoing_by_source.setdefault(edge.get("from"), []).append(edge)
        incoming_by_target.setdefault(edge.get("to"), []).append(edge)

    edge_priority = {
        "feeds-mapping": 100,
        "invokes-function": 95,
        "api-integration": 90,
        "schedule-target": 88,
        "schedule-task-definition": 87,
        "uses-task-definition": 85,
        "reads-from": 80,
        "event-source-mapping": 78,
        "env:": 70,
        "secret:": 68,
        "runs-in-cluster": 40,
        "uses-subnet": 20,
        "uses-security-group": 15,
        "assumes-role": 5,
        "execution-role": 4,
    }

    def edge_score(edge):
        relationship = edge.get("relationship", "")
        for prefix, score in edge_priority.items():
            if relationship.startswith(prefix):
                return score
        return 0

    def render_node(node_id):
        node = nodes.get(node_id, {})
        return {
            "id": node_id,
            "type": node.get("type", ""),
            "name": node.get("name", node_id),
        }

    chain_ids = []
    seen = set()

    incoming = sorted(incoming_by_target.get(resource_id, []), key=edge_score, reverse=True)
    if incoming and edge_score(incoming[0]) >= 80:
        start = incoming[0].get("from")
        if start and start not in seen:
            chain_ids.append(start)
            seen.add(start)

    current = resource_id
    for _ in range(5):
        if not current or current in seen:
            break
        chain_ids.append(current)
        seen.add(current)
        candidates = [edge for edge in outgoing_by_source.get(current, []) if edge.get("to") not in seen]
        if not candidates:
            break
        candidates.sort(key=edge_score, reverse=True)
        if edge_score(candidates[0]) <= 0:
            break
        current = candidates[0].get("to")

    return [render_node(node_id) for node_id in chain_ids]


def _incident_group_key(item, dependency_chain):
    category = item.get("category", "")
    resource_id = item.get("resource_id", "")
    chain = dependency_chain or []

    if category in {"ecs-unavailable", "missing-ecs-utilization", "ecs-network-review", "ecs-saturation"}:
        for node in chain:
            if node.get("type") == "ecs-task-definition":
                return (category, "ecs-task-definition", node.get("id"))
        return (category, "resource", resource_id)

    if category in {"lambda-trigger-failure", "disabled-processing-path"}:
        lambda_node = None
        source_node = None
        for node in chain:
            if node.get("type") == "lambda":
                lambda_node = node.get("id")
            if node.get("type") in {"sqs", "dynamodb"} or "stream/" in str(node.get("id", "")):
                source_node = node.get("id")
        if lambda_node and source_node:
            return (category, "lambda-path", lambda_node, source_node)
        if lambda_node:
            return (category, "lambda", lambda_node)
        mapping_prefix = str(resource_id).rsplit(":", 1)[0]
        if mapping_prefix:
            return (category, "mapping-prefix", mapping_prefix)
        return (category, "resource", resource_id)

    return (category, "resource", resource_id)


def _severity_rank(value):
    return SEVERITY_ORDER.get(value, 0)


def _business_impact(category, dependency_chain, finding_count):
    chain = dependency_chain or []
    lambda_name = next((node.get("name") for node in chain if node.get("type") == "lambda"), "")
    source_name = next((node.get("name") for node in chain if node.get("type") in {"sqs", "dynamodb"} or "stream/" in str(node.get("id", ""))), "")
    ecs_service = next((node.get("name") for node in chain if node.get("type") == "ecs-service"), "")

    if category == "lambda-trigger-failure":
        if source_name and lambda_name:
            return f"Events from {source_name} are likely not being processed by {lambda_name}, so async workflows can stall or fall behind."
        return "An event-driven workflow is broken, so background processing may stall or silently fail."
    if category == "disabled-processing-path":
        if source_name and lambda_name:
            return f"Work from {source_name} is currently paused before it reaches {lambda_name}, which can delay async jobs or retries."
        return "An async processing path is paused, which can delay queues, retries, or downstream updates."
    if category == "ecs-unavailable":
        if finding_count > 1:
            return "Multiple ECS services in the same deployment path are unavailable, so client-facing features or internal jobs on that path may be down."
        if ecs_service:
            return f"{ecs_service} has no healthy capacity, so requests or jobs routed to it may fail or time out."
        return "An ECS service has no healthy capacity, so requests on that path may fail or time out."
    if category == "missing-ecs-utilization":
        return "The service can still be running, but there is not enough telemetry to tell whether it is under-sized, over-sized, or intermittently saturated."
    if category == "ecs-saturation":
        return "The service is near its resource ceiling, so latency can spike and scaling pressure can increase under load."
    if category == "queue-latency":
        return "Queued work is waiting too long, so background actions, notifications, or downstream processing may lag behind user actions."
    if category == "api-gateway-latency":
        return "API responses are slow enough to be visible to end users and may cause retries or timeout-related failures."
    if category == "dynamodb-throttling":
        return "Database throttling can slow reads and writes, which often surfaces as API latency or failed async retries."
    if category == "rds-cpu-saturation":
        return "The database is under CPU pressure, which can slow application requests and increase timeout risk."
    return "This incident can reduce throughput or increase latency for the affected workflow."


def _build_grouped_incidents(findings, dependency_graph=None):
    grouped = {}
    order = []

    for item in findings:
        dependency_chain = _dependency_chain_for_resource(item.get("resource_id"), dependency_graph)
        group_key = _incident_group_key(item, dependency_chain)
        if group_key not in grouped:
            grouped[group_key] = {
                "severity": item.get("severity"),
                "title": item.get("title"),
                "category": item.get("category"),
                "resource_id": item.get("resource_id"),
                "probable_cause": item.get("probable_cause"),
                "recommended_action": item.get("recommended_action"),
                "dependency_chain": dependency_chain,
                "affected_resources": [],
                "finding_count": 0,
            }
            order.append(group_key)

        incident = grouped[group_key]
        incident["finding_count"] += 1
        incident["affected_resources"].append(item.get("resource_id"))

        if _severity_rank(item.get("severity")) > _severity_rank(incident.get("severity")):
            incident["severity"] = item.get("severity")
        if len(dependency_chain) > len(incident.get("dependency_chain", [])):
            incident["dependency_chain"] = dependency_chain

    incidents = []
    for key in order:
        incident = grouped[key]
        incident["affected_resources"] = list(dict.fromkeys(resource for resource in incident["affected_resources"] if resource))
        if incident["finding_count"] > 1:
            incident["title"] = f"{incident['title']} ({incident['finding_count']} related findings)"
        incident["business_impact"] = _business_impact(
            incident.get("category"),
            incident.get("dependency_chain", []),
            incident.get("finding_count", 0),
        )
        incidents.append(incident)

    incidents.sort(key=lambda item: (_severity_rank(item.get("severity")), item.get("finding_count", 0)), reverse=True)
    return incidents


def build_why_is_it_slow_report(performance_report, dependency_graph=None):
    findings = performance_report.get("findings", [])
    incidents = _build_grouped_incidents(findings, dependency_graph=dependency_graph)[:5]
    return {
        "generated_at": performance_report.get("generated_at"),
        "source_env": performance_report.get("source_env", ""),
        "region": performance_report.get("region", ""),
        "account_id": performance_report.get("account_id", ""),
        "headline": performance_report.get("root_cause_summary", ""),
        "finding_count": performance_report.get("summary", {}).get("finding_count", 0),
        "incident_count": len(incidents),
        "top_incidents": incidents,
    }


def build_why_is_it_slow_markdown(report):
    lines = [
        "# Why Is It Slow",
        "",
        f"Generated: {report.get('generated_at', '')}",
        "",
        "## Headline",
        "",
        report.get("headline", ""),
        "",
        "## Top Incidents",
        "",
    ]
    incidents = report.get("top_incidents", [])
    if not incidents:
        lines.append("- No clear slowdown incidents were detected.")
        return "\n".join(lines) + "\n"
    for item in incidents:
        lines.append(f"- [{item.get('severity')}] {item.get('title')}")
        lines.append(f"  Resource: {item.get('resource_id')}")
        if item.get("finding_count", 0) > 1:
            lines.append(f"  Impact: {item.get('finding_count')} related findings across {len(item.get('affected_resources', []))} resources")
        lines.append(f"  Business impact: {item.get('business_impact')}")
        lines.append(f"  Probable cause: {item.get('probable_cause')}")
        lines.append(f"  Action: {item.get('recommended_action')}")
        chain = item.get("dependency_chain", [])
        if chain:
            rendered = " -> ".join(node.get("name", node.get("id", "")) for node in chain)
            lines.append(f"  Dependency chain: {rendered}")
    return "\n".join(lines) + "\n"


def analyze_performance(snapshot, live_metrics=None):
    findings = []
    live_metrics = live_metrics or {}

    lambda_functions_by_arn = {item.get("FunctionArn"): item for item in snapshot.get("lambda_functions", [])}
    for mapping in snapshot.get("lambda_event_source_mappings", []):
        result = str(mapping.get("LastProcessingResult", ""))
        if "PROBLEM:" in result:
            function_name = lambda_functions_by_arn.get(mapping.get("FunctionArn"), {}).get("FunctionName", mapping.get("FunctionArn", ""))
            _add_finding(
                findings,
                "high",
                "lambda-trigger-failure",
                "Lambda event source is failing to process records",
                mapping.get("EventSourceMappingArn", ""),
                evidence=result,
                probable_cause=f"{function_name} cannot successfully consume from its event source.",
                recommended_action="Fix the execution role or permissions, then re-enable healthy processing and drain backlog.",
            )
        if mapping.get("State") == "Disabled":
            _add_finding(
                findings,
                "medium",
                "disabled-processing-path",
                "A disabled event source mapping may leave async work unprocessed",
                mapping.get("EventSourceMappingArn", ""),
                evidence=f"State=Disabled for source {mapping.get('EventSourceArn', '')}",
                probable_cause="Messages or stream records may not be flowing into the consumer path.",
                recommended_action="Confirm whether this path is intentionally paused. If not, restore the mapping after validating the consumer.",
            )

    for queue in snapshot.get("sqs_queues", []):
        attrs = queue.get("Attributes", {})
        visible = int(attrs.get("ApproximateNumberOfMessages", "0") or "0")
        not_visible = int(attrs.get("ApproximateNumberOfMessagesNotVisible", "0") or "0")
        if visible > 100 or not_visible > 100:
            _add_finding(
                findings,
                "high" if visible > 1000 else "medium",
                "queue-backlog",
                "SQS queue has a backlog that may indicate slow consumers",
                attrs.get("QueueArn", queue.get("QueueUrl", "")),
                evidence=f"Approximate visible={visible}, not visible={not_visible}",
                probable_cause="Consumers may be under-scaled, failing, or blocked on downstream dependencies.",
                recommended_action="Check consumer errors, throughput, concurrency, and downstream latency.",
            )
        queue_metrics = (live_metrics.get("sqs", {}) or {}).get(queue.get("QueueName", ""), {})
        oldest_age = float(queue_metrics.get("age_of_oldest", {}).get("Maximum", 0) or 0)
        if oldest_age >= 300:
            _add_finding(
                findings,
                "high" if oldest_age >= 1800 else "medium",
                "queue-latency",
                "SQS queue shows old messages waiting too long",
                attrs.get("QueueArn", queue.get("QueueUrl", "")),
                evidence=f"ApproximateAgeOfOldestMessage={round(oldest_age, 1)}s",
                probable_cause="Consumers are not keeping up, which can surface as end-user latency or delayed background work.",
                recommended_action="Increase consumer throughput, inspect failures, and confirm downstream systems are healthy.",
            )

    task_definitions = {item.get("taskDefinitionArn"): item for item in snapshot.get("ecs", {}).get("task_definitions", [])}
    for service in snapshot.get("ecs", {}).get("services", []):
        name = service.get("serviceName", "")
        desired = int(service.get("desiredCount", 0) or 0)
        running = int(service.get("runningCount", 0) or 0)
        failure_events = [
            str(event.get("message", ""))
            for event in service.get("events", [])
            if "unable to place a task" in str(event.get("message", "")).lower()
            or "failed to start" in str(event.get("message", "")).lower()
            or "cannotpullcontainererror" in str(event.get("message", "")).lower()
        ]
        if desired > 0 and running == 0 and failure_events:
            _add_finding(
                findings,
                "high",
                "ecs-unavailable",
                "ECS service has desired tasks but no healthy running capacity",
                service.get("serviceArn", ""),
                evidence=f"desiredCount={desired}, runningCount={running}, latest failure={failure_events[0]}",
                probable_cause=f"{name} cannot start healthy tasks, so the service path is unavailable or degraded.",
                recommended_action="Fix image pull, task definition, IAM, networking, or dependency startup issues before scaling traffic back to the service.",
            )

        network = service.get("networkConfiguration", {}).get("awsvpcConfiguration", {})
        if desired > 0 and network.get("assignPublicIp") == "ENABLED" and len(network.get("subnets", [])) >= 3:
            _add_finding(
                findings,
                "low",
                "ecs-network-review",
                "ECS service uses public IP networking and broad subnet spread",
                service.get("serviceArn", ""),
                evidence=f"assignPublicIp={network.get('assignPublicIp')}, subnets={len(network.get('subnets', []))}",
                probable_cause="Not necessarily a bottleneck, but network placement can complicate latency and startup behavior.",
                recommended_action="Review whether the service should run privately behind load balancing and whether subnet selection is intentional.",
            )

        task_definition = task_definitions.get(service.get("taskDefinition"), {})
        cpu = int(task_definition.get("cpu", "0") or "0")
        memory = int(task_definition.get("memory", "0") or "0")
        metrics = next(
            (
                item for item in snapshot.get("ecs_metrics", {}).get("services", [])
                if item.get("serviceName") == name
            ),
            {},
        )
        if not metrics.get("cpu") and not metrics.get("memory") and desired > 0:
            _add_finding(
                findings,
                "medium",
                "missing-ecs-utilization",
                "ECS service has no recent utilization datapoints in the snapshot",
                service.get("serviceArn", ""),
                evidence=f"task cpu={cpu}, memory={memory}, desiredCount={desired}",
                probable_cause="Without CPU or memory history, rightsizing and bottleneck analysis are blind.",
                recommended_action="Collect CloudWatch CPU and memory utilization history for this service to confirm whether it is starved or overprovisioned.",
            )
        live_service_metrics = (live_metrics.get("ecs", {}) or {}).get(name, {})
        live_cpu = float(live_service_metrics.get("cpu", {}).get("Maximum", 0) or 0)
        live_memory = float(live_service_metrics.get("memory", {}).get("Maximum", 0) or 0)
        if live_cpu >= 85 or live_memory >= 85:
            saturated = "CPU" if live_cpu >= live_memory else "memory"
            _add_finding(
                findings,
                "high" if max(live_cpu, live_memory) >= 95 else "medium",
                "ecs-saturation",
                "ECS service shows high utilization saturation",
                service.get("serviceArn", ""),
                evidence=f"cpu_max={round(live_cpu, 1)}%, memory_max={round(live_memory, 1)}%",
                probable_cause=f"{name} may be resource-constrained on {saturated}, which can increase latency or cause instability under load.",
                recommended_action="Scale the service, resize the task definition, or reduce expensive in-request work.",
            )

    for fn in snapshot.get("lambda_functions", []):
        memory = int(fn.get("MemorySize", 0) or 0)
        timeout = int(fn.get("Timeout", 0) or 0)
        name = fn.get("FunctionName", "")
        if timeout <= 3 and memory <= 128:
            _add_finding(
                findings,
                "low",
                "lambda-tight-config",
                "Lambda has a very small timeout and memory profile",
                fn.get("FunctionArn", ""),
                evidence=f"timeout={timeout}s, memory={memory}MB",
                probable_cause=f"{name} may be prone to latency spikes or timeouts if workload grows.",
                recommended_action="Review invocation duration percentiles and increase timeout or memory if the function is near its limits.",
            )
        lambda_metrics = (live_metrics.get("lambda", {}) or {}).get(name, {})
        max_duration = float(lambda_metrics.get("duration", {}).get("Maximum", 0) or 0)
        error_sum = float(lambda_metrics.get("errors", {}).get("Sum", 0) or 0)
        throttle_sum = float(lambda_metrics.get("throttles", {}).get("Sum", 0) or 0)
        invocation_sum = float(lambda_metrics.get("invocations", {}).get("Sum", 0) or 0)
        if max_duration >= timeout * 1000 * 0.8 and invocation_sum > 0:
            _add_finding(
                findings,
                "high" if max_duration >= timeout * 1000 * 0.95 else "medium",
                "lambda-near-timeout",
                "Lambda duration is close to its configured timeout",
                fn.get("FunctionArn", ""),
                evidence=f"max_duration_ms={round(max_duration, 1)}, timeout_s={timeout}, invocations={int(invocation_sum)}",
                probable_cause=f"{name} is spending too long per invocation and risks timeouts under load.",
                recommended_action="Profile the function, reduce downstream latency, or raise timeout and memory after measurement.",
            )
        if throttle_sum > 0:
            _add_finding(
                findings,
                "high",
                "lambda-throttling",
                "Lambda is experiencing throttling",
                fn.get("FunctionArn", ""),
                evidence=f"throttles={int(throttle_sum)}, invocations={int(invocation_sum)}",
                probable_cause=f"{name} may be concurrency-limited, causing delayed processing or failed requests.",
                recommended_action="Review reserved concurrency, upstream burst patterns, and async retry behavior.",
            )
        if error_sum > 0 and invocation_sum > 0:
            _add_finding(
                findings,
                "medium",
                "lambda-errors",
                "Lambda is returning errors in the recent window",
                fn.get("FunctionArn", ""),
                evidence=f"errors={int(error_sum)}, invocations={int(invocation_sum)}",
                probable_cause=f"{name} may be failing due to code, dependency, or downstream issues that also affect latency.",
                recommended_action="Inspect logs and traces for failing invocations around the peak error period.",
            )

    for table in snapshot.get("dynamodb_tables", []):
        table_data = table.get("Table", {})
        mode = table_data.get("BillingModeSummary", {}).get("BillingMode", "")
        item_count = int(table_data.get("ItemCount", 0) or 0)
        table_name = table_data.get("TableName", "")
        if mode == "PROVISIONED":
            throughput = table_data.get("ProvisionedThroughput", {})
            _add_finding(
                findings,
                "medium",
                "dynamodb-capacity-review",
                "DynamoDB table uses provisioned capacity and may need throughput review",
                table_data.get("TableArn", table_data.get("TableName", "")),
                evidence=f"itemCount={item_count}, read={throughput.get('ReadCapacityUnits')}, write={throughput.get('WriteCapacityUnits')}",
                probable_cause="Provisioned tables can throttle or overpay if capacity does not match actual traffic.",
                recommended_action="Compare consumed capacity and throttling metrics to the provisioned settings.",
            )
        table_metrics = (live_metrics.get("dynamodb", {}) or {}).get(table_name, {})
        throttled = float(table_metrics.get("throttled_requests", {}).get("Sum", 0) or 0)
        latency = float(table_metrics.get("successful_request_latency", {}).get("Maximum", 0) or 0)
        if throttled > 0:
            _add_finding(
                findings,
                "high",
                "dynamodb-throttling",
                "DynamoDB table is throttling requests",
                table_data.get("TableArn", table_name),
                evidence=f"throttled_requests={int(throttled)}, latency_max_ms={round(latency, 2)}",
                probable_cause="The table cannot serve the current request rate efficiently, which will surface as slow or failed operations.",
                recommended_action="Increase capacity, switch to on-demand, reduce hot partitions, or smooth bursty access patterns.",
            )
        elif latency >= 50:
            _add_finding(
                findings,
                "medium",
                "dynamodb-latency",
                "DynamoDB table shows elevated request latency",
                table_data.get("TableArn", table_name),
                evidence=f"successful_request_latency_max_ms={round(latency, 2)}",
                probable_cause="Request latency is elevated and may contribute to slow end-user or background operations.",
                recommended_action="Inspect partition key distribution, retry behavior, and downstream call chains around this table.",
            )

    for db in snapshot.get("rds", {}).get("instances", []):
        identifier = db.get("DBInstanceIdentifier", "")
        db_metrics = (live_metrics.get("rds", {}) or {}).get(identifier, {})
        cpu = float(db_metrics.get("cpu", {}).get("Maximum", 0) or 0)
        connections = float(db_metrics.get("connections", {}).get("Maximum", 0) or 0)
        read_latency = float(db_metrics.get("read_latency", {}).get("Maximum", 0) or 0)
        write_latency = float(db_metrics.get("write_latency", {}).get("Maximum", 0) or 0)
        if cpu >= 85:
            _add_finding(
                findings,
                "high" if cpu >= 95 else "medium",
                "rds-cpu-saturation",
                "RDS instance shows high CPU utilization",
                identifier,
                evidence=f"cpu_max={round(cpu, 1)}%, connections_max={round(connections, 1)}",
                probable_cause="Database CPU pressure can increase query latency and slow every dependent service.",
                recommended_action="Inspect expensive queries, add indexes, scale the instance, or reduce synchronous DB work.",
            )
        if read_latency >= 0.05 or write_latency >= 0.05:
            _add_finding(
                findings,
                "high" if max(read_latency, write_latency) >= 0.2 else "medium",
                "rds-latency",
                "RDS instance shows elevated read/write latency",
                identifier,
                evidence=f"read_latency_s={round(read_latency, 4)}, write_latency_s={round(write_latency, 4)}",
                probable_cause="Database IO or query latency is high, which can directly slow APIs and background jobs.",
                recommended_action="Inspect query performance, storage pressure, and connection behavior before scaling or tuning the instance.",
            )

    for api in snapshot.get("api_gateways", []):
        api_name = api.get("name", "")
        for stage in api.get("stages", []):
            key = f"{api_name}:{stage.get('stageName')}"
            api_metrics = (live_metrics.get("api_gateway", {}) or {}).get(key, {})
            latency = float(api_metrics.get("latency", {}).get("Maximum", 0) or 0)
            integration = float(api_metrics.get("integration_latency", {}).get("Maximum", 0) or 0)
            server_errors = float(api_metrics.get("server_errors", {}).get("Sum", 0) or 0)
            if latency >= 2000 or integration >= 1500:
                _add_finding(
                    findings,
                    "high" if latency >= 5000 or integration >= 3000 else "medium",
                    "api-gateway-latency",
                    "API Gateway stage shows elevated latency",
                    key,
                    evidence=f"latency_max_ms={round(latency, 1)}, integration_latency_max_ms={round(integration, 1)}, 5xx={int(server_errors)}",
                    probable_cause="The backend integration is slow or erroring, increasing end-user response time.",
                    recommended_action="Inspect integration dependencies, Lambda or service duration, and 5xx spikes for this stage.",
                )

    for lb in snapshot.get("load_balancers", []):
        name = lb.get("LoadBalancerName", "")
        lb_metrics = (live_metrics.get("alb", {}) or {}).get(name, {})
        response_time = float(lb_metrics.get("target_response_time", {}).get("Maximum", 0) or 0)
        elb_5xx = float(lb_metrics.get("http_5xx", {}).get("Sum", 0) or 0)
        target_5xx = float(lb_metrics.get("target_5xx", {}).get("Sum", 0) or 0)
        if response_time >= 1.5 or elb_5xx > 0 or target_5xx > 0:
            _add_finding(
                findings,
                "high" if response_time >= 3 or target_5xx > 0 else "medium",
                "alb-latency",
                "Application Load Balancer shows slow or failing target responses",
                lb.get("LoadBalancerArn", name),
                evidence=f"target_response_time_s={round(response_time, 3)}, elb_5xx={int(elb_5xx)}, target_5xx={int(target_5xx)}",
                probable_cause="Targets behind the load balancer are slow or erroring, which directly affects request latency.",
                recommended_action="Inspect target health, backend response times, and recent deploys behind this load balancer.",
            )

    findings.sort(key=lambda item: SEVERITY_ORDER.get(item["severity"], 0), reverse=True)
    summary = {
        "finding_count": len(findings),
        "high": sum(1 for item in findings if item["severity"] == "high"),
        "medium": sum(1 for item in findings if item["severity"] == "medium"),
        "low": sum(1 for item in findings if item["severity"] == "low"),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_env": snapshot.get("source_env", ""),
        "region": snapshot.get("region", ""),
        "account_id": snapshot.get("account_id", ""),
        "live_metrics_enabled": bool(live_metrics),
        "root_cause_summary": _build_root_cause_summary(findings),
        "summary": summary,
        "findings": findings,
    }


def build_client_performance_markdown(report):
    lines = [
        "# AWS Performance Report",
        "",
        f"Generated: {report.get('generated_at', '')}",
        "",
        "## Summary",
        "",
        f"Findings: {report.get('summary', {}).get('finding_count', 0)}",
        report.get("root_cause_summary", ""),
        "",
        "## Performance Findings",
        "",
    ]
    findings = report.get("findings", [])
    if not findings:
        lines.append("- No clear performance bottlenecks were detected in the current snapshot.")
    else:
        for item in findings[:10]:
            lines.append(f"- [{item.get('severity')}] {item.get('title')}")
            lines.append(f"  Resource: {item.get('resource_id')}")
            lines.append(f"  Evidence: {item.get('evidence')}")
            lines.append(f"  Probable cause: {item.get('probable_cause')}")
            lines.append(f"  Action: {item.get('recommended_action')}")
    return "\n".join(lines) + "\n"


def main():
    args = parse_args()
    source_env = args.source_env or "full-account-scan"
    config = load_transfer_config(args.config)
    client_slug = resolve_client_slug(args.client_slug, config, source_env=source_env)
    inventory_dir = _resolve_inventory_dir(source_env, args.region or "us-east-1", client_slug)
    snapshot = json.loads((inventory_dir / "source_snapshot.json").read_text(encoding="utf-8"))
    if not snapshot.get("source_env"):
        snapshot["source_env"] = source_env
    dependency_graph = snapshot.get("dependency_graph")
    if not dependency_graph:
        graph_path = inventory_dir / "dependency_graph.json"
        if graph_path.exists():
            dependency_graph = json.loads(graph_path.read_text(encoding="utf-8"))
    live_metrics = {}
    live_metrics_error = ""
    if args.live_metrics:
        source_external_id = args.source_external_id or config_override(config, "source_external_id", "")
        try:
            session = session_for(args.region, args.source_role_arn, external_id=source_external_id)
            cloudwatch_client = session.client("cloudwatch")
            live_metrics = collect_live_performance_metrics(snapshot, cloudwatch_client)
        except (NoCredentialsError, ClientError, Exception) as exc:
            live_metrics_error = str(exc)
    report = analyze_performance(snapshot, live_metrics=live_metrics)
    if live_metrics_error:
        report["live_metrics_error"] = live_metrics_error
    why_report = build_why_is_it_slow_report(report, dependency_graph=dependency_graph)
    json_path = inventory_dir / "performance_report.json"
    md_path = inventory_dir / "performance_report.md"
    why_json_path = inventory_dir / "why_is_it_slow_report.json"
    why_md_path = inventory_dir / "why_is_it_slow_report.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(build_client_performance_markdown(report), encoding="utf-8")
    why_json_path.write_text(json.dumps(why_report, indent=2), encoding="utf-8")
    why_md_path.write_text(build_why_is_it_slow_markdown(why_report), encoding="utf-8")
    append_audit_event(
        "analyze_performance_issues",
        "ok",
        {
            "report_path": str(json_path),
            "markdown_path": str(md_path),
            "why_report_path": str(why_json_path),
            "why_markdown_path": str(why_md_path),
            "finding_count": report["summary"]["finding_count"],
            "live_metrics_enabled": bool(args.live_metrics),
            "live_metrics_error": live_metrics_error,
        },
        source_env=source_env,
        client_slug=client_slug,
    )
    print(json.dumps({
        "status": "ok",
        "report_path": str(json_path),
        "markdown_path": str(md_path),
        "why_report_path": str(why_json_path),
        "why_markdown_path": str(why_md_path),
        "finding_count": report["summary"]["finding_count"],
        "live_metrics_enabled": bool(args.live_metrics),
        "live_metrics_error": live_metrics_error,
    }, indent=2))


if __name__ == "__main__":
    main()
