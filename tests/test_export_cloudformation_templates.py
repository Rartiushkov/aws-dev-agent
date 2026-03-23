import unittest

from collections import OrderedDict

from executor.scripts.export_cloudformation_templates import export_templates, safe_filename, serialize_template_body


class ExportCloudFormationTemplatesTests(unittest.TestCase):

    def test_safe_filename_strips_unsafe_characters(self):
        self.assertEqual(safe_filename("legacy stack/one"), "legacy-stack-one")

    def test_export_templates_collects_template_body_and_errors(self):
        class FakeCfClient:
            def get_template(self, StackName):
                if StackName == "broken-stack":
                    raise RuntimeError("access denied")
                return {"TemplateBody": "{\"Resources\":{}}", "StagesAvailable": ["Original"]}

        snapshot = {"cloudformation_stacks": [{"StackName": "legacy-stack"}, {"StackName": "broken-stack"}]}

        exported = export_templates(snapshot, FakeCfClient())

        self.assertEqual(exported[0]["stack_name"], "legacy-stack")
        self.assertIn("TemplateBody", {"TemplateBody": exported[0]["template_body"]})
        self.assertEqual(exported[1]["stack_name"], "broken-stack")
        self.assertIn("error", exported[1])

    def test_serialize_template_body_handles_ordered_dict(self):
        rendered = serialize_template_body(OrderedDict({"Resources": OrderedDict()}))
        self.assertIn("\"Resources\"", rendered)


if __name__ == "__main__":
    unittest.main()
