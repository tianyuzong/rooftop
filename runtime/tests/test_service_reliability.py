import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from app import alerts, server
from app.db import connect, initialize
from app.process_lock import ProcessLock


class ServiceReliabilityTests(unittest.TestCase):
    def test_smtp_close_error_does_not_requeue_sent_mail(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.db"
            initialize(path)
            factory = lambda: connect(path)
            alerts.queue_alert("close-error", "fixture", "body", target="test@example.invalid", conn_factory=factory)
            transport = {"ARGUS_SMTP_HOST": "smtp.example.invalid", "ARGUS_SMTP_USER": "test",
                         "ARGUS_SMTP_PASSWORD": "test-fixture", "ARGUS_EMAIL_SEND_ENABLED": "1"}
            client = Mock()
            smtp = Mock()
            smtp.return_value.__enter__ = Mock(return_value=client)
            smtp.return_value.__exit__ = Mock(side_effect=OSError("connection closed during QUIT"))
            with patch.dict(os.environ, transport), patch.object(alerts.smtplib, "SMTP_SSL", smtp):
                result = alerts.send_pending(conn_factory=factory)
            self.assertEqual(result["sent"], 1)
            with closing(factory()) as conn:
                row = conn.execute("SELECT status,attempts,next_attempt_at FROM alert_outbox").fetchone()
                self.assertEqual(row["status"], "SENT")
                self.assertEqual(row["attempts"], 1)
                self.assertIsNone(row["next_attempt_at"])

    def test_lock_excludes_another_process_and_releases_on_close(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "service.lock"
            lock = ProcessLock(path)
            self.assertTrue(lock.acquire())
            script = ("from pathlib import Path; from app.process_lock import ProcessLock; "
                      "import sys; lock=ProcessLock(Path(sys.argv[1])); "
                      "sys.exit(0 if lock.acquire() else 7)")
            args = [sys.executable, "-B", "-c", script, str(path)]
            options = {"cwd": str(Path(__file__).resolve().parents[1]),
                       "capture_output": True, "timeout": 10,
                       "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0}
            try:
                self.assertEqual(subprocess.run(args, **options).returncode, 7)
            finally:
                lock.release()
            self.assertEqual(subprocess.run(args, **options).returncode, 0)

    def test_second_service_cannot_initialize_or_start_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            lake = Path(folder)
            lock = ProcessLock(lake / "cache" / "service.lock")
            self.assertTrue(lock.acquire())
            try:
                with patch.object(server, "DATA_LAKE", lake), patch.object(server, "initialize") as initialize_mock:
                    with self.assertRaisesRegex(RuntimeError, "already has a running service"):
                        server.run(port=0)
                    initialize_mock.assert_not_called()
            finally:
                lock.release()

    def test_outbox_worker_retries_without_a_new_signal(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.db"
            initialize(path)
            factory = lambda: connect(path)
            alerts.queue_alert("test", "review fixture", "test body", target="test@example.invalid", conn_factory=factory)
            transport = {"ARGUS_SMTP_HOST": "smtp.example.invalid", "ARGUS_SMTP_USER": "test",
                         "ARGUS_SMTP_PASSWORD": "test-fixture", "ARGUS_EMAIL_SEND_ENABLED": "1"}
            with patch.dict(os.environ, transport), patch.object(
                alerts.smtplib, "SMTP_SSL", side_effect=OSError("temporary failure")
            ):
                first = alerts.send_pending(conn_factory=factory)
            self.assertEqual(first["status"], "FAILED")
            with closing(factory()) as conn:
                row = conn.execute("SELECT status,attempts,next_attempt_at FROM alert_outbox").fetchone()
                self.assertEqual(row["status"], "RETRY")
                self.assertEqual(row["attempts"], 1)
                self.assertTrue(row["next_attempt_at"])
                conn.execute("UPDATE alert_outbox SET next_attempt_at='2000-01-01T00:00:00+00:00'")
                conn.commit()
            client = Mock()
            transport_mock = Mock()
            transport_mock.return_value.__enter__ = Mock(return_value=client)
            transport_mock.return_value.__exit__ = Mock(return_value=False)
            with patch.dict(os.environ, transport), patch.object(alerts.smtplib, "SMTP_SSL", transport_mock), patch.object(
                server, "send_pending", side_effect=lambda **kwargs: alerts.send_pending(conn_factory=factory, **kwargs)
            ):
                result = server.AlertOutboxRefresher().run_once()
            self.assertEqual(result["sent"], 1)
            client.send_message.assert_called_once()
            with closing(factory()) as conn:
                self.assertEqual(conn.execute("SELECT status FROM alert_outbox").fetchone()[0], "SENT")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM alert_outbox").fetchone()[0], 1)

    def test_concurrent_email_dispatch_does_not_send_the_same_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.db"
            initialize(path)
            lock = ProcessLock(path.with_suffix(".outbox.lock"))
            self.assertTrue(lock.acquire())
            try:
                with patch.object(alerts.smtplib, "SMTP_SSL") as transport:
                    result = alerts.send_pending(conn_factory=lambda: connect(path))
                self.assertEqual(result["status"], "BUSY")
                transport.assert_not_called()
            finally:
                lock.release()


if __name__ == "__main__":
    unittest.main()
