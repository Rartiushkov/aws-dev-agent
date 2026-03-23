import unittest

from executor.scripts.transfer_s3_objects import (
    build_s3_transfer_plan,
    execute_s3_transfer,
    rewrite_bucket_encryption,
    rewrite_bucket_policy,
    rewrite_notification_configuration,
)


class TransferS3ObjectsTests(unittest.TestCase):

    def test_build_s3_transfer_plan_uses_snapshot_buckets(self):
        snapshot = {
            "source_env": "legacy",
            "account_id": "123",
            "region": "us-east-1",
            "s3_buckets": [{"Name": "bucket-a", "Region": "us-east-1", "Tags": [{"Key": "team", "Value": "platform"}]}],
        }

        plan = build_s3_transfer_plan(snapshot, config={"overrides": {"target_env": "virgin"}}, target_region="us-east-2", target_account_id="456")

        self.assertEqual(plan["bucket_count"], 1)
        self.assertEqual(plan["buckets"][0]["source_bucket"], "bucket-a")
        self.assertEqual(plan["buckets"][0]["target_bucket"], "bucket-a")
        self.assertEqual(plan["buckets"][0]["tags"][0]["Key"], "team")
        self.assertEqual(plan["target_env"], "virgin")
        self.assertEqual(plan["target_region"], "us-east-2")
        self.assertEqual(plan["target_account_id"], "456")
        self.assertFalse(plan["buckets"][0]["manual_review"])

    def test_build_s3_transfer_plan_remaps_account_scoped_bucket_names(self):
        snapshot = {
            "source_env": "legacy",
            "account_id": "123",
            "region": "us-east-1",
            "s3_buckets": [{"Name": "legacy-artifacts-123-us-east-1", "Region": "us-east-1"}],
        }

        plan = build_s3_transfer_plan(snapshot, config={"overrides": {"target_env": "virgin"}}, target_region="us-east-1", target_account_id="456")

        self.assertEqual(plan["buckets"][0]["target_bucket"], "virgin-artifacts-456-us-east-1")

    def test_execute_s3_transfer_copies_objects(self):
        class FakePaginator:
            def __init__(self, items):
                self.items = items

            def paginate(self, Bucket):
                return [{"Contents": self.items.get(Bucket, [])}]

        class Body:
            def __init__(self, content):
                self.content = content

            def read(self):
                return self.content

        class FakeSourceS3:
            def get_paginator(self, name):
                return FakePaginator({"bucket-a": [{"Key": "a.txt", "Size": 1}, {"Key": "b.txt", "Size": 2}]})

            def get_object(self, Bucket, Key):
                return {"Body": Body(f"data:{Key}".encode())}

        class FakeTargetS3:
            def __init__(self):
                self.objects = []

            def list_buckets(self):
                return {"Buckets": []}

            def get_paginator(self, name):
                return FakePaginator({"bucket-a": []})

            def create_bucket(self, **kwargs):
                return None

            def put_object(self, Bucket, Key, Body):
                self.objects.append((Bucket, Key, Body))

        plan = {"buckets": [{"source_bucket": "bucket-a", "target_bucket": "bucket-a"}]}
        target = FakeTargetS3()
        results = execute_s3_transfer(plan, FakeSourceS3(), target, "us-east-1")

        self.assertEqual(results[0]["copied_objects"], 2)
        self.assertEqual(len(target.objects), 2)

    def test_execute_s3_transfer_skips_matching_objects(self):
        class FakePaginator:
            def __init__(self, items):
                self.items = items

            def paginate(self, Bucket):
                return [{"Contents": self.items.get(Bucket, [])}]

        class Body:
            def __init__(self, content):
                self.content = content

            def read(self):
                return self.content

        class FakeSourceS3:
            def get_paginator(self, name):
                return FakePaginator({
                    "bucket-a": [{"Key": "a.txt", "Size": 1, "ETag": "\"1\""}],
                    "bucket-b": [],
                })

            def get_object(self, Bucket, Key):
                return {"Body": Body(b"data"), "Metadata": {"env": "test"}}

            def get_object_tagging(self, Bucket, Key):
                return {"TagSet": [{"Key": "team", "Value": "platform"}]}

        class FakeTargetS3:
            def __init__(self):
                self.objects = []
                self.tags = []

            def list_buckets(self):
                return {"Buckets": [{"Name": "bucket-a"}]}

            def get_paginator(self, name):
                return FakePaginator({
                    "bucket-a": [{"Key": "a.txt", "Size": 1, "ETag": "\"1\""}],
                })

            def put_object(self, **kwargs):
                self.objects.append(kwargs)

            def put_object_tagging(self, **kwargs):
                self.tags.append(kwargs)

        plan = {"buckets": [{"source_bucket": "bucket-a", "target_bucket": "bucket-a"}]}
        results = execute_s3_transfer(plan, FakeSourceS3(), FakeTargetS3(), "us-east-1")

        self.assertEqual(results[0]["copied_objects"], 0)
        self.assertEqual(results[0]["skipped_objects"], 1)

    def test_execute_s3_transfer_applies_bucket_configuration(self):
        class FakePaginator:
            def paginate(self, Bucket):
                return [{"Contents": []}]

        class FakeSourceS3:
            def get_paginator(self, name):
                return FakePaginator()

        class FakeTargetS3:
            def __init__(self):
                self.calls = []

            def list_buckets(self):
                return {"Buckets": []}

            def get_paginator(self, name):
                return FakePaginator()

            def create_bucket(self, **kwargs):
                return None

            def put_bucket_tagging(self, **kwargs):
                self.calls.append(("tagging", kwargs))

            def put_bucket_versioning(self, **kwargs):
                self.calls.append(("versioning", kwargs))

            def put_bucket_encryption(self, **kwargs):
                self.calls.append(("encryption", kwargs))

            def put_bucket_lifecycle_configuration(self, **kwargs):
                self.calls.append(("lifecycle", kwargs))

            def put_bucket_cors(self, **kwargs):
                self.calls.append(("cors", kwargs))

            def put_bucket_policy(self, **kwargs):
                self.calls.append(("policy", kwargs))

            def put_bucket_notification_configuration(self, **kwargs):
                self.calls.append(("notifications", kwargs))

        plan = {
            "buckets": [
                {
                    "source_bucket": "legacy-bucket",
                    "target_bucket": "target-bucket",
                    "tags": [{"Key": "team", "Value": "platform"}],
                    "versioning": {"Status": "Enabled"},
                    "bucket_encryption": {
                        "Rules": [
                            {
                                "ApplyServerSideEncryptionByDefault": {
                                    "SSEAlgorithm": "aws:kms",
                                    "KMSMasterKeyID": "alias/target-key",
                                }
                            }
                        ]
                    },
                    "lifecycle_rules": [{"ID": "expire-old", "Status": "Enabled", "Expiration": {"Days": 30}, "Filter": {"Prefix": ""}}],
                    "cors_rules": [{"AllowedMethods": ["GET"], "AllowedOrigins": ["*"]}],
                    "policy": "{\"Statement\":[{\"Resource\":\"arn:aws:s3:::legacy-bucket/*\"}]}",
                    "notification_configuration": {
                        "QueueConfigurations": [
                            {
                                "Id": "queue-events",
                                "QueueArn": "arn:aws:sqs:us-east-1:123:legacy-events",
                                "Events": ["s3:ObjectCreated:*"],
                            }
                        ]
                    },
                }
            ]
        }

        target = FakeTargetS3()
        results = execute_s3_transfer(plan, FakeSourceS3(), target, "us-east-1")

        self.assertEqual(results[0]["applied_bucket_configuration"], ["tags", "versioning", "encryption", "lifecycle", "cors", "policy", "notifications"])
        policy_call = next(call for call in target.calls if call[0] == "policy")
        self.assertIn("target-bucket", policy_call[1]["Policy"])
        notification_call = next(call for call in target.calls if call[0] == "notifications")
        self.assertEqual(notification_call[1]["NotificationConfiguration"]["QueueConfigurations"][0]["Id"], "queue-events")

    def test_rewrite_notification_configuration_remaps_exact_and_generic_arns(self):
        notification_configuration = {
            "QueueConfigurations": [
                {
                    "Id": "queue-events",
                    "QueueArn": "arn:aws:sqs:us-east-1:123:legacy-events",
                    "Events": ["s3:ObjectCreated:*"],
                }
            ],
            "LambdaFunctionConfigurations": [
                {
                    "Id": "lambda-events",
                    "LambdaFunctionArn": "arn:aws:lambda:us-east-1:123:function:legacy-handler",
                    "Events": ["s3:ObjectCreated:*"],
                }
            ],
        }
        bucket_plan = {
            "notification_arn_mapping": {
                "arn:aws:sqs:us-east-1:123:legacy-events": "arn:aws:sqs:us-east-2:456:virgin-events"
            },
            "source_region": "us-east-1",
            "target_region": "us-east-2",
            "source_account_id": "123",
            "target_account_id": "456",
            "source_env": "legacy",
            "target_env": "virgin",
        }

        rewritten = rewrite_notification_configuration(notification_configuration, bucket_plan)

        self.assertEqual(
            rewritten["QueueConfigurations"][0]["QueueArn"],
            "arn:aws:sqs:us-east-2:456:virgin-events",
        )
        self.assertEqual(
            rewritten["LambdaFunctionConfigurations"][0]["LambdaFunctionArn"],
            "arn:aws:lambda:us-east-2:456:function:virgin-handler",
        )

    def test_rewrite_bucket_policy_updates_bucket_account_region_and_env(self):
        bucket_plan = {
            "source_bucket": "legacy-bucket",
            "source_region": "us-east-1",
            "target_region": "us-east-2",
            "source_account_id": "123",
            "target_account_id": "456",
            "source_env": "legacy",
            "target_env": "virgin",
        }

        rewritten = rewrite_bucket_policy(
            "{\"Statement\":[{\"Principal\":{\"AWS\":\"arn:aws:iam::123:role/legacy-reader\"},\"Resource\":\"arn:aws:s3:::legacy-bucket/*\",\"Condition\":{\"ArnLike\":{\"aws:SourceArn\":\"arn:aws:lambda:us-east-1:123:function:legacy-loader\"}}}]}",
            bucket_plan,
            "target-bucket",
        )

        self.assertIn("arn:aws:iam::456:role/virgin-reader", rewritten)
        self.assertIn("arn:aws:s3:::target-bucket/*", rewritten)
        self.assertIn("arn:aws:lambda:us-east-2:456:function:virgin-loader", rewritten)

    def test_rewrite_bucket_encryption_remaps_kms_key(self):
        rewritten = rewrite_bucket_encryption(
            {
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "aws:kms",
                            "KMSMasterKeyID": "alias/source-key",
                        }
                    }
                ]
            },
            {"kms_key_mapping": {"alias/source-key": "alias/target-key"}},
        )

        self.assertEqual(
            rewritten["Rules"][0]["ApplyServerSideEncryptionByDefault"]["KMSMasterKeyID"],
            "alias/target-key",
        )


if __name__ == "__main__":
    unittest.main()
