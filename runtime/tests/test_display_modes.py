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

    def test_stop_loss_hint_stays_inline_so_the_input_row_aligns(self):
        styles = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")
        self.assertIn('class="decision-field-label"', self.app)
        self.assertIn(".decision-field-label{display:flex", styles)
        self.assertNotIn(
            'value="${Number(d.stop_loss_pct)}"><small>', self.app
        )

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

    def test_readability_layer_loads_last_and_raises_small_text_floor(self):
        self.assertGreater(
            self.index.index('href="/contrast.css"'),
            self.index.index('href="/logic.css"'),
        )
        self.assertIn("Final readability layer", self.contrast)
        self.assertIn("font-size: 13px !important", self.contrast)
        self.assertIn("outline: 2px solid var(--cyan)", self.contrast)

    def test_rooftop_brand_replaces_visible_argus_branding(self):
        self.assertIn("<title>Rooftop · 投资研究台</title>", self.index)
        self.assertIn("<strong>ROOFTOP</strong>", self.index)
        self.assertNotIn("<title>Argus", self.index)
        self.assertNotIn("<strong>ARGUS</strong>", self.index)
        self.assertIn("请输入 Rooftop 远程访问令牌", self.app)
        self.assertIn("Rooftop-持仓导入模板.csv", self.app)
        self.assertIn("X-Argus-Token", self.app)

    def test_saved_risk_settings_cannot_break_recommendation_views(self):
        self.assertIn("const maxDrawdown=Math.max(1,Math.min(80", self.app)
        self.assertIn("input[key]=bounded", self.app)
        self.assertIn("已将${adjusted.join('和')}收紧到最大回撤", self.app)
        self.assertIn('max="${Number(d.max_drawdown_pct)}"', self.app)
        self.assertIn("旧设置冲突时会自动收紧", self.app)

    def test_forecast_charts_prioritize_calibrated_horizon(self):
        self.assertIn("item.validated===true", self.app)
        self.assertIn("central_50_coverage_gate===true", self.app)
        self.assertIn("主图聚焦最近通过检验的核心区间", self.app)
        self.assertIn("长期历史基准仅在专业表格显示", self.app)
        self.assertIn("不作为精确预测", self.app)


if __name__ == "__main__":
    unittest.main()
