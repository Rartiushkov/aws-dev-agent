import unittest

from runner.runner import Runner


class FakeLogScanner:

    def __init__(self, logs_by_name):
        self.logs_by_name = logs_by_name

    def scan_lambda_logs(self, lambda_name):
        return self.logs_by_name.get(lambda_name, "")


class RunnerCloudWatchTests(unittest.TestCase):

    def test_inspect_cloudwatch_errors_creates_findings(self):
        runner = Runner()
        runner.log_scanner = FakeLogScanner(
            {
                "good-fn": "all good",
                "bad-fn": "Runtime error: AccessDenied on dependency",
            }
        )

        findings = runner.inspect_cloudwatch_errors({"lambdas": ["good-fn", "bad-fn"]})

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["lambda"], "bad-fn")
        self.assertEqual(findings[0]["source"], "cloudwatch")


if __name__ == "__main__":
    unittest.main()
