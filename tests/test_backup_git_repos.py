import os
import unittest

from executor.scripts.backup_git_repos import build_backup_manifest, build_direct_repo_entry, destination_repo_name, destination_repo_url
from executor.scripts.transfer_common import apply_git_backup_overrides, authenticated_git_url, git_auth_env, git_command_with_auth


class BackupGitReposTests(unittest.TestCase):

    def test_destination_repo_name_applies_prefix(self):
        git_config = {"repo_prefix": "backup"}
        repo = {"name": "Legacy-Service"}

        self.assertEqual(destination_repo_name(repo, git_config), "backup-legacy-service")

    def test_destination_repo_url_supports_https(self):
        git_config = {
            "protocol": "https",
            "host": "github.com",
            "organization": "client-org",
            "repo_prefix": "backup",
        }
        repo = {"name": "legacy-service"}

        self.assertEqual(
            destination_repo_url(repo, git_config),
            "https://github.com/client-org/backup-legacy-service.git",
        )

    def test_build_backup_manifest_adds_destination_and_commands(self):
        snapshot = {
            "source_env": "legacy",
            "account_id": "123456789012",
            "region": "us-east-1",
            "git_repositories": [
                {
                    "url": "https://github.com/example/legacy-service.git",
                    "host": "github.com",
                    "name": "legacy-service",
                    "sources": [{"type": "lambda-env", "name": "legacy-worker"}],
                }
            ],
        }
        git_config = {
            "provider": "github",
            "host": "github.com",
            "protocol": "https",
            "organization": "client-org",
            "repo_prefix": "backup",
        }

        manifest = build_backup_manifest(snapshot, git_config, "legacy")

        self.assertEqual(manifest["repository_count"], 1)
        self.assertEqual(
            manifest["repositories"][0]["destination_url"],
            "https://github.com/client-org/backup-legacy-service.git",
        )
        self.assertTrue(any("git clone --mirror" in cmd for cmd in manifest["repositories"][0]["commands"] if cmd))

    def test_authenticated_git_url_leaves_url_unmodified(self):
        os.environ["CLIENT_GIT_TOKEN"] = "secret-token"
        self.addCleanup(lambda: os.environ.pop("CLIENT_GIT_TOKEN", None))

        git_config = {
            "protocol": "https",
            "username": "octocat",
            "token_env": "CLIENT_GIT_TOKEN",
        }

        self.assertEqual(
            authenticated_git_url("https://github.com/client-org/repo.git", git_config),
            "https://github.com/client-org/repo.git",
        )

    def test_build_direct_repo_entry_infers_name(self):
        entry = build_direct_repo_entry("https://github.com/example/aws-dev-agent-backup-test.git")

        self.assertEqual(entry["name"], "aws-dev-agent-backup-test")
        self.assertEqual(entry["sources"][0]["type"], "direct-input")

    def test_apply_git_backup_overrides_merges_cli_values(self):
        config = {"git_backup": {"organization": "old-org", "protocol": "https"}}

        merged = apply_git_backup_overrides(config, {"organization": "new-org", "repo_prefix": "backup"})

        self.assertEqual(merged["git_backup"]["organization"], "new-org")
        self.assertEqual(merged["git_backup"]["repo_prefix"], "backup")
        self.assertEqual(merged["git_backup"]["protocol"], "https")

    def test_git_auth_env_builds_askpass_environment(self):
        os.environ["CLIENT_GIT_TOKEN"] = "secret-token"
        self.addCleanup(lambda: os.environ.pop("CLIENT_GIT_TOKEN", None))

        env = git_auth_env("https://github.com/client-org/repo.git", {
            "protocol": "https",
            "username": "octocat",
            "token_env": "CLIENT_GIT_TOKEN",
        })

        self.assertEqual(env["AWS_DEV_AGENT_GIT_USERNAME"], "octocat")
        self.assertEqual(env["AWS_DEV_AGENT_GIT_TOKEN"], "secret-token")
        self.assertTrue(env["GIT_ASKPASS"].endswith("git_askpass.cmd"))

    def test_git_command_with_auth_keeps_args_clean(self):
        os.environ["CLIENT_GIT_TOKEN"] = "secret-token"
        self.addCleanup(lambda: os.environ.pop("CLIENT_GIT_TOKEN", None))

        command = git_command_with_auth(["git", "ls-remote", "https://github.com/client-org/repo.git"], {
            "protocol": "https",
            "username": "octocat",
            "token_env": "CLIENT_GIT_TOKEN",
        }, "https://github.com/client-org/repo.git")

        self.assertEqual(command[0], "git")
        self.assertEqual(command[1], "ls-remote")
        self.assertNotIn("secret-token", " ".join(command))


if __name__ == "__main__":
    unittest.main()
