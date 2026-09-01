import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


class BackupScriptTests(unittest.TestCase):
    def test_backup_is_consistent_and_writes_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.db"
            output = root / "backups"
            with closing(sqlite3.connect(source)) as conn:
                conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY,value TEXT)")
                conn.execute("INSERT INTO sample(value) VALUES('verified')")
                conn.commit()
            script = Path(__file__).resolve().parents[2] / "scripts" / "backup_argus_data.py"
            completed = subprocess.run(
                [sys.executable, str(script), "--database", str(source),
                 "--output-directory", str(output), "--keep", "2"],
                check=True, capture_output=True, text=True,
            )
            payload = json.loads(completed.stdout)
            backup = Path(payload["backup_database"])
            self.assertEqual(payload["quick_check"], "ok")
            self.assertTrue(backup.is_file())
            self.assertTrue(backup.with_suffix(".json").is_file())
            with closing(sqlite3.connect(backup)) as conn:
                self.assertEqual(conn.execute("SELECT value FROM sample").fetchone()[0],
                                 "verified")


if __name__ == "__main__":
    unittest.main()
