import unittest

from executor.scripts.clone_ecs_cluster_same_region import (
    build_plan,
    service_preflight_issues,
    target_service_names,
    target_task_families,
)


class CloneEcsClusterSameRegionTests(unittest.TestCase):
    def test_service_preflight_flags_load_balancer_and_registry(self):
        issues = service_preflight_issues([
            {"serviceName": "comfi-sandbox-api", "loadBalancers": [{}]},
            {"serviceName": "comfi-sandbox-worker", "serviceRegistries": [{}]},
        ])

        self.assertEqual(len(issues), 2)
        self.assertEqual(issues[0]["reason"], "load_balancers_not_supported_yet")
        self.assertEqual(issues[1]["reason"], "service_registries_not_supported_yet")

    def test_target_names_replace_cluster_name_fragment(self):
        snapshot = {
            "ecs": {
                "services": [{"serviceName": "comfi-sandbox-api"}],
                "task_definitions": [{"family": "comfi-sandbox-taskdef"}],
            }
        }

        self.assertEqual(
            target_service_names(snapshot, "comfi-sandbox", "comfi-dev"),
            {"comfi-sandbox-api": "comfi-dev-api"},
        )
        self.assertEqual(
            target_task_families(snapshot, "comfi-sandbox", "comfi-dev"),
            {"comfi-sandbox-taskdef": "comfi-dev-taskdef"},
        )

    def test_build_plan_marks_cluster_ready_without_issues(self):
        snapshot = {
            "ecs": {
                "clusters": [{"clusterArn": "arn:aws:ecs:us-east-1:123:cluster/comfi-sandbox"}],
                "services": [{"serviceName": "comfi-sandbox-api"}],
                "task_definitions": [{"family": "comfi-sandbox-taskdef"}],
            }
        }

        plan = build_plan(snapshot, "comfi-sandbox", "comfi-dev", "us-east-1")

        self.assertTrue(plan["ready_to_apply"])
        self.assertEqual(plan["target_summary"]["cluster_name"], "comfi-dev")
        self.assertEqual(plan["target_summary"]["service_names"], ["comfi-dev-api"])


if __name__ == "__main__":
    unittest.main()
