import json
import shutil
import unittest
from pathlib import Path

from bridges.ui_actions import build_action_preview, build_action_status, get_ui_action, list_ui_actions


class UiActionsTests(unittest.TestCase):

    def test_catalog_contains_client_ready_actions(self):
        action_ids = {item["id"] for item in list_ui_actions()}

        self.assertIn("deploy-environment", action_ids)
        self.assertIn("destroy-environment", action_ids)
        self.assertIn("export-backup-to-git", action_ids)
        self.assertIn("create-lambda", action_ids)
        self.assertIn("analyze-cost-brain", action_ids)
        self.assertIn("analyze-performance-brain", action_ids)

    def test_deploy_preview_uses_read_only_plan(self):
        preview = build_action_preview("deploy-environment", {
            "source_env": "legacy",
            "target_env": "virgin",
            "team": "platform",
            "region": "us-east-2",
        })

        self.assertEqual(preview["mode"], "preview")
        self.assertIn("--read-only-plan", preview["commands"][0]["cmd"])
        self.assertFalse(preview["approval_required"])

    def test_destroy_apply_requires_approval(self):
        preview = build_action_preview("destroy-environment", {
            "target_env": "virgin",
            "region": "us-east-2",
        }, apply=True)

        self.assertTrue(preview["approval_required"])
        self.assertTrue(preview["destructive"])
        self.assertIn("destroy_deployed_env.py", preview["commands"][0]["cmd"])

    def test_deploy_apply_requires_approval(self):
        preview = build_action_preview("deploy-environment", {
            "source_env": "legacy",
            "target_env": "virgin",
        }, apply=True)

        self.assertTrue(preview["approval_required"])

    def test_export_backup_preview_includes_git_fields(self):
        preview = build_action_preview("export-backup-to-git", {
            "source_env": "legacy",
            "organization": "client-org",
            "repo_prefix": "backup",
            "push": True,
        }, apply=True)

        self.assertIn("--organization client-org", preview["commands"][0]["cmd"])
        self.assertIn("--repo-prefix backup", preview["commands"][0]["cmd"])
        self.assertIn("--push", preview["commands"][0]["cmd"])

    def test_create_lambda_preview_uses_form_values(self):
        preview = build_action_preview("create-lambda", {
            "function_name": "orders-worker",
            "runtime": "python3.12",
            "template_id": "sqs-consumer",
            "trigger_type": "sqs",
            "trigger_source": "arn:aws:sqs:us-east-1:123456789012:orders",
        }, apply=True)
        commands = [item["cmd"] for item in preview["commands"]]

        self.assertTrue(any("--runtime python3.12" in cmd for cmd in commands))
        self.assertTrue(any("create-event-source-mapping" in cmd for cmd in commands))

    def test_invalid_schedule_expression_is_rejected(self):
        with self.assertRaises(ValueError):
            build_action_preview("create-lambda", {
                "function_name": "orders-worker",
                "trigger_type": "schedule",
                "trigger_source": "bad command",
            }, apply=True)

    def test_invalid_git_host_is_rejected(self):
        with self.assertRaises(ValueError):
            build_action_preview("test-git-connection", {
                "host": "evil.example.com",
                "organization": "client",
            })

    def test_build_action_status_reports_existing_deploy_artifacts(self):
        deploy_dir = Path("state") / "clients" / "roman-art" / "deployments" / "ui-status-test"
        deploy_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(deploy_dir, ignore_errors=True))

        (deploy_dir / "deployment_manifest.json").write_text(json.dumps({
            "target_env": "ui-status-test",
            "region": "us-east-2",
            "failures": {"ecs_services": []},
        }), encoding="utf-8")
        (deploy_dir / "validation_report.json").write_text(json.dumps({
            "issues_found": False,
            "smoke_checks": [{"name": "lambda", "status": "ok"}],
        }), encoding="utf-8")

        status = build_action_status("deploy-environment", {"target_env": "ui-status-test", "client_slug": "roman-art"})

        self.assertEqual(status["run_status"], "completed")
        self.assertEqual(status["validation_result"], "passed")

    def test_create_lambda_action_has_structured_fields(self):
        action = get_ui_action("create-lambda")
        field_ids = {field["id"] for field in action["fields"]}

        self.assertIn("runtime", field_ids)
        self.assertIn("template_id", field_ids)
        self.assertIn("iam_scope", field_ids)
        self.assertIn("trigger_type", field_ids)

    def test_analyze_cost_brain_preview_builds_script(self):
        preview = build_action_preview("analyze-cost-brain", {
            "source_env": "full-account-scan",
            "region": "us-east-1",
            "days": "14",
        }, apply=True)

        self.assertIn("analyze_cost_brain.py", preview["commands"][0]["cmd"])
        self.assertIn("--days 14", preview["commands"][0]["cmd"])

    def test_analyze_performance_brain_preview_builds_script(self):
        preview = build_action_preview("analyze-performance-brain", {
            "source_env": "full-account-scan",
            "region": "us-east-1",
            "live_metrics": True,
        }, apply=True)

        self.assertIn("analyze_performance_issues.py", preview["commands"][0]["cmd"])
        self.assertIn("--live-metrics", preview["commands"][0]["cmd"])


if __name__ == "__main__":
    unittest.main()
