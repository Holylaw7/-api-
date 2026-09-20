"""Keep the AI maintenance and API guides aligned with exposed local routes."""
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def routes(relative):
    source = read(relative)
    return set(re.findall(r"(?:if|elif) path == ['\"](/api/[^'\"]+)['\"]", source))


class DocumentationCoverageTests(unittest.TestCase):
    def test_main_http_routes_are_in_api_reference(self):
        reference = read("docs/API_REFERENCE.md")
        missing = sorted(route for route in routes("app/server.py") if f"`{route}" not in reference)
        self.assertEqual([], missing)

    def test_assistant_http_routes_are_in_api_reference(self):
        reference = read("docs/API_REFERENCE.md")
        missing = sorted(route for route in routes("app/ai_studio.py") if f"`{route}" not in reference)
        self.assertEqual([], missing)

    def test_navigation_points_to_the_three_ai_guides(self):
        for relative in ("README.md", "AGENTS.md"):
            content = read(relative)
            with self.subTest(relative=relative):
                self.assertIn("AI_MAINTENANCE.md", content)
                self.assertIn("API_REFERENCE.md", content)
                self.assertIn("CONTINUOUS_OPTIMIZATION.md", content)

    def test_optimization_guide_preserves_guardrails(self):
        guide = read("docs/CONTINUOUS_OPTIMIZATION.md")
        for required in (
            "development_only", "awaiting_future_validation", "automatically_applied=false",
            "09:24:50", "09:26:00", "15:10", "holdouts.json",
            "30 日", "300", "60%", "+3 个百分点",
        ):
            with self.subTest(required=required):
                self.assertIn(required, guide)

    def test_optional_finance_skill_is_not_documented_as_runtime_or_credentials(self):
        for relative in (
            "README.md", "AGENTS.md", "docs/AI_MAINTENANCE.md", "docs/FINANCE_CONNECTION.md",
        ):
            content = read(relative)
            with self.subTest(relative=relative):
                self.assertIn("hithink-finance", content)
                self.assertIn("不是", content)
        connection = read("docs/FINANCE_CONNECTION.md")
        self.assertIn("不需要 Node.js", connection)
        self.assertIn("不会提供、保存或验证 API Key", connection)
        agents = read("AGENTS.md")
        self.assertIn("`API_KEY`", agents)
        self.assertIn("不得静默更新 Skill", agents)

    def test_project_finance_skill_is_discoverable_and_packaged(self):
        relative = ".agents/skills/auction-lab-finance/SKILL.md"
        skill_path = ROOT / relative
        self.assertTrue(skill_path.is_file())
        skill = skill_path.read_text(encoding="utf-8")
        self.assertRegex(skill, r"(?m)^name: auction-lab-finance$")
        self.assertNotIn("[TODO", skill)
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", skill):
            with self.subTest(target=target):
                self.assertTrue((skill_path.parent / target).resolve().is_file())
        release = read("tools/build_release.py")
        self.assertIn(relative, release)
        self.assertIn(".agents/skills/auction-lab-finance/agents/openai.yaml", release)


if __name__ == "__main__":
    unittest.main()
