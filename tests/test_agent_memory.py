import json
import unittest
from pathlib import Path
import shutil

from executor.scripts.agent_memory import find_similar_incidents, load_incidents, memory_store_path, record_incident


class AgentMemoryTests(unittest.TestCase):

    def setUp(self):
        self.base_dir = Path("state") / "test_agent_memory"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.base_dir, ignore_errors=True)

    def test_record_incident_deduplicates_and_counts_occurrences(self):
        path = str(self.base_dir / "incidents.json")
        record_incident("aws-cli-error", "An error occurred for arn:aws:sqs:us-east-2:123456789012:q", path=path)
        record_incident("aws-cli-error", "An error occurred for arn:aws:sqs:us-east-2:999999999999:q", path=path)
        incidents = load_incidents(path)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["occurrences"], 2)

    def test_record_incident_tracks_resolution_and_validation(self):
        path = str(self.base_dir / "incidents.json")
        record_incident(
            "validation-smoke-check-issue",
            "ecs-services-steady: running=0 desired=1",
            path=path,
            resolution="fixed by updating logs region",
            validated=True,
        )
        incidents = load_incidents(path)
        self.assertEqual(incidents[0]["validated_fix_count"], 1)
        self.assertIn("fixed by updating logs region", incidents[0]["last_resolution"])

    def test_find_similar_incidents_returns_ranked_matches(self):
        path = str(self.base_dir / "incidents.json")
        record_incident(
            "validation-smoke-check-issue",
            "ecs service failed because awslogs region stayed in source region",
            path=path,
            tags=["ecs", "validation"],
            resolution="rewrite awslogs-region to target region",
            validated=True,
        )
        record_incident(
            "validation-smoke-check-issue",
            "lambda mapping disabled by user initiated state",
            path=path,
            tags=["lambda", "validation"],
        )
        matches = find_similar_incidents("ecs awslogs region problem", path=path)
        self.assertGreaterEqual(len(matches), 1)
        self.assertIn("awslogs region", matches[0]["summary"])

    def test_default_memory_path_is_client_scoped(self):
        path = memory_store_path(client_slug="client-a")
        self.assertEqual(path, Path("state") / "clients" / "client-a" / "agent_memory" / "incidents.json")


if __name__ == "__main__":
    unittest.main()
