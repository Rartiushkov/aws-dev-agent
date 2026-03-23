import unittest

from cli.agent import format_memory_matches
from executor.command_runner import tokenize_command


class CliAgentTests(unittest.TestCase):

    def test_format_memory_matches_includes_resolution_and_counts(self):
        matches = [{
            "summary": "ecs service failed because awslogs region stayed in source region",
            "last_resolution": "rewrite awslogs-region to target region",
            "occurrences": 3,
            "validated_fix_count": 2,
        }]
        formatted = format_memory_matches(matches)
        self.assertIn("awslogs region", formatted)
        self.assertIn("rewrite awslogs-region", formatted)
        self.assertIn("seen=3", formatted)
        self.assertIn("validated=2", formatted)

    def test_format_memory_matches_handles_empty_resolution(self):
        matches = [{
            "summary": "lambda mapping disabled by user initiated state",
            "last_resolution": "",
            "occurrences": 1,
            "validated_fix_count": 0,
        }]
        formatted = format_memory_matches(matches)
        self.assertIn("lambda mapping disabled", formatted)
        self.assertIn("seen=1", formatted)

    def test_tokenize_command_rejects_shell_separators(self):
        with self.assertRaises(ValueError):
            tokenize_command("aws iam list-roles; whoami")

    def test_tokenize_command_accepts_simple_aws_command(self):
        tokens = tokenize_command('aws iam list-roles --query "Roles[].RoleName"')
        self.assertEqual(tokens[0], "aws")
        self.assertIn("--query", tokens)


if __name__ == "__main__":
    unittest.main()
