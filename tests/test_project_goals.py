import unittest
from pathlib import Path


PROJECT_GOALS = [
    "Convert user requests into AWS CLI commands",
    "Execute commands safely",
    "Detect errors and auto-fix",
    "Deploy infrastructure automatically",
    "Save git snapshots",
]


class ProjectGoalsTests(unittest.TestCase):

    def test_project_md_contains_core_goals(self):
        project_text = Path("project.md").read_text(encoding="utf-8")

        for goal in PROJECT_GOALS:
            self.assertIn(goal, project_text)


if __name__ == "__main__":
    unittest.main()
