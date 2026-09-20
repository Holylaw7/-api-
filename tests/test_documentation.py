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


if __name__ == "__main__":
    unittest.main()
