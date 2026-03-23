import unittest

from executor.scripts.analyze_kms_usage import build_kms_report


class AnalyzeKmsUsageTests(unittest.TestCase):

    def test_build_kms_report_collects_secret_and_queue_usage(self):
        snapshot = {
            "source_env": "legacy",
            "region": "us-east-1",
            "secrets": [{"Name": "secret-a", "KmsKeyId": "kms-1"}],
            "sqs_queues": [{"QueueName": "queue-a", "Attributes": {"KmsMasterKeyId": "kms-1"}}],
            "codebuild_projects": [],
            "s3_buckets": [],
        }

        report = build_kms_report(snapshot)

        self.assertEqual(report["kms_key_count"], 1)
        self.assertEqual(len(report["keys"][0]["used_by"]), 2)

    def test_build_kms_report_includes_suggested_mapping(self):
        snapshot = {
            "source_env": "legacy",
            "region": "us-east-1",
            "secrets": [{"Name": "secret-a", "KmsKeyId": "alias/source-key"}],
            "sqs_queues": [],
            "codebuild_projects": [],
            "s3_buckets": [],
        }

        report = build_kms_report(snapshot, {"overrides": {"kms_key_mapping": {"alias/source-key": "alias/target-key"}}})

        self.assertEqual(report["keys"][0]["suggested_target_key"], "alias/target-key")

    def test_build_kms_report_collects_codebuild_and_s3_usage(self):
        snapshot = {
            "source_env": "legacy",
            "region": "us-east-1",
            "secrets": [],
            "sqs_queues": [],
            "codebuild_projects": [{"name": "build-a", "encryptionKey": "kms-2"}],
            "s3_buckets": [
                {
                    "Name": "bucket-a",
                    "BucketEncryption": {
                        "Rules": [
                            {"ApplyServerSideEncryptionByDefault": {"KMSMasterKeyID": "alias/bucket-key"}}
                        ]
                    },
                }
            ],
        }

        report = build_kms_report(snapshot)

        self.assertEqual(report["kms_key_count"], 2)
        self.assertTrue(any(item["type"] == "codebuild" for key in report["keys"] for item in key["used_by"]))
        self.assertTrue(any(item["type"] == "s3" for key in report["keys"] for item in key["used_by"]))


if __name__ == "__main__":
    unittest.main()
