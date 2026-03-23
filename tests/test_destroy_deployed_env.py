import unittest

from executor.scripts.destroy_deployed_env import (
    find_event_source_mapping_uuid,
    should_delete_network_resource,
)


class DestroyDeployedEnvTests(unittest.TestCase):

    def test_should_delete_network_resource_only_for_created(self):
        self.assertTrue(should_delete_network_resource({"operation": "created"}))
        self.assertFalse(should_delete_network_resource({"operation": "adopted-existing"}))
        self.assertFalse(should_delete_network_resource({"operation": "mapped-default"}))

    def test_find_event_source_mapping_uuid_matches_function_and_source(self):
        class FakeLambdaClient:
            def get_paginator(self, _name):
                class Paginator:
                    def paginate(self_inner, FunctionName):
                        return [{
                            "EventSourceMappings": [
                                {"UUID": "m-1", "EventSourceArn": "arn:aws:sqs:us-east-2:123:q-1"},
                                {"UUID": "m-2", "EventSourceArn": "arn:aws:sqs:us-east-2:123:q-2"},
                            ]
                        }]
                return Paginator()

        self.assertEqual(
            find_event_source_mapping_uuid(FakeLambdaClient(), "fn-a", "arn:aws:sqs:us-east-2:123:q-2"),
            "m-2",
        )
        self.assertIsNone(
            find_event_source_mapping_uuid(FakeLambdaClient(), "fn-a", "arn:aws:sqs:us-east-2:123:q-3")
        )


if __name__ == "__main__":
    unittest.main()
