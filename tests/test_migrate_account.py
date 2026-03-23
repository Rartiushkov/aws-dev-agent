import unittest

from executor.scripts.discover_aws_environment import signal_resource_count
from executor.scripts.migrate_account import (
    merged_region_config,
    parse_regions,
    region_deployment_key,
    region_inventory_key,
)
from executor.scripts.transfer_common import deployment_dir_name, inventory_dir_name


class MigrateAccountTests(unittest.TestCase):

    def test_signal_resource_count_ignores_network_baseline_only(self):
        counts = {
            "vpcs": 3,
            "subnets": 6,
            "route_tables": 4,
            "lambda_functions": 2,
            "sqs_queues": 1,
        }

        self.assertEqual(signal_resource_count(counts), 3)

    def test_inventory_and_deployment_dir_names_prefer_explicit_keys(self):
        self.assertEqual(inventory_dir_name("legacy", "legacy-us-east-1"), "legacy-us-east-1")
        self.assertEqual(deployment_dir_name("virgin", "virgin-us-east-1"), "virgin-us-east-1")

    def test_region_keys_are_stable(self):
        self.assertEqual(region_inventory_key("legacy", "us-east-1"), "legacy-us-east-1")
        self.assertEqual(region_deployment_key("virgin", "us-east-1", "us-east-1"), "virgin-us-east-1")
        self.assertEqual(region_deployment_key("virgin", "us-east-1", "us-west-2"), "virgin-us-east-1-to-us-west-2")

    def test_parse_regions_accepts_csv(self):
        self.assertEqual(parse_regions("us-east-1, us-west-2"), ["us-east-1", "us-west-2"])
        self.assertEqual(parse_regions(""), [])

    def test_merged_region_config_overrides_regions_only(self):
        base_config = {
            "overrides": {
                "source_region": "eu-central-1",
                "target_region": "eu-west-1",
                "preserve_names": True,
            },
            "source_role_arn": "arn:aws:iam::123:role/source",
        }

        merged = merged_region_config(base_config, "us-east-1", "us-west-2")

        self.assertEqual(merged["overrides"]["source_region"], "us-east-1")
        self.assertEqual(merged["overrides"]["target_region"], "us-west-2")
        self.assertTrue(merged["overrides"]["preserve_names"])
        self.assertEqual(merged["source_role_arn"], "arn:aws:iam::123:role/source")


if __name__ == "__main__":
    unittest.main()
