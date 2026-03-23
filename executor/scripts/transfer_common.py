import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import boto3


def sanitize_name(value):
    cleaned = re.sub(r"[^a-zA-Z0-9-_]+", "-", value.strip().lower())
    return re.sub(r"-{2,}", "-", cleaned).strip("-_")


def resolve_client_slug(client_slug="", config=None, source_env="", target_env=""):
    explicit = str(client_slug or "").strip()
    if explicit:
        return sanitize_name(explicit)
    config = config or {}
    configured = str(config.get("overrides", {}).get("client_slug", "") or "").strip()
    if configured:
        return sanitize_name(configured)
    if target_env:
        return sanitize_name(target_env)
    if source_env:
        return sanitize_name(source_env)
    return "roman-art"


def state_root(client_slug=""):
    if not client_slug:
        return Path("state")
    return Path("state") / "clients" / sanitize_name(client_slug)


def inventory_dir_path(source_env="", inventory_key="", client_slug=""):
    return state_root(client_slug) / "aws_inventory" / inventory_dir_name(source_env, inventory_key)


def deployment_dir_path(target_env="", deployment_key="", client_slug=""):
    return state_root(client_slug) / "deployments" / deployment_dir_name(target_env, deployment_key)


def migration_dir_path(target_env="", client_slug=""):
    return state_root(client_slug) / "migrations" / sanitize_name(target_env)


def audit_log_path(client_slug=""):
    return state_root(client_slug) / "audit" / "execution_log.jsonl"


def agent_memory_path(client_slug=""):
    return state_root(client_slug) / "agent_memory" / "incidents.json"


def load_transfer_config(path):
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def inventory_dir_name(source_env="", inventory_key=""):
    if inventory_key:
        return sanitize_name(inventory_key)
    if source_env:
        return sanitize_name(source_env)
    return "full-account-scan"


def deployment_dir_name(target_env="", deployment_key=""):
    if deployment_key:
        return sanitize_name(deployment_key)
    if target_env:
        return sanitize_name(target_env)
    raise ValueError("target_env or deployment_key is required")


def git_backup_config(config):
    git_config = dict(config.get("git_backup", {}))
    git_config.setdefault("provider", "github")
    git_config.setdefault("host", "github.com")
    git_config.setdefault("protocol", "https")
    git_config.setdefault("organization", "")
    git_config.setdefault("repo_prefix", "")
    git_config.setdefault("create_repos", False)
    git_config.setdefault("username", "")
    git_config.setdefault("token_env", "")
    git_config.setdefault("test_repo_url", "")
    return git_config


GIT_BACKUP_OVERRIDE_KEYS = {
    "provider",
    "host",
    "protocol",
    "organization",
    "repo_prefix",
    "create_repos",
    "username",
    "token_env",
    "test_repo_url",
}


def apply_git_backup_overrides(config, overrides):
    merged = dict(config or {})
    git_config = git_backup_config(merged)
    for key, value in (overrides or {}).items():
        if key not in GIT_BACKUP_OVERRIDE_KEYS:
            continue
        if value in ("", None):
            continue
        git_config[key] = value
    merged["git_backup"] = git_config
    return merged


def authenticated_git_url(url, git_config):
    return url


def git_auth_env(url, git_config, base_env=None):
    if not url or git_config.get("protocol") == "ssh":
        return None
    token_env = git_config.get("token_env", "")
    if not token_env:
        return None
    token = os.environ.get(token_env, "")
    if not token:
        return None
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    username = git_config.get("username") or "x-access-token"
    env = dict(base_env or os.environ)
    askpass_path = Path(__file__).with_name("git_askpass.cmd")
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = str(askpass_path)
    env["AWS_DEV_AGENT_GIT_USERNAME"] = username
    env["AWS_DEV_AGENT_GIT_TOKEN"] = token
    return env


def git_command_with_auth(base_args, git_config, url=""):
    return list(base_args)


def session_for(region, role_arn="", session_name="aws-dev-agent-transfer", external_id=""):
    if not role_arn:
        return boto3.session.Session(region_name=region)
    base = boto3.session.Session(region_name=region)
    sts = base.client("sts")
    assume_role_args = {
        "RoleArn": role_arn,
        "RoleSessionName": session_name,
    }
    if external_id:
        assume_role_args["ExternalId"] = external_id
    creds = sts.assume_role(**assume_role_args)["Credentials"]
    return boto3.session.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )


def account_id_for(session):
    return session.client("sts").get_caller_identity()["Account"]


def enabled_regions(session, fallback_region="us-east-1"):
    region_name = session.region_name or fallback_region
    try:
        regions = session.client("ec2", region_name=region_name).describe_regions(AllRegions=False).get("Regions", [])
    except Exception:
        return [region_name]
    names = [item.get("RegionName") for item in regions if item.get("RegionName")]
    return names or [region_name]


def should_exclude(resource_type, resource_name, config):
    exclusions = config.get("exclusions", {})
    patterns = exclusions.get(resource_type, [])
    for pattern in patterns:
        if pattern == resource_name:
            return True
        if pattern.endswith("*") and resource_name.startswith(pattern[:-1]):
            return True
    return False


def config_override(config, key, default=None):
    return config.get("overrides", {}).get(key, default)


def ensure_target_scope_safe(source_session, target_session, source_region, target_region, config=None):
    config = config or {}
    source_account = account_id_for(source_session)
    target_account = account_id_for(target_session)
    allow_same_scope = bool(config_override(config, "allow_same_scope", False))
    if source_account == target_account and source_region == target_region and not allow_same_scope:
        raise RuntimeError(
            "Refusing target writes in the same AWS account and region as the source. "
            "Set overrides.allow_same_scope=true only if that is intentional."
        )
    return source_account, target_account
