import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app import code_evolution
from app.db import connect, initialize


class CodeEvolutionTests(unittest.TestCase):
    def test_ast_gate_rejects_io_and_process_capabilities(self):
        unsafe = "def strategy_candidates():\n    return open('secret').read()\n"
        with self.assertRaises(ValueError):
            code_evolution._validate_recipe_source(unsafe)

    def test_generated_source_can_be_promoted_and_rolled_back(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "repo"
            target = root / code_evolution.EVOLVABLE_PATH
            target.parent.mkdir(parents=True)
            production_target = Path(code_evolution.__file__).parent / "evolvable" / "intraday_recipes.py"
            original = production_target.read_text(encoding="utf-8")
            target.write_text(original, encoding="utf-8")
            lake = Path(folder) / "lake"
            path = lake / "db" / "evolution.db"
            initialize(path)
            with patch.multiple(
                code_evolution, ROOT=root, connect=lambda: connect(path),
                initialize=lambda: initialize(path),
            ), patch.object(code_evolution.db, "DATA_LAKE", lake):
                created = code_evolution.create_automatic_candidate()
                with closing(connect(path)) as conn:
                    candidate = dict(conn.execute(
                        "SELECT * FROM code_evolution_candidates WHERE candidate_key=?",
                        (created["candidate_key"],)).fetchone())
                manifest = code_evolution._load(candidate["allowed_paths_json"], {})
                promoted = code_evolution._promote(
                    candidate, manifest, {"passed": True}, {"test": True})
                self.assertEqual(promoted["status"], "PROMOTED")
                self.assertNotEqual(target.read_text(encoding="utf-8"), original)
                rolled_back = code_evolution.rollback_active_version("unit test")
                self.assertEqual(rolled_back["status"], "ROLLED_BACK")
                self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_cleanup_only_removes_managed_sandboxes(self):
        with tempfile.TemporaryDirectory() as folder:
            lake = Path(folder) / "lake"
            managed = lake / "research" / "code_evolution" / "candidate" / "sandbox-test"
            managed.mkdir(parents=True)
            (managed / "snapshot.db").write_bytes(b"test")
            outside = Path(folder) / "sandbox-outside"
            outside.mkdir()
            with patch.object(code_evolution.db, "DATA_LAKE", lake):
                removed = code_evolution._cleanup_sandbox(managed)
                refused = code_evolution._cleanup_sandbox(outside)
            self.assertTrue(removed["removed"])
            self.assertFalse(managed.exists())
            self.assertFalse(refused["removed"])
            self.assertTrue(outside.exists())


if __name__ == "__main__":
    unittest.main()
