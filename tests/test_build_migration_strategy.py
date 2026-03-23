import unittest

from executor.scripts.build_migration_strategy import build_strategy


class BuildMigrationStrategyTests(unittest.TestCase):

    def test_build_strategy_marks_manual_tracks_when_resources_exist(self):
        snapshot = {
            "source_env": "legacy",
            "account_id": "123",
            "region": "us-east-1",
            "vpcs": [{"VpcId": "vpc-1"}],
            "subnets": [{"SubnetId": "subnet-1"}],
            "route_tables": [{"RouteTableId": "rtb-1"}],
            "security_groups": [{"GroupId": "sg-1"}],
            "api_gateways": [{"id": "api-1"}],
            "rds": {"instances": [{"DBInstanceIdentifier": "db-1"}], "clusters": []},
            "s3_buckets": [{"Name": "bucket-1"}],
            "ec2_instances": [{"InstanceId": "i-123"}],
            "ecs": {"clusters": [{"clusterName": "cluster-1"}], "services": [{"serviceName": "svc-1"}], "task_definitions": [{"family": "td"}]},
            "load_balancers": [],
            "cloudformation_stacks": [{"StackName": "legacy-stack"}],
            "secrets": [{"Name": "secret-1", "KmsKeyId": "kms-1"}],
            "sqs_queues": [{"QueueName": "queue-1", "Attributes": {"KmsMasterKeyId": "kms-1"}}],
        }
        risk_report = {"summary": {"high": 1, "medium": 2, "low": 0}}

        strategy = build_strategy(snapshot, risk_report)
        tracks = {track["name"]: track for track in strategy["tracks"]}

        self.assertEqual(strategy["overall_status"], "partial")
        self.assertEqual(tracks["network"]["status"], "partial")
        self.assertEqual(tracks["rds_data_migration"]["status"], "partial")
        self.assertEqual(tracks["s3_object_transfer"]["status"], "manual")
        self.assertEqual(tracks["full_ecs_service_migration"]["status"], "partial")
        self.assertEqual(tracks["ec2_strategy"]["status"], "partial")
        self.assertEqual(tracks["iam_kms_deeper_handling"]["status"], "partial")
        self.assertEqual(tracks["zero_downtime_orchestration"]["status"], "partial")
        self.assertEqual(strategy["risk_summary"]["high"], 1)

    def test_build_strategy_reports_covered_when_category_absent(self):
        snapshot = {
            "source_env": "empty",
            "account_id": "123",
            "region": "us-east-1",
            "vpcs": [],
            "subnets": [],
            "route_tables": [],
            "security_groups": [],
            "api_gateways": [],
            "rds": {"instances": [], "clusters": []},
            "s3_buckets": [],
            "ec2_instances": [],
            "ecs": {"clusters": [], "services": [], "task_definitions": []},
            "load_balancers": [],
            "cloudformation_stacks": [],
            "secrets": [],
            "sqs_queues": [],
        }

        strategy = build_strategy(snapshot, {"summary": {}})
        tracks = {track["name"]: track for track in strategy["tracks"]}

        self.assertEqual(tracks["network"]["status"], "covered")
        self.assertEqual(tracks["api_gateway"]["status"], "covered")
        self.assertEqual(tracks["rds_data_migration"]["status"], "covered")
        self.assertEqual(tracks["s3_object_transfer"]["status"], "covered")
        self.assertEqual(tracks["full_ecs_service_migration"]["status"], "covered")


if __name__ == "__main__":
    unittest.main()
