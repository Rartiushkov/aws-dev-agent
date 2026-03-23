import json
import re
from pathlib import Path

from bridges.plan_creator import build_create_lambda_plan
from executor.scripts.transfer_common import resolve_client_slug


ROOT_DIR = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT_DIR / "state"


def _field(field_id, label, field_type, required=False, default=None, options=None, help_text=""):
    return {
        "id": field_id,
        "label": label,
        "type": field_type,
        "required": required,
        "default": default,
        "options": options or [],
        "help_text": help_text,
    }


ACTION_CATALOG = {
    "test-aws-connection": {
        "id": "test-aws-connection",
        "label": "Test AWS Connection",
        "category": "aws",
        "ready": "ready",
        "preview_supported": True,
        "apply_supported": True,
        "approval_required": False,
        "destructive": False,
        "description": "Verify AWS credentials and show basic account identity before running migrations.",
        "fields": [
            _field("region", "AWS Region", "text", default="us-east-1"),
        ],
    },
    "deploy-environment": {
        "id": "deploy-environment",
        "label": "Deploy Environment",
        "category": "migration",
        "ready": "ready",
        "preview_supported": True,
        "apply_supported": True,
        "approval_required": True,
        "destructive": False,
        "description": "Deploy a discovered source environment into a target environment and validate it.",
        "fields": [
            _field("source_env", "Source Env", "text", required=True),
            _field("target_env", "Target Env", "text", required=True),
            _field("team", "Team", "text", default=""),
            _field("source_region", "Source Region", "text", default=""),
            _field("region", "Target Region", "text", default="us-east-1"),
            _field("config", "Config Path", "path", default=""),
            _field("client_slug", "Client Slug", "text", default="roman-art"),
        ],
    },
    "destroy-environment": {
        "id": "destroy-environment",
        "label": "Destroy Environment",
        "category": "migration",
        "ready": "ready",
        "preview_supported": True,
        "apply_supported": True,
        "approval_required": True,
        "destructive": True,
        "description": "Delete target resources created for a deployed environment and emit a destroy report.",
        "fields": [
            _field("target_env", "Target Env", "text", required=True),
            _field("region", "Target Region", "text", default="us-east-1"),
            _field("config", "Config Path", "path", default=""),
            _field("client_slug", "Client Slug", "text", default="roman-art"),
        ],
    },
    "export-backup-to-git": {
        "id": "export-backup-to-git",
        "label": "Export AWS Backup To Git",
        "category": "git",
        "ready": "ready",
        "preview_supported": True,
        "apply_supported": True,
        "approval_required": True,
        "destructive": False,
        "description": "Export discovered AWS artifacts into a Git repository layout, optionally commit and push them.",
        "fields": [
            _field("source_env", "Source Env", "text", required=True),
            _field("organization", "Git Organization", "text", default=""),
            _field("repo_prefix", "Repo Prefix", "text", default=""),
            _field("repo_name", "Repo Name", "text", default=""),
            _field("host", "Git Host", "text", default="github.com"),
            _field("protocol", "Protocol", "select", default="https", options=["https", "ssh"]),
            _field("username", "Git Username", "text", default=""),
            _field("token_env", "Token Env", "text", default=""),
            _field("output_dir", "Output Dir", "path", default=""),
            _field("init_git", "Init Git", "boolean", default=True),
            _field("commit", "Commit", "boolean", default=True),
            _field("push", "Push", "boolean", default=False),
            _field("config", "Config Path", "path", default=""),
            _field("client_slug", "Client Slug", "text", default="roman-art"),
        ],
    },
    "test-git-connection": {
        "id": "test-git-connection",
        "label": "Test Git Connection",
        "category": "git",
        "ready": "ready",
        "preview_supported": True,
        "apply_supported": True,
        "approval_required": False,
        "destructive": False,
        "description": "Check whether the client Git destination is reachable with the configured credentials.",
        "fields": [
            _field("organization", "Git Organization", "text", default=""),
            _field("host", "Git Host", "text", default="github.com"),
            _field("protocol", "Protocol", "select", default="https", options=["https", "ssh"]),
            _field("username", "Git Username", "text", default=""),
            _field("token_env", "Token Env", "text", default=""),
            _field("test_repo_url", "Test Repo URL", "text", default=""),
            _field("config", "Config Path", "path", default=""),
        ],
    },
    "create-lambda": {
        "id": "create-lambda",
        "label": "Create Lambda",
        "category": "builder",
        "ready": "ready",
        "preview_supported": True,
        "apply_supported": True,
        "approval_required": True,
        "destructive": False,
        "description": "Create a Lambda function from a safe template with optional trigger wiring.",
        "fields": [
            _field("function_name", "Function Name", "text", required=True),
            _field("runtime", "Runtime", "select", default="python3.11", options=["python3.11", "python3.12"]),
            _field("template_id", "Code Template", "select", default="hello-world", options=["hello-world", "api-handler", "sqs-consumer", "scheduled-task"]),
            _field("iam_scope", "IAM Scope", "select", default="basic", options=["basic", "custom"]),
            _field("role_arn", "Custom Role ARN", "text", default=""),
            _field("trigger_type", "Optional Trigger", "select", default="none", options=["none", "sqs", "schedule"]),
            _field("trigger_source", "Trigger Source", "text", default="", help_text="Queue ARN for SQS or cron/rate expression for schedule."),
            _field("include_test", "Run Smoke Test", "boolean", default=True),
        ],
    },
    "analyze-cost-brain": {
        "id": "analyze-cost-brain",
        "label": "Analyze Cost Brain",
        "category": "aws",
        "ready": "ready",
        "preview_supported": True,
        "apply_supported": True,
        "approval_required": False,
        "destructive": False,
        "description": "Build a billing-aware cost report that shows where spend is concentrated and what to optimize next.",
        "fields": [
            _field("source_env", "Source Env", "text", default="full-account-scan"),
            _field("inventory_key", "Inventory Key", "text", default=""),
            _field("region", "AWS Region", "text", default="us-east-1"),
            _field("days", "Window Days", "text", default="30"),
            _field("config", "Config Path", "path", default=""),
            _field("client_slug", "Client Slug", "text", default="roman-art"),
        ],
    },
    "analyze-performance-brain": {
        "id": "analyze-performance-brain",
        "label": "Analyze Performance Brain",
        "category": "aws",
        "ready": "ready",
        "preview_supported": True,
        "apply_supported": True,
        "approval_required": False,
        "destructive": False,
        "description": "Explain why the system may be slow by surfacing likely AWS bottlenecks and root-cause signals.",
        "fields": [
            _field("source_env", "Source Env", "text", default="full-account-scan"),
            _field("region", "AWS Region", "text", default="us-east-1"),
            _field("live_metrics", "Live Metrics", "boolean", default=True),
            _field("config", "Config Path", "path", default=""),
            _field("client_slug", "Client Slug", "text", default="roman-art"),
        ],
    },
}


def list_ui_actions():
    return list(ACTION_CATALOG.values())


def get_ui_action(action_id):
    action = ACTION_CATALOG.get(action_id)
    if not action:
        raise KeyError(f"Unknown UI action: {action_id}")
    return action


def _append_flag(parts, flag, value):
    if value in ("", None, False):
        return
    if value is True:
        parts.append(flag)
        return
    parts.extend([flag, str(value)])


def _quote_if_needed(value):
    value = str(value)
    return f'"{value}"' if " " in value else value


def _command_string(parts):
    return " ".join(_quote_if_needed(item) for item in parts)


SAFE_GIT_HOSTS = {"github.com", "gitlab.com", "bitbucket.org"}
SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
SAFE_ENV_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_FUNCTION_PATTERN = re.compile(r"^[A-Za-z0-9-_]{1,64}$")


def validate_ui_action_values(action_id, values):
    values = dict(values or {})
    if action_id in {"deploy-environment", "destroy-environment", "export-backup-to-git", "analyze-cost-brain"}:
        for key in ("source_env", "target_env", "team", "client_slug"):
            if key in values and values.get(key) and not SAFE_NAME_PATTERN.match(str(values[key])):
                raise ValueError(f"Invalid value for {key}")
    if action_id == "analyze-cost-brain":
        days = str(values.get("days", "30") or "30")
        if not days.isdigit() or int(days) <= 0:
            raise ValueError("Invalid days")
    if action_id in {"test-git-connection", "export-backup-to-git"}:
        host = values.get("host", "")
        if host and host not in SAFE_GIT_HOSTS:
            raise ValueError("Unsupported Git host")
        for key in ("organization", "repo_prefix", "repo_name"):
            if values.get(key) and not SAFE_NAME_PATTERN.match(str(values[key])):
                raise ValueError(f"Invalid value for {key}")
        if values.get("token_env") and not SAFE_ENV_PATTERN.match(str(values["token_env"])):
            raise ValueError("Invalid token_env")
        if values.get("test_repo_url") and not str(values["test_repo_url"]).startswith(("https://", "git@")):
            raise ValueError("Invalid test_repo_url")
    if action_id == "create-lambda":
        function_name = str(values.get("function_name", ""))
        if not SAFE_FUNCTION_PATTERN.match(function_name):
            raise ValueError("Invalid function_name")
        trigger_type = values.get("trigger_type") or "none"
        trigger_source = str(values.get("trigger_source") or "")
        if trigger_type == "schedule" and trigger_source and not re.match(r"^(rate|cron)\([^)]+\)$", trigger_source):
            raise ValueError("Invalid schedule expression")
        if trigger_type == "sqs" and trigger_source and not trigger_source.startswith("arn:aws:sqs:"):
            raise ValueError("Invalid SQS trigger ARN")
        if values.get("role_arn") and not str(values["role_arn"]).startswith("arn:aws:iam::"):
            raise ValueError("Invalid role ARN")
    return values


def _client_root(client_slug=""):
    resolved = resolve_client_slug(client_slug)
    return STATE_DIR / "clients" / resolved


def _deployment_dir(target_env, client_slug=""):
    return _client_root(client_slug) / "deployments" / str(target_env).strip().lower().replace(" ", "-")


def _inventory_dir(source_env, client_slug=""):
    return _client_root(client_slug) / "aws_inventory" / str(source_env).strip().lower().replace(" ", "-")


def _read_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _artifact_entry(label, path):
    data = _read_json(path)
    entry = {
        "label": label,
        "path": str(path),
        "exists": path.exists(),
    }
    if not data:
        return entry
    if label == "Deployment Manifest":
        failure_count = sum(len(items) for items in data.get("failures", {}).values()) if isinstance(data.get("failures"), dict) else 0
        entry["summary"] = {
            "target_env": data.get("target_env", ""),
            "target_region": data.get("region", ""),
            "failure_count": failure_count,
        }
    elif label == "Validation Report":
        entry["summary"] = {
            "issues_found": data.get("issues_found"),
            "smoke_check_count": len(data.get("smoke_checks", [])),
        }
    elif label == "Destroy Report":
        entry["summary"] = {
            "status": data.get("status", ""),
            "failed_resource_count": data.get("failed_resource_count", 0),
        }
    elif label == "Git Export Index":
        entry["summary"] = {
            "source_env": data.get("source_env", ""),
            "account_id": data.get("account_id", ""),
        }
    return entry


def build_action_artifacts(action_id, values):
    values = values or {}
    if action_id == "test-aws-connection":
        return []
    if action_id == "deploy-environment":
        deployment_dir = _deployment_dir(values.get("target_env", ""), values.get("client_slug", ""))
        return [
            _artifact_entry("Deployment Manifest", deployment_dir / "deployment_manifest.json"),
            _artifact_entry("Validation Report", deployment_dir / "validation_report.json"),
        ]
    if action_id == "destroy-environment":
        deployment_dir = _deployment_dir(values.get("target_env", ""), values.get("client_slug", ""))
        return [
            _artifact_entry("Destroy Report", deployment_dir / "destroy_report.json"),
        ]
    if action_id == "export-backup-to-git":
        source_env = values.get("source_env", "")
        organization = values.get("organization", "") or "local"
        output_dir = Path(values.get("output_dir") or _client_root(values.get("client_slug", "")) / "client_git_exports" / organization / source_env)
        return [
            _artifact_entry("Git Export Index", output_dir / "README.json"),
            {"label": "Git Export Directory", "path": str(output_dir), "exists": output_dir.exists()},
        ]
    if action_id == "analyze-cost-brain":
        inventory_dir = _inventory_dir(values.get("inventory_key") or values.get("source_env") or "full-account-scan", values.get("client_slug", ""))
        return [
            _artifact_entry("Cost Breakdown Report", inventory_dir / "cost_breakdown_report.json"),
            _artifact_entry("Cost Brain Report", inventory_dir / "cost_brain_report.json"),
            _artifact_entry("Unused Resource Report", inventory_dir / "unused_resource_report.json"),
            _artifact_entry("Client Cost Report", inventory_dir / "client_cost_report.json"),
            {"label": "Client Cost Markdown", "path": str(inventory_dir / "client_cost_report.md"), "exists": (inventory_dir / "client_cost_report.md").exists()},
        ]
    if action_id == "analyze-performance-brain":
        inventory_dir = _inventory_dir(values.get("source_env") or "full-account-scan", values.get("client_slug", ""))
        return [
            _artifact_entry("Performance Report", inventory_dir / "performance_report.json"),
            {"label": "Performance Markdown", "path": str(inventory_dir / "performance_report.md"), "exists": (inventory_dir / "performance_report.md").exists()},
            _artifact_entry("Why Is It Slow Report", inventory_dir / "why_is_it_slow_report.json"),
            {"label": "Why Is It Slow Markdown", "path": str(inventory_dir / "why_is_it_slow_report.md"), "exists": (inventory_dir / "why_is_it_slow_report.md").exists()},
        ]
    if action_id in {"test-git-connection", "create-lambda"}:
        return []
    return []


def build_action_status(action_id, values):
    artifacts = build_action_artifacts(action_id, values)
    if action_id == "test-aws-connection":
        return {"run_status": "ready", "validation_result": "connection-check"}
    if action_id == "deploy-environment":
        manifest = next((item for item in artifacts if item["label"] == "Deployment Manifest"), None)
        validation = next((item for item in artifacts if item["label"] == "Validation Report"), None)
        if validation and validation.get("exists") and validation.get("summary", {}).get("issues_found") is False:
            return {
                "run_status": "completed",
                "manifest_path": manifest["path"] if manifest else "",
                "validation_result": "passed",
            }
        if manifest and manifest.get("exists"):
            return {
                "run_status": "partial",
                "manifest_path": manifest["path"],
                "validation_result": "pending" if not validation or not validation.get("exists") else "issues-found",
            }
        return {"run_status": "not_started", "manifest_path": "", "validation_result": "unknown"}
    if action_id == "destroy-environment":
        report = artifacts[0] if artifacts else None
        if report and report.get("exists"):
            return {"run_status": "completed", "report_path": report["path"]}
        return {"run_status": "not_started", "report_path": ""}
    if action_id == "export-backup-to-git":
        index = next((item for item in artifacts if item["label"] == "Git Export Index"), None)
        return {
            "run_status": "completed" if index and index.get("exists") else "not_started",
            "report_path": index["path"] if index and index.get("exists") else "",
        }
    if action_id == "analyze-cost-brain":
        report = next((item for item in artifacts if item["label"] == "Cost Brain Report"), None)
        return {
            "run_status": "completed" if report and report.get("exists") else "not_started",
            "report_path": report["path"] if report and report.get("exists") else "",
        }
    if action_id == "analyze-performance-brain":
        report = next((item for item in artifacts if item["label"] == "Performance Report"), None)
        return {
            "run_status": "completed" if report and report.get("exists") else "not_started",
            "report_path": report["path"] if report and report.get("exists") else "",
        }
    return {"run_status": "preview_only"}


def _build_deploy_commands(values, preview):
    parts = ["python", "executor/scripts/deploy_discovered_env.py"]
    _append_flag(parts, "--source-env", values.get("source_env"))
    _append_flag(parts, "--target-env", values.get("target_env"))
    _append_flag(parts, "--team", values.get("team"))
    _append_flag(parts, "--source-region", values.get("source_region"))
    _append_flag(parts, "--region", values.get("region") or "us-east-1")
    _append_flag(parts, "--config", values.get("config"))
    _append_flag(parts, "--client-slug", values.get("client_slug") or "roman-art")
    if preview:
        parts.append("--read-only-plan")
        return [{"type": "command", "cmd": _command_string(parts)}]
    return [
        {"type": "command", "cmd": _command_string(parts)},
        {
            "type": "command",
            "cmd": _command_string([
                "python",
                "executor/scripts/validate_deployed_env.py",
                "--target-env",
                values.get("target_env"),
                "--region",
                values.get("region") or "us-east-1",
                "--client-slug",
                values.get("client_slug") or "roman-art",
            ]),
        },
    ]


def _build_destroy_commands(values):
    parts = ["python", "executor/scripts/destroy_deployed_env.py"]
    _append_flag(parts, "--target-env", values.get("target_env"))
    _append_flag(parts, "--region", values.get("region") or "us-east-1")
    _append_flag(parts, "--config", values.get("config"))
    _append_flag(parts, "--client-slug", values.get("client_slug") or "roman-art")
    return [{"type": "command", "cmd": _command_string(parts)}]


def _build_export_backup_commands(values):
    parts = ["python", "executor/scripts/export_aws_backup_to_git.py"]
    _append_flag(parts, "--source-env", values.get("source_env"))
    _append_flag(parts, "--config", values.get("config"))
    _append_flag(parts, "--output-dir", values.get("output_dir"))
    _append_flag(parts, "--repo-name", values.get("repo_name"))
    _append_flag(parts, "--organization", values.get("organization"))
    _append_flag(parts, "--repo-prefix", values.get("repo_prefix"))
    _append_flag(parts, "--host", values.get("host") or "github.com")
    _append_flag(parts, "--protocol", values.get("protocol") or "https")
    _append_flag(parts, "--username", values.get("username"))
    _append_flag(parts, "--token-env", values.get("token_env"))
    _append_flag(parts, "--init-git", values.get("init_git", True))
    _append_flag(parts, "--commit", values.get("commit", True))
    _append_flag(parts, "--push", values.get("push", False))
    _append_flag(parts, "--client-slug", values.get("client_slug") or "roman-art")
    return [{"type": "command", "cmd": _command_string(parts)}]


def _build_test_git_connection_commands(values):
    parts = ["python", "executor/scripts/test_git_connection.py"]
    _append_flag(parts, "--config", values.get("config"))
    _append_flag(parts, "--organization", values.get("organization"))
    _append_flag(parts, "--host", values.get("host") or "github.com")
    _append_flag(parts, "--protocol", values.get("protocol") or "https")
    _append_flag(parts, "--username", values.get("username"))
    _append_flag(parts, "--token-env", values.get("token_env"))
    _append_flag(parts, "--test-repo-url", values.get("test_repo_url"))
    _append_flag(parts, "--client-slug", values.get("client_slug") or "roman-art")
    return [{"type": "command", "cmd": _command_string(parts)}]


def _build_create_lambda_commands(values):
    return build_create_lambda_plan(
        function_name=values.get("function_name", ""),
        runtime=values.get("runtime") or "python3.11",
        template_id=values.get("template_id") or "hello-world",
        iam_scope=values.get("iam_scope") or "basic",
        role_arn=values.get("role_arn") or "",
        trigger_type=values.get("trigger_type") or "none",
        trigger_source=values.get("trigger_source") or "",
        include_test=bool(values.get("include_test", True)),
    )


def _build_analyze_cost_brain_commands(values):
    parts = ["python", "executor/scripts/analyze_cost_brain.py"]
    _append_flag(parts, "--source-env", values.get("source_env") or "full-account-scan")
    _append_flag(parts, "--inventory-key", values.get("inventory_key"))
    _append_flag(parts, "--region", values.get("region") or "us-east-1")
    _append_flag(parts, "--days", values.get("days") or "30")
    _append_flag(parts, "--config", values.get("config"))
    _append_flag(parts, "--client-slug", values.get("client_slug") or "roman-art")
    return [{"type": "command", "cmd": _command_string(parts)}]


def _build_analyze_performance_brain_commands(values):
    parts = ["python", "executor/scripts/analyze_performance_issues.py"]
    _append_flag(parts, "--source-env", values.get("source_env") or "full-account-scan")
    _append_flag(parts, "--region", values.get("region") or "us-east-1")
    _append_flag(parts, "--config", values.get("config"))
    _append_flag(parts, "--live-metrics", values.get("live_metrics", True))
    _append_flag(parts, "--client-slug", values.get("client_slug") or "roman-art")
    return [{"type": "command", "cmd": _command_string(parts)}]


def build_action_preview(action_id, values=None, apply=False):
    action = get_ui_action(action_id)
    values = validate_ui_action_values(action_id, values or {})
    if action_id == "test-aws-connection":
        commands = [{"type": "command", "cmd": _command_string(["python", "-c", "import boto3; print(boto3.client('sts').get_caller_identity())"])}]
    elif action_id == "deploy-environment":
        commands = _build_deploy_commands(values, preview=not apply)
    elif action_id == "destroy-environment":
        commands = _build_destroy_commands(values)
    elif action_id == "export-backup-to-git":
        commands = _build_export_backup_commands(values)
    elif action_id == "test-git-connection":
        commands = _build_test_git_connection_commands(values)
    elif action_id == "create-lambda":
        commands = _build_create_lambda_commands(values)
    elif action_id == "analyze-cost-brain":
        commands = _build_analyze_cost_brain_commands(values)
    elif action_id == "analyze-performance-brain":
        commands = _build_analyze_performance_brain_commands(values)
    else:
        raise KeyError(f"Unknown UI action: {action_id}")

    return {
        "action": action,
        "mode": "apply" if apply else "preview",
        "approval_required": bool(action.get("approval_required") and apply),
        "destructive": bool(action.get("destructive")),
        "commands": commands,
        "artifacts": build_action_artifacts(action_id, values),
        "status": build_action_status(action_id, values),
    }
