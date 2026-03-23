import json
import unittest

from executor.scripts.build_cloudformation_import_plan import build_import_plan, identify_resource


class BuildCloudFormationImportPlanTests(unittest.TestCase):

    def test_build_import_plan_maps_existing_ecs_cluster(self):
        exports = {
            "stacks": [
                {
                    "stack_name": "Infra-ECS-Cluster-legacy",
                    "template_path": "ignored.json",
                }
            ]
        }
        deployment_manifest = {
            "resource_mappings": {
                "ecs_cluster_names": {"legacy-cluster": "roma-art-legacy-cluster"},
                "ecs_cluster_arns": {
                    "arn:aws:ecs:us-east-1:123:cluster/legacy-cluster": "arn:aws:ecs:us-east-1:456:cluster/roma-art-legacy-cluster"
                },
            }
        }
        template = {
            "Parameters": {"ECSClusterName": {"Type": "String", "Default": "legacy-cluster"}},
            "Resources": {
                "ECSCluster": {"Type": "AWS::ECS::Cluster", "Properties": {"ClusterName": {"Ref": "ECSClusterName"}}}
            },
        }

        import executor.scripts.build_cloudformation_import_plan as module
        original = module.Path.read_text
        module.Path.read_text = lambda self, encoding="utf-8": json.dumps(template)
        try:
            plan = build_import_plan(exports, deployment_manifest, "roma-art")
        finally:
            module.Path.read_text = original

        resource = plan["stacks"][0]["resources"][0]
        self.assertEqual(resource["identifier_values"]["ClusterName"], "roma-art-legacy-cluster")
        self.assertTrue(resource["physical_id"].endswith("/roma-art-legacy-cluster"))

    def test_identify_resource_supports_queue_and_table(self):
        mappings = {
            "queue_arns": {"old": "arn:aws:sqs:us-east-1:456:roma-art-queue"},
            "dynamodb_table_arns": {"old": "arn:aws:dynamodb:us-east-1:456:table/roma-art-table"},
        }
        params = {"QueueName": "roma-art-queue", "TableName": "roma-art-table"}

        queue = identify_resource(
            "Queue",
            {"Type": "AWS::SQS::Queue", "Properties": {"QueueName": {"Ref": "QueueName"}}},
            params,
            mappings,
        )
        table = identify_resource(
            "Table",
            {"Type": "AWS::DynamoDB::Table", "Properties": {"TableName": {"Ref": "TableName"}}},
            params,
            mappings,
        )

        self.assertEqual(queue["identifier_values"]["QueueName"], "roma-art-queue")
        self.assertTrue(queue["physical_id"].endswith(":roma-art-queue"))
        self.assertEqual(table["identifier_values"]["TableName"], "roma-art-table")
        self.assertTrue(table["physical_id"].endswith("/roma-art-table"))


if __name__ == "__main__":
    unittest.main()
