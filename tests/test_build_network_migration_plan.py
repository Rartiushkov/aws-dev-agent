import unittest

from executor.scripts.build_network_migration_plan import build_network_plan


class BuildNetworkMigrationPlanTests(unittest.TestCase):

    def test_build_network_plan_counts_resources(self):
        snapshot = {
            "source_env": "legacy",
            "region": "us-east-1",
            "vpcs": [{}],
            "subnets": [{}, {}],
            "route_tables": [{}],
            "security_groups": [{}, {}, {}],
        }

        plan = build_network_plan(snapshot)

        self.assertEqual(plan["summary"]["vpcs"], 1)
        self.assertEqual(plan["summary"]["subnets"], 2)
        self.assertEqual(plan["steps"][0]["resource"], "vpcs")


if __name__ == "__main__":
    unittest.main()
