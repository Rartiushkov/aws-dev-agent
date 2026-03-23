import unittest

from executor.scripts.export_iac_blueprint import build_iac_blueprint


class ExportIacBlueprintTests(unittest.TestCase):

    def test_build_iac_blueprint_counts_resources(self):
        snapshot = {
            "source_env": "legacy",
            "region": "us-east-1",
            "iam_roles": [{}, {}],
            "sqs_queues": [{}],
            "sns_topics": [],
            "lambda_functions": [{}, {}],
            "dynamodb_tables": [{}],
            "api_gateways": [{}],
            "ecs": {"clusters": [{}], "services": [{}]},
            "rds": {"instances": [{"DBInstanceIdentifier": "db-1"}], "clusters": []},
            "ec2_instances": [{"InstanceId": "i-123"}],
            "cloudformation_stacks": [{"StackName": "legacy-stack"}],
        }

        blueprint = build_iac_blueprint(snapshot)

        self.assertEqual(blueprint["terraform_blueprint"]["aws_iam_role"], 2)
        self.assertEqual(blueprint["cloudformation_blueprint"]["AWS::Lambda::Function"], 2)
        self.assertEqual(blueprint["terraform_blueprint"]["aws_db_instance"], 1)
        self.assertEqual(blueprint["cloudformation_blueprint"]["AWS::EC2::Instance"], 1)
        self.assertIn("terraform_stub_resources", blueprint)
        self.assertIn("cloudformation_stub_resources", blueprint)


if __name__ == "__main__":
    unittest.main()
