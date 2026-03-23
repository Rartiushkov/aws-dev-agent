import unittest

from executor.scripts.build_advanced_migration_plan import (
    build_advanced_plan,
    build_cloudformation_plan,
    build_ec2_plan,
    build_rds_plan,
)


class BuildAdvancedMigrationPlanTests(unittest.TestCase):

    def test_build_rds_plan_creates_engine_aware_entries(self):
        snapshot = {
            "source_env": "legacy",
            "rds": {
                "instances": [
                    {
                        "DBInstanceIdentifier": "legacy-db",
                        "Engine": "postgres",
                        "MultiAZ": True,
                        "StorageEncrypted": True,
                        "DBSubnetGroup": {"DBSubnetGroupName": "legacy-subnets"},
                        "DBParameterGroups": [{"DBParameterGroupName": "legacy-pg"}],
                    }
                ],
                "clusters": [
                    {
                        "DBClusterIdentifier": "legacy-aurora",
                        "Engine": "aurora-postgresql",
                        "StorageEncrypted": True,
                    }
                ],
            },
        }

        plan = build_rds_plan(snapshot, target_env="roma-art")

        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[0]["strategy"], "db-snapshot-restore")
        self.assertEqual(plan[1]["strategy"], "cluster-snapshot-restore")

    def test_build_ec2_plan_creates_ami_strategy(self):
        snapshot = {
            "source_env": "legacy",
            "ec2_instances": [
                {
                    "InstanceId": "i-123",
                    "InstanceType": "m6i.large",
                    "SubnetId": "subnet-1",
                    "SecurityGroups": [{"GroupId": "sg-1"}],
                    "Tags": [{"Key": "Name", "Value": "legacy-api"}],
                }
            ],
        }

        plan = build_ec2_plan(snapshot, target_env="roma-art")

        self.assertEqual(plan[0]["strategy"], "create-image-copy-launch")
        self.assertEqual(plan[0]["target_name"], "roma-art-api")

    def test_build_cloudformation_plan_marks_export_strategy(self):
        snapshot = {
            "source_env": "legacy",
            "cloudformation_stacks": [{"StackName": "legacy-stack", "StackStatus": "CREATE_COMPLETE"}],
        }

        plan = build_cloudformation_plan(snapshot, target_env="roma-art")

        self.assertEqual(plan[0]["strategy"], "export-template-and-redeploy")
        self.assertTrue(plan[0]["requires_template_export"])

    def test_build_advanced_plan_summarizes_sections(self):
        snapshot = {
            "source_env": "legacy",
            "account_id": "123",
            "region": "us-east-1",
            "rds": {"instances": [{"DBInstanceIdentifier": "legacy-db", "Engine": "postgres"}], "clusters": []},
            "ec2_instances": [{"InstanceId": "i-123"}],
            "cloudformation_stacks": [{"StackName": "legacy-stack"}],
        }

        plan = build_advanced_plan(snapshot, target_env="roma-art")

        self.assertEqual(plan["summary"]["rds_plan_count"], 1)
        self.assertEqual(plan["summary"]["ec2_plan_count"], 1)
        self.assertEqual(plan["summary"]["cloudformation_plan_count"], 1)


if __name__ == "__main__":
    unittest.main()
