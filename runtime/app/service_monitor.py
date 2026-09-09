"""Runtime truth for background services, with restart on confirmed thread exit."""
from __future__ import annotations
from datetime import datetime, timezone
import threading
import time


class TrackedEvent:
    def __init__(self, original):
        self.original = original
        self.last_heartbeat = time.time()
        self.waiting_until = None

    def wait(self, timeout=None):
        self.last_heartbeat = time.time()
        self.waiting_until = time.time() + timeout if timeout is not None else None
        try:
            return self.original.wait(timeout)
        finally:
            self.waiting_until = None
            self.last_heartbeat = time.time()

    def set(self):
        return self.original.set()

    def is_set(self):
        return self.original.is_set()


class ServiceMonitor(threading.Thread):
    def __init__(self):
        super().__init__(name="rooftop-service-monitor", daemon=True)
        self.stop_event = threading.Event()
        self.entries = {}
        self.lock = threading.RLock()

    def register(self, key, label, worker, factory=None, enabled=True):
        with self.lock:
            if worker and hasattr(worker, "stop_event") and not isinstance(worker.stop_event, TrackedEvent):
                worker.stop_event = TrackedEvent(worker.stop_event)
            self.entries[key] = {"label": label, "worker": worker, "factory": factory,
                                 "enabled": enabled, "restarts": 0, "last_error": None,
                                 "last_restart": 0}

    def check_once(self):
        with self.lock:
            for entry in self.entries.values():
                worker = entry["worker"]
                if not entry["enabled"] or not worker or worker.is_alive() or not entry["factory"]:
                    continue
                if worker.stop_event.is_set() or time.time() - entry["last_restart"] < 30:
                    continue
                # A long operation is never restarted merely because a heartbeat is old.
                try:
                    replacement = entry["factory"]()
                    if hasattr(replacement, "stop_event"):
                        replacement.stop_event = TrackedEvent(replacement.stop_event)
                    if not replacement.is_alive():
                        replacement.start()
                    entry.update(worker=replacement, last_error=None)
                    entry["restarts"] += 1
                except Exception as exc:
                    entry["last_error"] = str(exc)[:300]
                entry["last_restart"] = time.time()

    def payload(self):
        with self.lock:
            services = []
            for key, entry in self.entries.items():
                worker = entry["worker"]
                alive = bool(worker and worker.is_alive())
                tracker = getattr(worker, "stop_event", None)
                waiting_until = getattr(tracker, "waiting_until", None)
                heartbeat = getattr(tracker, "last_heartbeat", None)
                state = "WAITING" if alive and waiting_until and waiting_until > time.time() else "RUNNING" if alive else "STOPPED"
                if not entry["enabled"]:
                    state = "DISABLED"
                services.append({"key": key, "label": entry["label"], "enabled": entry["enabled"],
                                 "alive": alive, "state": state, "thread_id": worker.ident if worker else None,
                                 "heartbeat_at": datetime.fromtimestamp(heartbeat, timezone.utc).isoformat() if heartbeat else None,
                                 "next_poll_at": datetime.fromtimestamp(waiting_until, timezone.utc).isoformat() if waiting_until else None,
                                 "restart_count": entry["restarts"], "last_error": entry["last_error"]})
            return {"monitor_alive": self.is_alive(), "services": services,
                    "all_enabled_alive": all(s["alive"] for s in services if s["enabled"])}

    def run(self):
        while not self.stop_event.wait(10):
            self.check_once()

    def stop(self):
        self.stop_event.set()
        with self.lock:
            for entry in self.entries.values():
                if entry["worker"]:
                    entry["worker"].stop()
