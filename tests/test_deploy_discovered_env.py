import unittest

from executor.scripts.deploy_discovered_env import (
    role_allows_lambda_assume,
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

    def test_update_env_values_replaces_env_and_team_placeholder(self):
        variables = {
            "BASE_URL": "https://legacy.example.com",
            "TEAM_NAME": "{team}",
            "UNCHANGED": "value",
        }

        self.assertEqual(
            update_env_values(variables, "legacy", "virgin", "payments"),
            {
                "BASE_URL": "https://virgin.example.com",
                "TEAM_NAME": "payments",
                "UNCHANGED": "value",
            },
        )


if __name__ == "__main__":
    unittest.main()
