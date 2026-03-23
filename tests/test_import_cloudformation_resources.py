import unittest

from executor.scripts.import_cloudformation_resources import ensure_deletion_policies, prepare_template_for_import


class ImportCloudFormationResourcesTests(unittest.TestCase):

    def test_ensure_deletion_policies_marks_imported_resources(self):
        template = {
            "Resources": {
                "ECSCluster": {
                    "Type": "AWS::ECS::Cluster",
                    "Properties": {},
                }
            }
        }

        updated = ensure_deletion_policies(template, ["ECSCluster"])

        self.assertEqual(updated["Resources"]["ECSCluster"]["DeletionPolicy"], "Retain")
        self.assertEqual(updated["Resources"]["ECSCluster"]["UpdateReplacePolicy"], "Retain")

    def test_prepare_template_for_import_removes_outputs(self):
        template = {
            "Resources": {"ECSCluster": {"Type": "AWS::ECS::Cluster", "Properties": {}}},
            "Outputs": {"Cluster": {"Value": "x"}},
        }

        prepared = prepare_template_for_import(template, ["ECSCluster"])

        self.assertNotIn("Outputs", prepared)


if __name__ == "__main__":
    unittest.main()
