import json
import shutil
import unittest
from pathlib import Path

from executor.scripts.pre_migration_snapshot import build_snapshot_manifest


class PreMigrationSnapshotTests(unittest.TestCase):

    def setUp(self):
        self.client_root = Path("state") / "clients" / "test-client-snapshot"
        self.inventory_dir = self.client_root / "aws_inventory" / "legacy"
        self.inventory_dir.mkdir(parents=True, exist_ok=True)
        (self.inventory_dir / "source_snapshot.json").write_text(json.dumps({
            "source_env": "legacy",
            "account_id": "123456789012",
            "region": "us-east-1",
            "git_repositories": [{"url": "https://github.com/example/repo.git", "name": "repo"}],
        }), encoding="utf-8")
        (self.inventory_dir / "summary.json").write_text(json.dumps({"counts": {"lambda_functions": 1}}), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.client_root, ignore_errors=True)

    def test_build_snapshot_manifest_writes_client_scoped_files(self):
        result = build_snapshot_manifest(
            source_env="legacy",
            target_env="roman-art",
            client_slug="test-client-snapshot",
        )

        self.assertEqual(result["client_slug"], "test-client-snapshot")
        self.assertEqual(result["git_repository_count"], 1)
        self.assertTrue(Path(result["report_path"]).exists())
        self.assertTrue(Path(result["git_manifest_path"]).exists())


if __name__ == "__main__":
    unittest.main()
