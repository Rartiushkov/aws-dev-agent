import unittest

from executor.scripts.build_client_migration_report import build_report


class BuildClientMigrationReportTests(unittest.TestCase):

    def test_build_report_summarizes_status(self):
        report = build_report(
            "roma-art",
            {"roles": [1], "sqs_queues": [1], "dynamodb_tables": [1], "lambda_functions": [1], "ecs_services": [1], "codebuild_projects": [1]},
            {"issues_found": False, "smoke_checks": [{"name": "a", "status": "ok"}]},
            {"results": [{"import_required": True, "operation": "skipped"}]},
            {"results": [{"operation": "imported"}]},
        )

        self.assertEqual(report["outcome"], "ready")
        self.assertEqual(report["summary"]["cloudformation"]["imported"], 1)
        self.assertEqual(report["summary"]["validation"]["passed_checks"], 1)


if __name__ == "__main__":
    unittest.main()
