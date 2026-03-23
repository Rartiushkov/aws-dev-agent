import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from botocore.exceptions import ClientError

from executor.scripts.transfer_common import config_override, deployment_dir_name, deployment_dir_path, load_transfer_config, resolve_client_slug, session_for
from executor.scripts.agent_memory import record_incident, suggest_known_fixes
from executor.scripts.audit_log import append_audit_event


def sanitize_name(value):
    return value.strip().lower().replace(" ", "-")


def parse_args():
    parser = argparse.ArgumentParser(description="Validate a deployed cloned environment.")
    parser.add_argument("--target-env", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--config", default="")
    parser.add_argument("--source-role-arn", default="")
    parser.add_argument("--target-role-arn", default="")
    parser.add_argument("--source-external-id", default="")
    parser.add_argument("--target-external-id", default="")
    parser.add_argument("--deployment-key", default="")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def load_manifest(target_env, deployment_key="", client_slug=""):
    manifest_path = deployment_dir_path(target_env, deployment_key, client_slug) / "deployment_manifest.json"
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
        if "ResourceNotFoundException" in str(exc):
            return []
        return [f"log-check-failed: {exc}"]


def remember_validation_issues(report, target_env, client_slug=""):
    for item in report.get("functions", []):
        for issue in item.get("issues", []):
            record_incident(
                "validation-function-issue",
                issue,
                target_env=target_env,
                client_slug=client_slug,
                tags=["validation", "lambda", item.get("function_name", "")],
                details={"function_name": item.get("function_name", "")},
            )
    for check in report.get("smoke_checks", []):
        if check.get("status") == "ok":
            continue
        for issue in check.get("issues", []):
            summary = issue.get("error") or issue.get("details") or json.dumps(issue, default=str)
            detail_payload = {
                key: value
                for key, value in issue.items()
                if key not in {"error"}
            }
            record_incident(
                "validation-smoke-check-issue",
                f"{check.get('name')}: {summary}",
                target_env=target_env,
                client_slug=client_slug,
                tags=["validation", check.get("name", "")],
                details=detail_payload,
            )


def attach_known_fixes_to_checks(checks, client_slug=""):
    for check in checks:
        if check.get("status") == "ok":
            continue
        query_parts = [check.get("name", "")]
        if check.get("details"):
            query_parts.append(str(check.get("details")))
        for issue in check.get("issues", []):
            if isinstance(issue, dict):
                query_parts.append(issue.get("error", ""))
                for key in ("function", "service", "resource_type", "project", "bucket", "api"):
                    if issue.get(key):
                        query_parts.append(str(issue.get(key)))
            else:
                query_parts.append(str(issue))
        suggestions = suggest_known_fixes(" ".join(part for part in query_parts if part), limit=3, client_slug=client_slug)
        if suggestions:
            check["known_fixes"] = suggestions
    return checks


def scan_table_count(table):
    count = 0
    scan_kwargs = {"Select": "COUNT"}
    while True:
        response = table.scan(**scan_kwargs)
        count += response.get("Count", 0)
        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    return count


def expected_mapping_count(manifest):
    return len(manifest.get("lambda_event_source_mappings", []))


def expected_mapping_enabled_states(source_snapshot, manifest):
    expected = {}
    source_mappings = {item.get("UUID"): item for item in source_snapshot.get("lambda_event_source_mappings", [])}
    for item in manifest.get("lambda_event_source_mappings", []):
        source_uuid = item.get("source_uuid")
        source_mapping = source_mappings.get(source_uuid, {})
        target_function = item.get("target_function")
        target_event_source_arn = item.get("target_event_source_arn")
        if target_function and target_event_source_arn:
            expected[(target_function, target_event_source_arn)] = source_mapping.get("State", "Enabled") != "Disabled"
    return expected


def expected_ecs_service_health(source_snapshot, manifest):
    expected = {}
    source_services = {item.get("serviceName"): item for item in source_snapshot.get("ecs", {}).get("services", [])}
    for item in manifest.get("ecs_services", []):
        source_service = source_services.get(item.get("source_service"), {})
        target_cluster_arn = item.get("target_cluster_arn")
        target_service = item.get("target_service")
        if not target_cluster_arn or not target_service:
            continue
        desired = source_service.get("desiredCount", 0)
        running = source_service.get("runningCount", 0)
        status = source_service.get("status")
        expected[(target_cluster_arn, target_service)] = status == "ACTIVE" and running >= desired
    return expected


def build_smoke_checks(manifest, lambda_client, sqs_client, ecs_client, source_snapshot=None, apigw_client=None, ec2_client=None):
    checks = []
    source_snapshot = source_snapshot or {}
    lambda_issues = []
    lambda_names = {item["target_function"] for item in manifest.get("lambda_functions", [])}
    for item in manifest.get("lambda_functions", []):
        function_name = item["target_function"]
        try:
            config = lambda_client.get_function_configuration(FunctionName=function_name)
            if config.get("State") != "Active":
                lambda_issues.append({"function": function_name, "error": f"state={config.get('State')}"})
            last_update_status = config.get("LastUpdateStatus")
            if last_update_status not in {"Successful", None}:
                lambda_issues.append({"function": function_name, "error": f"last_update_status={last_update_status}"})
        except Exception as exc:
            lambda_issues.append({"function": function_name, "error": str(exc)})
    checks.append({
        "name": "lambda-functions-active",
        "status": "ok" if not lambda_issues else "issue",
        "issues": lambda_issues,
    })
    lambda_invoke_issues = []
    for item in manifest.get("lambda_functions", []):
        function_name = item["target_function"]
        try:
            response = lambda_client.invoke(FunctionName=function_name, InvocationType="DryRun")
            if response.get("StatusCode") not in {204}:
                lambda_invoke_issues.append({"function": function_name, "error": f"status_code={response.get('StatusCode')}"})
        except Exception as exc:
            lambda_invoke_issues.append({"function": function_name, "error": str(exc)})
    checks.append({
        "name": "lambda-dry-run-invoke",
        "status": "ok" if not lambda_invoke_issues else "issue",
        "issues": lambda_invoke_issues,
    })

    try:
        mappings = []
        paginator = lambda_client.get_paginator("list_event_source_mappings")
        for page in paginator.paginate():
            mappings.extend(page.get("EventSourceMappings", []))
        actual = sum(1 for item in mappings if item.get("FunctionArn", "").split(":")[-1] in lambda_names)
        expected = expected_mapping_count(manifest)
        checks.append({
            "name": "lambda-event-source-mapping-count",
            "status": "ok" if actual >= expected else "issue",
            "expected": expected,
            "actual": actual,
        })
        expected_states = expected_mapping_enabled_states(source_snapshot, manifest)
        mapping_issues = []
        for item in mappings:
            function_name = item.get("FunctionArn", "").split(":")[-1]
            if function_name not in lambda_names:
                continue
            target_event_source_arn = item.get("EventSourceArn")
            expected_enabled = expected_states.get((function_name, target_event_source_arn), True)
            state = item.get("State", "")
            if item.get("StateTransitionReason"):
                state = f"{state} ({item.get('StateTransitionReason')})"
            if expected_enabled and state and "Enabled" not in state:
                mapping_issues.append({
                    "function": function_name,
                    "uuid": item.get("UUID"),
                    "error": f"state={state}",
                })
            if expected_enabled and item.get("LastProcessingResult") == "PROBLEM":
                mapping_issues.append({
                    "function": function_name,
                    "uuid": item.get("UUID"),
                    "error": "last_processing_result=PROBLEM",
                })
        checks.append({
            "name": "lambda-event-source-mappings-enabled",
            "status": "ok" if not mapping_issues else "issue",
            "issues": mapping_issues,
        })
    except Exception as exc:
        checks.append({"name": "lambda-event-source-mapping-count", "status": "issue", "details": str(exc)})
        checks.append({"name": "lambda-event-source-mappings-enabled", "status": "issue", "details": str(exc)})

    queue_issues = []
    for item in manifest.get("sqs_queues", []):
        try:
            sqs_client.get_queue_attributes(QueueUrl=item["target_queue_url"], AttributeNames=["QueueArn"])
        except Exception as exc:
            queue_issues.append({"queue": item["target_queue"], "error": str(exc)})
    checks.append({
        "name": "sqs-queues-present",
        "status": "ok" if not queue_issues else "issue",
        "issues": queue_issues,
    })

    ecs_issues = []
    clusters = manifest.get("ecs_clusters", [])
    if clusters:
        try:
            described = ecs_client.describe_clusters(clusters=[item["target_cluster"] for item in clusters]).get("clusters", [])
            found_names = {item.get("clusterName") for item in described}
            for cluster in clusters:
                if cluster["target_cluster"] not in found_names:
                    ecs_issues.append({"cluster": cluster["target_cluster"], "error": "not found"})
        except Exception as exc:
            ecs_issues.append({"cluster": "unknown", "error": str(exc)})
    checks.append({
        "name": "ecs-clusters-present",
        "status": "ok" if not ecs_issues else "issue",
        "issues": ecs_issues,
    })

    ecs_service_issues = []
    expected_service_health = expected_ecs_service_health(source_snapshot, manifest)
    services_by_cluster = {}
    for item in manifest.get("ecs_services", []):
        cluster_arn = item.get("target_cluster_arn")
        service_name = item.get("target_service")
        if cluster_arn and service_name:
            services_by_cluster.setdefault(cluster_arn, []).append(service_name)
    for cluster_arn, service_names in services_by_cluster.items():
        try:
            described = ecs_client.describe_services(cluster=cluster_arn, services=service_names).get("services", [])
            found_names = {item.get("serviceName"): item for item in described}
        except Exception as exc:
            ecs_service_issues.append({"cluster": cluster_arn, "error": str(exc)})
            continue
        for service_name in service_names:
            service = found_names.get(service_name)
            if not service:
                ecs_service_issues.append({"cluster": cluster_arn, "service": service_name, "error": "not found"})
                continue
            desired = service.get("desiredCount", 0)
            running = service.get("runningCount", 0)
            status = service.get("status")
            expected_steady = expected_service_health.get((cluster_arn, service_name), True)
            if status != "ACTIVE":
                ecs_service_issues.append({"cluster": cluster_arn, "service": service_name, "error": f"status={status}"})
            elif expected_steady and running < desired:
                ecs_service_issues.append({"cluster": cluster_arn, "service": service_name, "error": f"running={running} desired={desired}"})
    checks.append({
        "name": "ecs-services-steady",
        "status": "ok" if not ecs_service_issues else "issue",
        "issues": ecs_service_issues,
    })

    api_issues = []
    api_stage_issues = []
    if apigw_client:
        for item in manifest.get("api_gateways", []):
            try:
                apigw_client.get_rest_api(restApiId=item["target_api_id"])
            except Exception as exc:
                api_issues.append({"api": item["target_api"], "error": str(exc)})
            try:
                stages = apigw_client.get_stages(restApiId=item["target_api_id"]).get("item", [])
                if not stages:
                    api_stage_issues.append({"api": item["target_api"], "error": "no stages"})
            except Exception as exc:
                api_stage_issues.append({"api": item["target_api"], "error": str(exc)})
    checks.append({
        "name": "api-gateways-present",
        "status": "ok" if not api_issues else "issue",
        "issues": api_issues,
    })
    checks.append({
        "name": "api-gateway-stages-present",
        "status": "ok" if not api_stage_issues else "issue",
        "issues": api_stage_issues,
    })

    network_issues = []
    if ec2_client:
        checks_to_run = [
            ("vpcs", "target_vpc", "describe_vpcs", "VpcIds", "Vpcs", "VpcId"),
            ("subnets", "target_subnet", "describe_subnets", "SubnetIds", "Subnets", "SubnetId"),
            ("route_tables", "target_route_table", "describe_route_tables", "RouteTableIds", "RouteTables", "RouteTableId"),
            ("security_groups", "target_group", "describe_security_groups", "GroupIds", "SecurityGroups", "GroupId"),
        ]
        for manifest_key, target_key, method_name, arg_name, response_key, id_key in checks_to_run:
            ids = [item[target_key] for item in manifest.get(manifest_key, []) if item.get(target_key)]
            if not ids:
                continue
            try:
                method = getattr(ec2_client, method_name)
                response = method(**{arg_name: ids})
                found = {item.get(id_key) for item in response.get(response_key, [])}
            except Exception as exc:
                network_issues.append({"resource": manifest_key, "error": str(exc)})
                continue
            for item in ids:
                if item not in found:
                    network_issues.append({"resource": manifest_key, "id": item, "error": "not found"})
    checks.append({
        "name": "network-resources-present",
        "status": "ok" if not network_issues else "issue",
        "issues": network_issues,
    })
    return attach_known_fixes_to_checks(checks)


def codebuild_smoke_checks(manifest, codebuild_client):
    issues = []
    projects = manifest.get("codebuild_projects", [])
    if not projects:
        return {"name": "codebuild-projects-present", "status": "ok", "issues": []}
    try:
        names = [item["target_project"] for item in projects]
        response = codebuild_client.batch_get_projects(names=names)
        projects_by_name = {item.get("name"): item for item in response.get("projects", [])}
        found = set(projects_by_name)
        for name in names:
            if name not in found:
                issues.append({"project": name, "error": "not found"})
                continue
            project = projects_by_name[name]
            if not project.get("serviceRole"):
                issues.append({"project": name, "error": "missing serviceRole"})
            environment = project.get("environment", {})
            if not environment.get("type") or not environment.get("image") or not environment.get("computeType"):
                issues.append({"project": name, "error": "incomplete environment"})
            source = project.get("source", {})
            if not source.get("type"):
                issues.append({"project": name, "error": "missing source type"})
    except Exception as exc:
        issues.append({"project": "unknown", "error": str(exc)})
    return attach_known_fixes_to_checks([{"name": "codebuild-projects-ready", "status": "ok" if not issues else "issue", "issues": issues}])[0]


def collect_snapshot_kms_references(snapshot):
    refs = []
    for secret in snapshot.get("secrets", []):
        if secret.get("KmsKeyId"):
            refs.append({"type": "secret", "name": secret.get("Name", ""), "key_id": secret["KmsKeyId"]})
    for queue in snapshot.get("sqs_queues", []):
        key_id = queue.get("Attributes", {}).get("KmsMasterKeyId")
        if key_id:
            refs.append({"type": "sqs", "name": queue.get("QueueName", ""), "key_id": key_id})
    for project in snapshot.get("codebuild_projects", []):
        if project.get("encryptionKey"):
            refs.append({"type": "codebuild", "name": project.get("name", ""), "key_id": project["encryptionKey"]})
    for bucket in snapshot.get("s3_buckets", []):
        encryption = bucket.get("BucketEncryption", {})
        for rule in encryption.get("Rules", []):
            key_id = rule.get("ApplyServerSideEncryptionByDefault", {}).get("KMSMasterKeyID")
            if key_id:
                refs.append({"type": "s3", "name": bucket.get("Name", ""), "key_id": key_id})
    return refs


def kms_smoke_checks(snapshot, manifest, kms_client):
    issues = []
    resource_mappings = manifest.get("resource_mappings", {})
    key_mappings = resource_mappings.get("kms_key_ids", {})
    alias_mappings = resource_mappings.get("kms_aliases", {})
    target_region = manifest.get("region", "")
    target_account_id = manifest.get("target_account_id", "")
    for item in collect_snapshot_kms_references(snapshot):
        source_key = item["key_id"]
        target_key = key_mappings.get(source_key, alias_mappings.get(source_key, source_key))
        if (
            isinstance(source_key, str)
            and source_key.startswith("arn:aws:kms:")
            and ":alias/aws/" in source_key
            and target_region
            and target_key == source_key
        ):
            parts = source_key.split(":")
            if len(parts) > 5:
                parts[3] = target_region
                if target_account_id:
                    parts[4] = target_account_id
                target_key = ":".join(parts)
        if isinstance(source_key, str) and source_key.startswith("arn:aws:kms:") and target_region:
            parts = source_key.split(":")
            if len(parts) > 3 and parts[3] != target_region and target_key == source_key:
                issues.append({
                    "resource_type": item["type"],
                    "resource_name": item["name"],
                    "key_id": source_key,
                    "error": "missing target-region KMS mapping",
                })
                continue
        try:
            kms_client.describe_key(KeyId=target_key)
        except Exception as exc:
            issues.append({
                "resource_type": item["type"],
                "resource_name": item["name"],
                "key_id": target_key,
                "error": str(exc),
            })
    return attach_known_fixes_to_checks([{"name": "kms-keys-ready", "status": "ok" if not issues else "issue", "issues": issues}])[0]


def api_gateway_parity_checks(source_snapshot, manifest, apigw_client):
    issues = []
    source_apis = {item.get("name"): item for item in source_snapshot.get("api_gateways", [])}
    for deployed in manifest.get("api_gateways", []):
        source_api = source_apis.get(deployed.get("source_api"))
        if not source_api:
            continue
        rest_api_id = deployed.get("target_api_id")
        try:
            actual_authorizers = apigw_client.get_authorizers(restApiId=rest_api_id, limit=500).get("items", [])
            actual_validators = apigw_client.get_request_validators(restApiId=rest_api_id, limit=500).get("items", [])
            actual_gateway_responses = apigw_client.get_gateway_responses(restApiId=rest_api_id, limit=500).get("items", [])
            all_usage_plans = apigw_client.get_usage_plans(limit=500).get("items", [])
            actual_usage_plans = [
                plan for plan in all_usage_plans
                if any(item.get("apiId") == rest_api_id for item in plan.get("apiStages", []))
            ]
            actual_api_key_count = 0
            for usage_plan in actual_usage_plans:
                actual_api_key_count += len(
                    apigw_client.get_usage_plan_keys(usagePlanId=usage_plan.get("id"), limit=500).get("items", [])
                )
            actual_domain_mapping_count = 0
            for domain_name in apigw_client.get_domain_names(limit=500).get("items", []):
                mappings = apigw_client.get_base_path_mappings(domainName=domain_name["domainName"], limit=500).get("items", [])
                actual_domain_mapping_count += sum(1 for mapping in mappings if mapping.get("restApiId") == rest_api_id)
        except Exception as exc:
            issues.append({"api": deployed.get("target_api"), "error": str(exc)})
            continue
        expected_authorizers = len(source_api.get("authorizers", []))
        expected_validators = len(source_api.get("request_validators", []))
        expected_gateway_responses = len(source_api.get("gateway_responses", []))
        expected_usage_plans = len(source_api.get("usage_plans", []))
        expected_api_key_count = sum(len(item.get("apiKeys", [])) for item in source_api.get("usage_plans", []))
        expected_domain_mapping_count = sum(len(item.get("mappings", [])) for item in source_api.get("domain_mappings", []))
        if len(actual_authorizers) < expected_authorizers:
            issues.append({"api": deployed.get("target_api"), "error": f"authorizers expected={expected_authorizers} actual={len(actual_authorizers)}"})
        if len(actual_validators) < expected_validators:
            issues.append({"api": deployed.get("target_api"), "error": f"request_validators expected={expected_validators} actual={len(actual_validators)}"})
        if len(actual_gateway_responses) < expected_gateway_responses:
            issues.append({"api": deployed.get("target_api"), "error": f"gateway_responses expected={expected_gateway_responses} actual={len(actual_gateway_responses)}"})
        if len(actual_usage_plans) < expected_usage_plans:
            issues.append({"api": deployed.get("target_api"), "error": f"usage_plans expected={expected_usage_plans} actual={len(actual_usage_plans)}"})
        if actual_api_key_count < expected_api_key_count:
            issues.append({"api": deployed.get("target_api"), "error": f"api_keys expected={expected_api_key_count} actual={actual_api_key_count}"})
        if actual_domain_mapping_count < expected_domain_mapping_count:
            issues.append({"api": deployed.get("target_api"), "error": f"domain_mappings expected={expected_domain_mapping_count} actual={actual_domain_mapping_count}"})
    return attach_known_fixes_to_checks([{"name": "api-gateway-extras-parity", "status": "ok" if not issues else "issue", "issues": issues}])[0]


def s3_parity_checks(source_snapshot, s3_client, manifest=None, s3_transfer_plan=None):
    issues = []
    bucket_mapping = {}
    for bucket in (s3_transfer_plan or {}).get("buckets", []):
        source_bucket = bucket.get("source_bucket")
        target_bucket = bucket.get("target_bucket")
        if source_bucket and target_bucket:
            bucket_mapping[source_bucket] = target_bucket
    bucket_mapping.update(((manifest or {}).get("resource_mappings", {}) or {}).get("s3_bucket_names", {}))
    execution_results = {
        item.get("source_bucket"): item
        for item in (s3_transfer_plan or {}).get("execution_results", [])
        if item.get("source_bucket")
    }
    for bucket in source_snapshot.get("s3_buckets", []):
        source_bucket_name = bucket.get("Name")
        if not source_bucket_name:
            continue
        bucket_name = bucket_mapping.get(source_bucket_name, source_bucket_name)
        transfer_result = execution_results.get(source_bucket_name)
        if transfer_result and transfer_result.get("issues"):
            for error in transfer_result.get("issues", []):
                issues.append({"bucket": source_bucket_name, "target_bucket": bucket_name, "error": error})
            continue
        try:
            s3_client.head_bucket(Bucket=bucket_name)
        except Exception as exc:
            issues.append({"bucket": source_bucket_name, "target_bucket": bucket_name, "error": str(exc)})
            continue
        try:
            target_tags = s3_client.get_bucket_tagging(Bucket=bucket_name).get("TagSet", [])
        except Exception:
            target_tags = []
        if bucket.get("Tags", []) and len(target_tags) < len(bucket.get("Tags", [])):
            issues.append({"bucket": source_bucket_name, "target_bucket": bucket_name, "error": f"tags expected>={len(bucket.get('Tags', []))} actual={len(target_tags)}"})
        try:
            target_versioning = s3_client.get_bucket_versioning(Bucket=bucket_name)
        except Exception:
            target_versioning = {}
        source_versioning = bucket.get("Versioning", {})
        if source_versioning.get("Status") and target_versioning.get("Status") != source_versioning.get("Status"):
            issues.append({"bucket": source_bucket_name, "target_bucket": bucket_name, "error": f"versioning expected={source_versioning.get('Status')} actual={target_versioning.get('Status')}"})
        try:
            target_encryption = s3_client.get_bucket_encryption(Bucket=bucket_name).get("ServerSideEncryptionConfiguration", {})
        except Exception:
            target_encryption = {}
        if bucket.get("BucketEncryption", {}).get("Rules") and not target_encryption.get("Rules"):
            issues.append({"bucket": source_bucket_name, "target_bucket": bucket_name, "error": "missing bucket encryption"})
        try:
            target_lifecycle = s3_client.get_bucket_lifecycle_configuration(Bucket=bucket_name).get("Rules", [])
        except Exception:
            target_lifecycle = []
        if bucket.get("LifecycleRules", []) and len(target_lifecycle) < len(bucket.get("LifecycleRules", [])):
            issues.append({"bucket": source_bucket_name, "target_bucket": bucket_name, "error": f"lifecycle_rules expected>={len(bucket.get('LifecycleRules', []))} actual={len(target_lifecycle)}"})
        try:
            target_cors = s3_client.get_bucket_cors(Bucket=bucket_name).get("CORSRules", [])
        except Exception:
            target_cors = []
        if bucket.get("CorsRules", []) and len(target_cors) < len(bucket.get("CorsRules", [])):
            issues.append({"bucket": source_bucket_name, "target_bucket": bucket_name, "error": f"cors_rules expected>={len(bucket.get('CorsRules', []))} actual={len(target_cors)}"})
        try:
            target_notifications = s3_client.get_bucket_notification_configuration(Bucket=bucket_name)
        except Exception:
            target_notifications = {}
        source_notification_count = sum(len(bucket.get("NotificationConfiguration", {}).get(key, [])) for key in ["QueueConfigurations", "TopicConfigurations", "LambdaFunctionConfigurations"])
        target_notification_count = sum(len(target_notifications.get(key, [])) for key in ["QueueConfigurations", "TopicConfigurations", "LambdaFunctionConfigurations"])
        if source_notification_count and target_notification_count < source_notification_count:
            issues.append({"bucket": source_bucket_name, "target_bucket": bucket_name, "error": f"notifications expected>={source_notification_count} actual={target_notification_count}"})
    return attach_known_fixes_to_checks([{"name": "s3-bucket-parity", "status": "ok" if not issues else "issue", "issues": issues}])[0]


def main():
    args = parse_args()
    config = load_transfer_config(args.config)
    client_slug = resolve_client_slug(args.client_slug, config, target_env=args.target_env)
    manifest_path, manifest = load_manifest(args.target_env, args.deployment_key, client_slug)
    target_external_id = args.target_external_id or config_override(config, "target_external_id", "")
    source_external_id = args.source_external_id or config_override(config, "source_external_id", "")
    target_region = args.region or manifest.get("region") or config_override(config, "target_region", "us-east-1")
    target_session = session_for(target_region, args.target_role_arn, external_id=target_external_id)
    lambda_client = target_session.client("lambda")
    logs_client = target_session.client("logs")
    secrets_client = target_session.client("secretsmanager")
    sqs_client = target_session.client("sqs")
    ecs_client = target_session.client("ecs")
    codebuild_client = target_session.client("codebuild")
    apigw_client = target_session.client("apigateway")
    ec2_client = target_session.client("ec2")
    kms_client = target_session.client("kms")
    s3_client = target_session.client("s3")
    dynamodb_resource = target_session.resource("dynamodb")
    source_dynamodb_resource = None
    source_snapshot = {}
    s3_transfer_plan = {}
    if manifest.get("source_region"):
        source_session = session_for(manifest["source_region"], args.source_role_arn, external_id=source_external_id)
        source_dynamodb_resource = source_session.resource("dynamodb")
    source_snapshot_path = manifest.get("source_snapshot")
    if source_snapshot_path:
        try:
            source_snapshot = json.loads(Path(source_snapshot_path).read_text(encoding="utf-8"))
        except Exception:
            source_snapshot = {}
        try:
            s3_transfer_plan = json.loads(Path(source_snapshot_path).with_name("s3_transfer_plan.json").read_text(encoding="utf-8"))
        except Exception:
            s3_transfer_plan = {}

    report = {
        "manifest_path": str(manifest_path),
        "target_env": sanitize_name(args.target_env),
        "deployment_key": manifest.get("deployment_key", deployment_dir_name(args.target_env, args.deployment_key)),
        "region": target_region,
        "functions": [],
        "secrets": [],
        "dynamodb_tables": [],
        "smoke_checks": [],
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

    for item in manifest.get("secrets", []):
        secret_name = item["target_secret"]
        issues = []
        details = {}
        try:
            details = secrets_client.describe_secret(SecretId=secret_name)
            present = True
        except ClientError as exc:
            present = False
            issues.append(str(exc))
        report["secrets"].append({
            "secret_name": secret_name,
            "present": present,
            "arn": details.get("ARN"),
            "issues": issues,
        })
        if issues:
            report["issues_found"] = True

    source_counts = {}
    if source_dynamodb_resource:
        for item in manifest.get("dynamodb_table_items", []):
            source_name = item["source_table"]
            try:
                source_counts[source_name] = scan_table_count(source_dynamodb_resource.Table(source_name))
            except Exception as exc:
                source_counts[source_name] = f"source-count-failed: {exc}"

    for item in manifest.get("dynamodb_tables", []):
        source_name = item["source_table"]
        target_name = item["target_table"]
        issues = []
        actual_count = None
        present = False
        try:
            table = dynamodb_resource.Table(target_name)
            table.load()
            actual_count = scan_table_count(table)
            present = True
        except Exception as exc:
            issues.append(str(exc))
        copied_item = next((entry for entry in manifest.get("dynamodb_table_items", []) if entry["target_table"] == target_name), None)
        expected_count = copied_item.get("copied_item_count", 0) if copied_item else 0
        if present and copied_item and actual_count != expected_count:
            issues.append(f"item-count-mismatch expected={expected_count} actual={actual_count}")
        source_count = source_counts.get(source_name)
        if present and isinstance(source_count, int) and actual_count != source_count:
            issues.append(f"source-target-count-mismatch source={source_count} target={actual_count}")
        report["dynamodb_tables"].append({
            "source_table": source_name,
            "target_table": target_name,
            "present": present,
            "expected_item_count": expected_count,
            "actual_item_count": actual_count,
            "source_item_count": source_count,
            "issues": issues,
        })
        if issues:
            report["issues_found"] = True

    report["smoke_checks"] = build_smoke_checks(
        manifest,
        lambda_client,
        sqs_client,
        ecs_client,
        source_snapshot=source_snapshot,
        apigw_client=apigw_client,
        ec2_client=ec2_client,
    )
    report["smoke_checks"].append(codebuild_smoke_checks(manifest, codebuild_client))
    report["smoke_checks"].append(kms_smoke_checks(source_snapshot, manifest, kms_client))
    report["smoke_checks"].append(api_gateway_parity_checks(source_snapshot, manifest, apigw_client))
    report["smoke_checks"].append(s3_parity_checks(source_snapshot, s3_client, manifest=manifest, s3_transfer_plan=s3_transfer_plan))
    if any(item.get("status") != "ok" for item in report["smoke_checks"]):
        report["issues_found"] = True

    report_dir = deployment_dir_path(args.target_env, args.deployment_key or manifest.get("deployment_key", ""), client_slug)
    report_path = report_dir / "validation_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if report["issues_found"]:
        remember_validation_issues(report, args.target_env, client_slug)
    append_audit_event(
        "validate_deployed_env",
        "ok" if not report["issues_found"] else "issues-found",
        {"report_path": str(report_path), "smoke_check_count": len(report["smoke_checks"])},
        target_env=args.target_env,
        client_slug=client_slug,
    )

    print(json.dumps({
        "status": "ok",
        "report_path": str(report_path),
        "issues_found": report["issues_found"],
    }, indent=2))


if __name__ == "__main__":
    main()
