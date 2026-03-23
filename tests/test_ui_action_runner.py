import io
import unittest

from executor.ui_action_runner import build_script_invocation, run_create_lambda_action, run_ui_action


class FakeWaiter:
    def wait(self, **kwargs):
        self.kwargs = kwargs


class FakeLambdaClient:
    def __init__(self):
        self.calls = []

    def create_function(self, **kwargs):
        self.calls.append(("create_function", kwargs))

    def get_waiter(self, name):
        self.calls.append(("get_waiter", name))
        return FakeWaiter()

    def update_function_code(self, **kwargs):
        self.calls.append(("update_function_code", kwargs))

    def create_event_source_mapping(self, **kwargs):
        self.calls.append(("create_event_source_mapping", kwargs))

    def get_function(self, **kwargs):
        self.calls.append(("get_function", kwargs))
        return {"Configuration": {"FunctionArn": "arn:aws:lambda:us-east-1:123456789012:function:test-fn"}}

    def add_permission(self, **kwargs):
        self.calls.append(("add_permission", kwargs))

    def invoke(self, **kwargs):
        self.calls.append(("invoke", kwargs))
        return {"StatusCode": 200, "Payload": io.BytesIO(b"{\"ok\":true}")}


class FakeEventsClient:
    def __init__(self):
        self.calls = []

    def put_rule(self, **kwargs):
        self.calls.append(("put_rule", kwargs))
        return {"RuleArn": "arn:aws:events:us-east-1:123456789012:rule/test-rule"}

    def put_targets(self, **kwargs):
        self.calls.append(("put_targets", kwargs))


class UiActionRunnerTests(unittest.TestCase):

    def test_build_script_invocation_avoids_shell_strings(self):
        command = build_script_invocation("deploy-environment", {
            "source_env": "legacy",
            "target_env": "virgin",
            "region": "us-east-2",
            "team": "platform",
        })

        self.assertEqual(command[0], "python")
        self.assertIn("executor/scripts/deploy_discovered_env.py", command[1])
        self.assertIn("--source-env", command)
        self.assertIn("legacy", command)

    def test_run_create_lambda_action_supports_sqs_trigger(self):
        lambda_client = FakeLambdaClient()
        result = run_create_lambda_action({
            "function_name": "orders-worker",
            "template_id": "sqs-consumer",
            "trigger_type": "sqs",
            "trigger_source": "arn:aws:sqs:us-east-1:123456789012:orders",
            "include_test": True,
        }, lambda_client=lambda_client, events_client=FakeEventsClient())

        self.assertEqual(result["status"], "ok")
        self.assertTrue(any(call[0] == "create_event_source_mapping" for call in lambda_client.calls))
        self.assertTrue(any(call[0] == "invoke" for call in lambda_client.calls))

    def test_run_create_lambda_action_supports_schedule_trigger(self):
        lambda_client = FakeLambdaClient()
        events_client = FakeEventsClient()
        result = run_create_lambda_action({
            "function_name": "nightly-job",
            "template_id": "scheduled-task",
            "trigger_type": "schedule",
            "trigger_source": "rate(5 minutes)",
            "include_test": False,
        }, lambda_client=lambda_client, events_client=events_client)

        self.assertEqual(result["trigger_type"], "schedule")
        self.assertTrue(any(call[0] == "put_rule" for call in events_client.calls))
        self.assertTrue(any(call[0] == "add_permission" for call in lambda_client.calls))

    def test_run_ui_action_requires_approval_for_writes(self):
        result = run_ui_action("destroy-environment", {"target_env": "virgin"})
        self.assertEqual(result["status"], "approval_required")

    def test_build_script_invocation_supports_cost_brain(self):
        command = build_script_invocation("analyze-cost-brain", {
            "source_env": "full-account-scan",
            "region": "us-east-1",
            "days": "14",
        })

        self.assertEqual(command[0], "python")
        self.assertIn("executor/scripts/analyze_cost_brain.py", command[1])
        self.assertIn("--days", command)

    def test_build_script_invocation_supports_performance_brain(self):
        command = build_script_invocation("analyze-performance-brain", {
            "source_env": "full-account-scan",
            "region": "us-east-1",
            "live_metrics": True,
        })

        self.assertEqual(command[0], "python")
        self.assertIn("executor/scripts/analyze_performance_issues.py", command[1])
        self.assertIn("--live-metrics", command)


if __name__ == "__main__":
    unittest.main()
