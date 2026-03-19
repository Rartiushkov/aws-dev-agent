import unittest

from executor.scripts.deploy_discovered_env import (
    queue_target_name,
    rewrite_string_value,
    role_allows_lambda_assume,
    should_skip_recloning,
    target_name,
    update_env_values,
)


class DeployDiscoveredEnvTests(unittest.TestCase):

    def test_role_allows_lambda_assume_when_trust_policy_matches(self):
        document = {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ]
        }

        self.assertTrue(role_allows_lambda_assume(document))

    def test_role_allows_lambda_assume_rejects_non_lambda_service(self):
        document = {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ]
        }

        self.assertFalse(role_allows_lambda_assume(document))

    def test_target_name_rewrites_source_and_adds_team(self):
        self.assertEqual(
            target_name("legacy-worker", "legacy", "virgin", "payments"),
            "virgin-worker-payments",
        )

    def test_queue_target_name_preserves_fifo_suffix(self):
        self.assertEqual(
            queue_target_name("legacy-events.fifo", "legacy", "virgin", "payments"),
            "virgin-events-payments.fifo",
        )

    def test_should_skip_recloning_for_existing_target_resources(self):
        self.assertTrue(should_skip_recloning("virgin-worker-platform", "virgin", "platform"))
        self.assertFalse(should_skip_recloning("legacy-worker", "virgin", "platform"))

    def test_rewrite_string_value_updates_dependency_links(self):
        mappings = {
            "queue_urls": {
                "https://sqs.us-east-1.amazonaws.com/123/legacy-events": "https://sqs.us-east-1.amazonaws.com/123/virgin-events"
            },
            "queue_arns": {},
            "topic_arns": {},
            "function_arns": {},
            "role_arns": {},
        }
        self.assertEqual(
            rewrite_string_value(
                "https://sqs.us-east-1.amazonaws.com/123/legacy-events",
                mappings,
                "legacy",
                "virgin",
                "payments",
            ),
            "https://sqs.us-east-1.amazonaws.com/123/virgin-events",
        )

    def test_update_env_values_replaces_env_and_team_placeholder(self):
        variables = {
            "BASE_URL": "https://legacy.example.com",
            "TEAM_NAME": "{team}",
            "UNCHANGED": "value",
        }
        mappings = {
            "queue_urls": {},
            "queue_arns": {},
            "topic_arns": {},
            "function_arns": {},
            "role_arns": {},
        }

        self.assertEqual(
            update_env_values(variables, mappings, "legacy", "virgin", "payments"),
            {
                "BASE_URL": "https://virgin.example.com",
                "TEAM_NAME": "payments",
                "UNCHANGED": "value",
            },
        )


if __name__ == "__main__":
    unittest.main()
