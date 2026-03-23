import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from executor.scripts.transfer_common import (
    apply_git_backup_overrides,
    deployment_dir_path,
    git_auth_env,
    git_command_with_auth,
    git_backup_config,
    inventory_dir_path,
    resolve_client_slug,
    load_transfer_config,
    state_root,
)


def parse_args():
    parser = argparse.ArgumentParser(description="Export discovered AWS artifacts into a Git-friendly backup folder.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--init-git", action="store_true")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--repo-name", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--host", default="")
    parser.add_argument("--protocol", default="")
    parser.add_argument("--organization", default="")
    parser.add_argument("--repo-prefix", default="")
    parser.add_argument("--username", default="")
    parser.add_argument("--token-env", default="")
    parser.add_argument("--test-repo-url", default="")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def default_output_dir(source_env, git_config, client_slug=""):
    org = git_config.get("organization", "") or "local"
    return state_root(resolve_client_slug(client_slug, source_env=source_env)) / "client_git_exports" / org / source_env


def destination_export_repo_name(source_env, git_config, explicit_repo_name=""):
    if explicit_repo_name:
        return explicit_repo_name.strip()
    prefix = git_config.get("repo_prefix", "").strip("-_")
    base = f"aws-backup-{source_env}"
    return f"{prefix}-{base}" if prefix else base


def destination_export_repo_url(source_env, git_config, explicit_repo_name=""):
    organization = git_config.get("organization", "")
    if not organization:
        return ""
    protocol = git_config.get("protocol", "https")
    host = git_config.get("host", "github.com")
    repo_name = destination_export_repo_name(source_env, git_config, explicit_repo_name)
    if protocol == "ssh":
        return f"git@{host}:{organization}/{repo_name}.git"
    return f"https://{host}/{organization}/{repo_name}.git"


def build_index(snapshot, risk_report, summary):
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_env": snapshot.get("source_env", ""),
        "account_id": snapshot.get("account_id", ""),
        "region": snapshot.get("region", ""),
        "summary_counts": summary.get("counts", {}),
        "risk_summary": risk_report.get("summary", {}),
        "files": {
            "snapshot": "snapshots/source_snapshot.json",
            "summary": "snapshots/summary.json",
            "dependency_graph": "snapshots/dependency_graph.json",
            "risk_report": "reports/risk_report.json",
            "deployment_manifest": "reports/deployment_manifest.json",
        },
    }


SENSITIVE_EXPORT_KEYS = {
    "AccessKeyId",
    "SecretAccessKey",
    "SessionToken",
    "CloudTrailEvent",
    "Authorization",
    "Password",
    "SecretString",
    "SecretBinary",
}


def sanitize_for_export(value):
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if key in SENSITIVE_EXPORT_KEYS:
                sanitized[key] = "[REDACTED]"
                continue
            sanitized[key] = sanitize_for_export(item)
        return sanitized

    if isinstance(value, list):
        return [sanitize_for_export(item) for item in value]

    return value


def ensure_git_repo(output_dir):
    git_dir = output_dir / ".git"
    if git_dir.exists():
        return {"status": "existing"}
    completed = subprocess.run(["git", "init", str(output_dir)], capture_output=True, text=True)
    return {
        "status": "ok" if completed.returncode == 0 else "failed",
        "details": (completed.stderr or completed.stdout).strip(),
    }


def git_commit_all(output_dir, source_env):
    subprocess.run(["git", "-C", str(output_dir), "add", "."], capture_output=True, text=True)
    completed = subprocess.run(
        ["git", "-C", str(output_dir), "commit", "-m", f"AWS backup export for {source_env}"],
        capture_output=True,
        text=True,
    )
    details = (completed.stderr or completed.stdout).strip()
    if completed.returncode != 0 and "nothing to commit" in details.lower():
        return {"status": "skipped", "details": "Nothing to commit"}
    return {"status": "ok" if completed.returncode == 0 else "failed", "details": details}


def git_push_all(output_dir, remote_url, git_config):
    if not remote_url:
        return {"status": "skipped", "details": "No destination URL configured"}
    subprocess.run(["git", "-C", str(output_dir), "remote", "remove", "client"], capture_output=True, text=True)
    subprocess.run(["git", "-C", str(output_dir), "remote", "add", "client", remote_url], capture_output=True, text=True)
    completed = subprocess.run(
        git_command_with_auth(["git", "-C", str(output_dir), "push", "-u", "client", "HEAD"], git_config, remote_url),
        capture_output=True,
        text=True,
        env=git_auth_env(remote_url, git_config),
    )
    return {
        "status": "ok" if completed.returncode == 0 else "failed",
        "details": (completed.stderr or completed.stdout).strip(),
    }


def main():
    args = parse_args()
    source_env = args.source_env or "full-account-scan"
    config = load_transfer_config(args.config)
    client_slug = resolve_client_slug(args.client_slug, config, source_env=source_env)
    config = apply_git_backup_overrides(config, {
        "provider": args.provider,
        "host": args.host,
        "protocol": args.protocol,
        "organization": args.organization,
        "repo_prefix": args.repo_prefix,
        "username": args.username,
        "token_env": args.token_env,
        "test_repo_url": args.test_repo_url,
    })
    git_config = git_backup_config(config)

    inventory_dir = inventory_dir_path(source_env, client_slug=client_slug)
    deployment_dir = deployment_dir_path(source_env, client_slug=client_slug)

    snapshot = json.loads((inventory_dir / "source_snapshot.json").read_text(encoding="utf-8"))
    summary = json.loads((inventory_dir / "summary.json").read_text(encoding="utf-8"))
    dependency_graph = json.loads((inventory_dir / "dependency_graph.json").read_text(encoding="utf-8"))
    risk_report_path = inventory_dir / "risk_report.json"
    risk_report = json.loads(risk_report_path.read_text(encoding="utf-8")) if risk_report_path.exists() else {"summary": {}, "findings": []}
    export_snapshot = sanitize_for_export(snapshot)
    export_dependency_graph = sanitize_for_export(dependency_graph)

    output_dir = Path(args.output_dir) if args.output_dir else default_output_dir(source_env, git_config, client_slug)
    snapshots_dir = output_dir / "snapshots"
    reports_dir = output_dir / "reports"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    (snapshots_dir / "source_snapshot.json").write_text(json.dumps(export_snapshot, indent=2, default=str), encoding="utf-8")
    (snapshots_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    (snapshots_dir / "dependency_graph.json").write_text(json.dumps(export_dependency_graph, indent=2, default=str), encoding="utf-8")
    (reports_dir / "risk_report.json").write_text(json.dumps(risk_report, indent=2, default=str), encoding="utf-8")

    deployment_manifest_path = deployment_dir / "deployment_manifest.json"
    if deployment_manifest_path.exists():
        (reports_dir / "deployment_manifest.json").write_text(
            deployment_manifest_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    index = build_index(export_snapshot, risk_report, summary)
    (output_dir / "README.json").write_text(json.dumps(index, indent=2, default=str), encoding="utf-8")

    git_init = {"status": "skipped", "details": "Git init not requested"}
    if args.init_git or args.commit:
        git_init = ensure_git_repo(output_dir)

    git_commit = {"status": "skipped", "details": "Commit not requested"}
    if args.commit:
        git_commit = git_commit_all(output_dir, source_env)

    remote_url = destination_export_repo_url(source_env, git_config, args.repo_name)
    git_push = {"status": "skipped", "details": "Push not requested"}
    if args.push:
        git_push = git_push_all(output_dir, remote_url, git_config)

    print(json.dumps({
        "status": "ok",
        "output_dir": str(output_dir),
        "remote_url": remote_url,
        "git_init": git_init,
        "git_commit": git_commit,
        "git_push": git_push,
    }, indent=2))


if __name__ == "__main__":
    main()
