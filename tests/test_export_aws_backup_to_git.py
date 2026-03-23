import json
import unittest
from pathlib import Path
import shutil

from executor.scripts.export_aws_backup_to_git import (
    build_index,
    destination_export_repo_name,
    destination_export_repo_url,
    sanitize_for_export,
)


class ExportAwsBackupToGitTests(unittest.TestCase):

    def test_build_index_contains_expected_file_map(self):
        snapshot = {"source_env": "legacy", "account_id": "123", "region": "us-east-1"}
        risk_report = {"summary": {"high": 1, "medium": 2, "low": 3}}
        summary = {"counts": {"lambda_functions": 2}}

        index = build_index(snapshot, risk_report, summary)

        self.assertEqual(index["source_env"], "legacy")
        self.assertEqual(index["risk_summary"]["high"], 1)
        self.assertEqual(index["summary_counts"]["lambda_functions"], 2)
        self.assertIn("snapshots/source_snapshot.json", index["files"]["snapshot"])

    def test_export_layout_example_files_can_be_written(self):
        output_dir = Path("state") / "test_export_layout"
        if output_dir.exists():
            shutil.rmtree(output_dir)
        self.addCleanup(lambda: shutil.rmtree(output_dir, ignore_errors=True))

        snapshots_dir = output_dir / "snapshots"
        reports_dir = output_dir / "reports"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        reports_dir.mkdir(parents=True, exist_ok=True)

        (snapshots_dir / "summary.json").write_text(json.dumps({"counts": {"x": 1}}), encoding="utf-8")
        (reports_dir / "risk_report.json").write_text(json.dumps({"summary": {"high": 0}}), encoding="utf-8")

        self.assertTrue((snapshots_dir / "summary.json").exists())
        self.assertTrue((reports_dir / "risk_report.json").exists())

    def test_sanitize_for_export_redacts_sensitive_fields(self):
        source = {
            "AccessKeyId": "AKIAEXAMPLE",
            "CloudTrailEvent": "{\"secret\":true}",
            "nested": {
                "SecretString": "top-secret",
                "safe": "value",
            },
            "items": [
                {"SessionToken": "token"},
                {"name": "ok"},
            ],
        }

        sanitized = sanitize_for_export(source)

        self.assertEqual(sanitized["AccessKeyId"], "[REDACTED]")
        self.assertEqual(sanitized["CloudTrailEvent"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["SecretString"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["safe"], "value")
        self.assertEqual(sanitized["items"][0]["SessionToken"], "[REDACTED]")
        self.assertEqual(sanitized["items"][1]["name"], "ok")

    def test_destination_export_repo_url_uses_prefix_and_org(self):
        git_config = {"organization": "client-org", "repo_prefix": "backup", "host": "github.com", "protocol": "https"}

        self.assertEqual(destination_export_repo_name("legacy", git_config), "backup-aws-backup-legacy")
        self.assertEqual(
            destination_export_repo_url("legacy", git_config),
            "https://github.com/client-org/backup-aws-backup-legacy.git",
        )


if __name__ == "__main__":
    unittest.main()
