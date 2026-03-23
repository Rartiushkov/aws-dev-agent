import io
import json
import subprocess
import zipfile
from pathlib import Path

import boto3

from bridges.plan_creator import lambda_template_source, resolve_lambda_role_arn
from bridges.ui_actions import validate_ui_action_values


def run_test_aws_connection_action(values, sts_client=None):
    values = validate_ui_action_values("test-aws-connection", values)
    session = boto3.session.Session(region_name=values.get("region") or "us-east-1")
    sts_client = sts_client or session.client("sts")
    identity = sts_client.get_caller_identity()
    return {
        "status": "ok",
        "region": values.get("region") or "us-east-1",
        "account": identity.get("Account"),
        "arn": identity.get("Arn"),
        "user_id": identity.get("UserId"),
    }


def build_script_invocation(action_id, values):
    values = validate_ui_action_values(action_id, values)
    if action_id == "analyze-cost-brain":
        return [
            "python",
            "executor/scripts/analyze_cost_brain.py",
            "--source-env",
            values.get("source_env") or "full-account-scan",
            "--region",
            values.get("region") or "us-east-1",
            "--days",
            str(values.get("days") or "30"),
            *([] if not values.get("inventory_key") else ["--inventory-key", values["inventory_key"]]),
            *([] if not values.get("config") else ["--config", values["config"]]),
        ]
    if action_id == "analyze-performance-brain":
        return [
            "python",
            "executor/scripts/analyze_performance_issues.py",
            "--source-env",
            values.get("source_env") or "full-account-scan",
            "--region",
            values.get("region") or "us-east-1",
            *([] if not values.get("config") else ["--config", values["config"]]),
            *(["--live-metrics"] if values.get("live_metrics", True) else []),
        ]
    if action_id == "deploy-environment":
        return [
            "python",
            "executor/scripts/deploy_discovered_env.py",
            "--source-env",
            values["source_env"],
            "--target-env",
            values["target_env"],
            "--region",
            values.get("region") or "us-east-1",
            *([] if not values.get("team") else ["--team", values["team"]]),
            *([] if not values.get("source_region") else ["--source-region", values["source_region"]]),
            *([] if not values.get("config") else ["--config", values["config"]]),
        ]
    if action_id == "destroy-environment":
        return [
            "python",
            "executor/scripts/destroy_deployed_env.py",
            "--target-env",
            values["target_env"],
            "--region",
            values.get("region") or "us-east-1",
            *([] if not values.get("config") else ["--config", values["config"]]),
        ]
    if action_id == "export-backup-to-git":
        return [
            "python",
            "executor/scripts/export_aws_backup_to_git.py",
            "--source-env",
            values["source_env"],
            "--host",
            values.get("host") or "github.com",
            "--protocol",
            values.get("protocol") or "https",
            *([] if not values.get("organization") else ["--organization", values["organization"]]),
            *([] if not values.get("repo_prefix") else ["--repo-prefix", values["repo_prefix"]]),
            *([] if not values.get("repo_name") else ["--repo-name", values["repo_name"]]),
            *([] if not values.get("username") else ["--username", values["username"]]),
            *([] if not values.get("token_env") else ["--token-env", values["token_env"]]),
            *([] if not values.get("output_dir") else ["--output-dir", values["output_dir"]]),
            *([] if not values.get("config") else ["--config", values["config"]]),
            *(["--init-git"] if values.get("init_git", True) else []),
            *(["--commit"] if values.get("commit", True) else []),
            *(["--push"] if values.get("push", False) else []),
        ]
    if action_id == "test-git-connection":
        return [
            "python",
            "executor/scripts/test_git_connection.py",
            "--host",
            values.get("host") or "github.com",
            "--protocol",
            values.get("protocol") or "https",
            *([] if not values.get("organization") else ["--organization", values["organization"]]),
            *([] if not values.get("username") else ["--username", values["username"]]),
            *([] if not values.get("token_env") else ["--token-env", values["token_env"]]),
            *([] if not values.get("test_repo_url") else ["--test-repo-url", values["test_repo_url"]]),
            *([] if not values.get("config") else ["--config", values["config"]]),
        ]
    raise KeyError(f"Unsupported script action: {action_id}")


def run_script_action(action_id, values, check=False):
    command = build_script_invocation(action_id, values)
    completed = subprocess.run(command, capture_output=True, text=True, check=check)
    return {
        "status": "ok" if completed.returncode == 0 else "failed",
        "command": command,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "exit_code": completed.returncode,
    }


def _lambda_zip_bytes(template_id):
    code = lambda_template_source(template_id).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("lambda_function.py", code)
    return buffer.getvalue()


def run_create_lambda_action(values, lambda_client=None, events_client=None):
    values = validate_ui_action_values("create-lambda", values)
    lambda_client = lambda_client or boto3.client("lambda")
    events_client = events_client or boto3.client("events")
    function_name = values["function_name"]
    runtime = values.get("runtime") or "python3.11"
    role_arn = resolve_lambda_role_arn(values.get("iam_scope") or "basic", values.get("role_arn") or "")
    payload_bytes = _lambda_zip_bytes(values.get("template_id") or "hello-world")

    lambda_client.create_function(
        FunctionName=function_name,
        Runtime=runtime,
        Role=role_arn,
        Handler="lambda_function.handler",
        Code={"ZipFile": payload_bytes},
    )
    lambda_client.get_waiter("function_active_v2").wait(FunctionName=function_name)
    lambda_client.update_function_code(FunctionName=function_name, ZipFile=payload_bytes)
    lambda_client.get_waiter("function_updated").wait(FunctionName=function_name)

    trigger_type = values.get("trigger_type") or "none"
    trigger_source = values.get("trigger_source") or ""
    if trigger_type == "sqs" and trigger_source:
        lambda_client.create_event_source_mapping(FunctionName=function_name, EventSourceArn=trigger_source)
    elif trigger_type == "schedule" and trigger_source:
        rule_name = f"{function_name}-schedule"
        rule = events_client.put_rule(Name=rule_name, ScheduleExpression=trigger_source)
        function_arn = lambda_client.get_function(FunctionName=function_name)["Configuration"]["FunctionArn"]
        lambda_client.add_permission(
            FunctionName=function_name,
            StatementId=f"{function_name}-schedule-invoke",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule["RuleArn"],
        )
        events_client.put_targets(Rule=rule_name, Targets=[{"Id": "1", "Arn": function_arn}])

    invoke_result = None
    if values.get("include_test", True):
        response = lambda_client.invoke(FunctionName=function_name, InvocationType="RequestResponse", Payload=b"{}")
        body = response.get("Payload")
        payload_text = body.read().decode("utf-8") if hasattr(body, "read") else ""
        invoke_result = {
            "status_code": response.get("StatusCode"),
            "payload": payload_text,
        }

    return {
        "status": "ok",
        "function_name": function_name,
        "runtime": runtime,
        "role_arn": role_arn,
        "trigger_type": trigger_type,
        "invoke_result": invoke_result,
    }


def run_ui_action(action_id, values, approved=False):
    if action_id == "test-aws-connection":
        return run_test_aws_connection_action(values)
    if action_id == "create-lambda":
        if not approved:
            return {"status": "approval_required"}
        return run_create_lambda_action(values)
    if action_id in {"deploy-environment", "destroy-environment", "export-backup-to-git"} and not approved:
        return {"status": "approval_required"}
    return run_script_action(action_id, values)
