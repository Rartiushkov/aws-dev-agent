import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from executor.scripts.transfer_common import (
    apply_git_backup_overrides,
    git_auth_env,
    git_command_with_auth,
    git_backup_config,
    load_transfer_config,
)


def parse_args():
    parser = argparse.ArgumentParser(description="Test access to a client Git destination.")
    parser.add_argument("--config", default="")
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
    config = load_transfer_config(args.config)
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
    test_repo = git_config.get("test_repo_url")
    if not test_repo:
        organization = git_config.get("organization", "")
        host = git_config.get("host", "github.com")
        protocol = git_config.get("protocol", "https")
        if not organization:
            raise SystemExit("git_backup.organization or git_backup.test_repo_url is required")
        test_repo = f"https://{host}/{organization}/.git" if protocol != "ssh" else f"git@{host}:{organization}/.git"
    completed = subprocess.run(
        git_command_with_auth(["git", "ls-remote", test_repo], git_config, test_repo),
        capture_output=True,
        text=True,
        env=git_auth_env(test_repo, git_config),
    )
    print(json.dumps({
        "status": "ok" if completed.returncode == 0 else "failed",
        "test_repo_url": git_config.get("test_repo_url") or test_repo,
        "details": (completed.stderr or completed.stdout).strip(),
    }, indent=2))
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
