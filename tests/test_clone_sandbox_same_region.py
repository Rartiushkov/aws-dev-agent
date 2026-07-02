import unittest

from executor.scripts.clone_sandbox_same_region import assert_safe_clone_plan


class CloneSandboxSameRegionTests(unittest.TestCase):
    def test_accepts_same_region_plan_without_manual_review(self):
        plan = {
            "mode": "read-only-assessment",
            "source_env": "sandbox1",
            "target_env": "sandbox2",
            "region": "us-east-1",
            "manual_review": {
                "cloudformation_stacks": 0,
                "s3_buckets": 0,
                "load_balancers": 0,
                "rds_instances": 0,
                "rds_clusters": 0,
            },
            "preflight_checks": [
                {
                    "name": "scope",
                    "details": {
                        "same_account": True,
                        "same_region": True,
                    },
                },
                {
                    "name": "hardcoded-source-account-references",
                    "status": "warning",
                },
            ],
        }

        assert_safe_clone_plan(plan, "sandbox1", "sandbox2", "us-east-1")

    def test_rejects_manual_review_resources(self):
        plan = {
            "mode": "read-only-assessment",
            "source_env": "sandbox1",
            "target_env": "sandbox2",
            "region": "us-east-1",
            "manual_review": {
                "cloudformation_stacks": 1,
            },
            "preflight_checks": [
                {
                    "name": "scope",
                    "details": {
                        "same_account": True,
                        "same_region": True,
                    },
                },
                {
                    "name": "hardcoded-source-account-references",
                    "status": "warning",
                },
            ],
        }

        with self.assertRaises(RuntimeError):
            assert_safe_clone_plan(plan, "sandbox1", "sandbox2", "us-east-1")


if __name__ == "__main__":
    unittest.main()
