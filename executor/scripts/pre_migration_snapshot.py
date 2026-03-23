import json
from datetime import datetime, timezone
from pathlib import Path

from executor.scripts.backup_git_repos import build_backup_manifest
from executor.scripts.transfer_common import (
    git_backup_config,
    inventory_dir_path,
    load_transfer_config,
    migration_dir_path,
    resolve_client_slug,
)


def load_json(path):
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def build_snapshot_manifest(*, source_env="", inventory_key="", target_env="", client_slug="", config_path=""):
    config = load_transfer_config(config_path)
    resolved_client_slug = resolve_client_slug(client_slug, config, source_env=source_env, target_env=target_env)
    inventory_dir = inventory_dir_path(source_env or "full-account-scan", inventory_key, resolved_client_slug)
    snapshot = load_json(inventory_dir / "source_snapshot.json")
    summary = load_json(inventory_dir / "summary.json")
    git_manifest = build_backup_manifest(snapshot, git_backup_config(config), source_env or "full-account-scan")
    report = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "client_slug": resolved_client_slug,
        "source_env": source_env or "full-account-scan",
        "inventory_key": inventory_key or "",
        "target_env": target_env or "",
        "inventory_dir": str(inventory_dir),
        "source_snapshot_present": bool(snapshot),
        "summary_present": bool(summary),
        "git_repository_count": git_manifest.get("repository_count", 0),
        "snapshot_files": {
            "source_snapshot": str(inventory_dir / "source_snapshot.json"),
            "summary": str(inventory_dir / "summary.json"),
            "git_backup_manifest": "",
        },
    }
    snapshot_dir = migration_dir_path(target_env or resolved_client_slug, resolved_client_slug) / "snapshots"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest_path = snapshot_dir / f"pre_migration_snapshot_{timestamp}.json"
    git_manifest_path = snapshot_dir / f"git_backup_manifest_{timestamp}.json"
    write_json(git_manifest_path, git_manifest)
    report["snapshot_files"]["git_backup_manifest"] = str(git_manifest_path)
    write_json(manifest_path, report)
    return {
        "client_slug": resolved_client_slug,
        "report_path": str(manifest_path),
        "git_manifest_path": str(git_manifest_path),
        "git_repository_count": git_manifest.get("repository_count", 0),
    }
