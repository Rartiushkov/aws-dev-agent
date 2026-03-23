import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from executor.scripts.transfer_common import (
    apply_git_backup_overrides,
    git_auth_env,
    git_command_with_auth,
    git_backup_config,
    inventory_dir_path,
    load_transfer_config,
    migration_dir_path,
    resolve_client_slug,
)


def sanitize_repo_name(value):
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.strip().lower())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-_")


def destination_repo_name(repo, git_config):
    prefix = sanitize_repo_name(git_config.get("repo_prefix", ""))
    base = sanitize_repo_name(repo.get("name", "repo"))
    return f"{prefix}-{base}" if prefix else base


def destination_repo_url(repo, git_config):
    protocol = git_config.get("protocol", "https")
    host = git_config.get("host", "github.com")
    organization = git_config.get("organization", "")
    repo_name = destination_repo_name(repo, git_config)
    if protocol == "ssh":
        return f"git@{host}:{organization}/{repo_name}.git"
    return f"https://{host}/{organization}/{repo_name}.git"


def build_direct_repo_entry(repo_url, repo_name=""):
    parsed = urlparse(repo_url.replace("git@", "ssh://git@") if repo_url.startswith("git@") else repo_url)
    inferred_name = repo_name or (parsed.path.rsplit("/", 1)[-1] if parsed.path else "repo")
    inferred_name = inferred_name.removesuffix(".git")
    return {
        "url": repo_url,
        "host": parsed.hostname or "",
        "name": inferred_name,
        "sources": [{"type": "direct-input", "name": inferred_name}],
    }


def build_backup_manifest(snapshot, git_config, source_env, client_slug=""):
    repositories = snapshot.get("git_repositories", [])
    manifest = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source_env": snapshot.get("source_env", source_env),
        "account_id": snapshot.get("account_id", ""),
        "region": snapshot.get("region", ""),
        "git_provider": git_config.get("provider", ""),
        "git_host": git_config.get("host", ""),
        "git_organization": git_config.get("organization", ""),
        "repository_count": len(repositories),
        "repositories": [],
    }
    for repo in repositories:
        repo_name = destination_repo_name(repo, git_config)
        destination = destination_repo_url(repo, git_config) if git_config.get("organization") else ""
        base_root = Path("state") / "git_backups"
        if client_slug:
            base_root = Path("state") / "clients" / sanitize_repo_name(client_slug) / "git_backups"
        local_mirror = str((base_root / source_env / f"{repo_name}.git").as_posix())
        manifest["repositories"].append({
            "url": repo.get("url"),
            "host": repo.get("host"),
            "name": repo.get("name"),
            "sources": repo.get("sources", []),
            "backup_mode": "mirror",
            "destination_repo_name": repo_name,
            "destination_url": destination,
            "local_mirror_path": local_mirror,
            "commands": [
                f"git clone --mirror {repo.get('url')} {local_mirror}",
                f"git -C {local_mirror} remote add client {destination}" if destination else "",
                f"git -C {local_mirror} push --mirror client" if destination else "",
            ],
        })
    return manifest


def execute_backup_manifest(manifest):
    results = []
    git_config = manifest.get("git_backup", {})
    for repo in manifest.get("repositories", []):
        local_mirror = Path(repo["local_mirror_path"])
        local_mirror.parent.mkdir(parents=True, exist_ok=True)
        clone_cmd = ["git", "clone", "--mirror", repo["url"], str(local_mirror)]
        remote_name = "client"
        if local_mirror.exists():
            clone_result = {"status": "skipped", "details": "Local mirror already exists"}
        else:
            completed = subprocess.run(clone_cmd, capture_output=True, text=True)
            clone_result = {"status": "ok" if completed.returncode == 0 else "failed", "details": (completed.stderr or completed.stdout).strip()}
            if completed.returncode != 0:
                results.append({"repository": repo["name"], "clone": clone_result})
                continue
        push_result = {"status": "skipped", "details": "No destination URL configured"}
        if repo.get("destination_url"):
            subprocess.run(["git", "-C", str(local_mirror), "remote", "remove", remote_name], capture_output=True, text=True)
            subprocess.run(["git", "-C", str(local_mirror), "remote", "add", remote_name, repo["destination_url"]], capture_output=True, text=True)
            completed = subprocess.run(
                git_command_with_auth(["git", "-C", str(local_mirror), "push", "--mirror", remote_name], git_config, repo["destination_url"]),
                capture_output=True,
                text=True,
                env=git_auth_env(repo["destination_url"], git_config),
            )
            push_result = {"status": "ok" if completed.returncode == 0 else "failed", "details": (completed.stderr or completed.stdout).strip()}
        results.append({"repository": repo["name"], "clone": clone_result, "push": push_result})
    return results


def parse_args():
    parser = argparse.ArgumentParser(description="Create a Git backup manifest from a discovered AWS snapshot.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--repo-url", default="")
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


def main():
    args = parse_args()
    source_env = args.source_env or "full-account-scan"
    config = load_transfer_config(args.config)
    client_slug = resolve_client_slug(args.client_slug, config, source_env=source_env)
    base_dir = inventory_dir_path(source_env, client_slug=client_slug)
    manifest_path = base_dir / "git_backup_manifest.json"
    if args.repo_url:
        snapshot = {
            "source_env": source_env,
            "account_id": "",
            "region": "",
            "git_repositories": [build_direct_repo_entry(args.repo_url, args.repo_name)],
        }
    else:
        snapshot_path = base_dir / "source_snapshot.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
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
    manifest = build_backup_manifest(snapshot, git_config, source_env, client_slug=client_slug)
    snapshot_dir = migration_dir_path(source_env, client_slug) / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    manifest["git_backup"] = {
        "provider": git_config.get("provider", ""),
        "host": git_config.get("host", ""),
        "protocol": git_config.get("protocol", ""),
        "organization": git_config.get("organization", ""),
        "repo_prefix": git_config.get("repo_prefix", ""),
        "create_repos": git_config.get("create_repos", False),
        "username": git_config.get("username", ""),
        "token_env": git_config.get("token_env", ""),
    }
    execution_results = execute_backup_manifest(manifest) if args.execute else []
    if execution_results:
        manifest["execution_results"] = execution_results
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    archived_manifest = snapshot_dir / "git_backup_manifest_latest.json"
    archived_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(json.dumps({
        "status": "ok",
        "manifest_path": str(manifest_path),
        "repository_count": manifest["repository_count"],
        "executed": args.execute,
    }, indent=2))


if __name__ == "__main__":
    main()
