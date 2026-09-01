import tempfile
import unittest
from pathlib import Path

from app.db import connect, initialize
from app.model_registry import (
    activate_model_definition,
    create_model_definition,
    load_model_context,
    model_registry_payload,
)


class ModelRegistryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "registry.db"
        initialize(self.path)
        self.factory = lambda: connect(self.path)

    def tearDown(self):
        self.folder.cleanup()

    def test_defaults_are_seeded_and_balanced_is_available(self):
        payload = model_registry_payload(self.factory)
        self.assertEqual(len(payload["definitions"]), 4)
        self.assertEqual(len(payload["assignments"]), 4)
        context = load_model_context(["600519"], self.factory)
        selected = context["by_symbol"]["600519"]["balanced"]
        self.assertEqual(selected["financial"]["model_key"], "financial.default.balanced")
        self.assertEqual(selected["valuation"]["model_key"], "valuation.default.general")

    def test_industry_model_is_versioned_validated_and_requires_approval(self):
        created = create_model_definition({
            "model_key": "valuation.baijiu",
            "model_kind": "valuation",
            "name": "白酒估值模型",
            "version": "1.0.0",
            "specification": {
                "dimensions": {
                    "value": [{"field": "valuation.pe_ttm", "low": 12, "high": 45,
                               "inverse": True}],
                },
            },
        }, "analyst", self.factory)
        with self.assertRaises(ValueError):
            activate_model_definition(
                created["id"], "INDUSTRY", "白酒", None, "", False, self.factory
            )
        active = activate_model_definition(
            created["id"], "INDUSTRY", "白酒", None, "reviewer", True, self.factory
        )
        self.assertEqual(active["status"], "ACTIVE")
        self.assertFalse(active["order_execution"])

    def test_model_cannot_enable_order_execution(self):
        with self.assertRaises(ValueError):
            create_model_definition({
                "model_key": "risk.unsafe",
                "model_kind": "risk",
                "version": "1",
                "specification": {"order_execution": True},
            }, "analyst", self.factory)


if __name__ == "__main__":
    unittest.main()
