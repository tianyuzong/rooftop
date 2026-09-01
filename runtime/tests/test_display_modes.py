from pathlib import Path
import unittest


STATIC_ROOT = Path(__file__).resolve().parents[1] / "app" / "static"


class ContentDisclosureContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        cls.app = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
        cls.contrast = (STATIC_ROOT / "contrast.css").read_text(encoding="utf-8")

    def test_global_beginner_professional_switch_is_removed(self):
        self.assertNotIn('data-display-mode=', self.index + self.app)
        self.assertNotIn('argus-display-mode', self.index + self.app)
        self.assertNotIn('applyDisplayMode', self.app)

    def test_professional_material_is_available_in_disclosures(self):
        self.assertIn(".professional-only { display: none !important; }", self.contrast)
        self.assertIn(".professional-disclosure .professional-only", self.contrast)
        self.assertIn("professional-disclosure quant-professional-disclosure", self.app)
        self.assertIn("专业数据与计算依据", self.app)

    def test_existing_technical_surfaces_are_collapsible(self):
        for marker in (
            "source-advanced professional-disclosure",
            "decision-options professional-disclosure",
            "advanced-harness professional-disclosure",
            "model-registry-band professional-disclosure",
        ):
            source = self.index if marker.startswith("model-registry") else self.app
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
