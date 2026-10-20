"""Persistent daily subscription refresh followed by a complete proxy test."""
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

from .parser import ConfigError
from .store import connect
from .subscriptions import fetch_subscription, reconcile
from .tester import MAX_WORKERS


TEHRAN = timezone(timedelta(hours=3, minutes=30), "Asia/Tehran")
CONFIG_KEY = "daily_automation"
DEFAULTS = {"enabled": True, "time": "03:00", "timezone": "Asia/Tehran",
            "workers": 100, "auto_delete_failed": False}


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def next_daily_run(clock, now):
    hour, minute = map(int, clock.split(":"))
    candidate = now.astimezone(TEHRAN).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return timestamp(candidate)


class AutomationManager:
    def __init__(self, db_path, tester, syncing):
        self.db_path, self.tester, self.syncing = db_path, tester, syncing
        self.shutdown = threading.Event()
        self.thread = None
        with tester.lock:
            with connect(db_path) as db:
                row = db.execute("SELECT value FROM settings WHERE key=?", (CONFIG_KEY,)).fetchone()
                if row:
                    self.state = json.loads(row[0])
                else:
                    self.state = {**DEFAULTS, "next_run": next_daily_run(DEFAULTS["time"], utc_now()),
                                  "last_started": None, "last_completed": None, "last_error": None,
                                  "phase": "idle", "last_summary": None}
                if self.state["phase"] != "idle":
                    self.state.update(phase="idle", last_error="The daily run was interrupted by a service restart.")
                self._persist(db)

    def _persist(self, db):
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
                   (CONFIG_KEY, json.dumps(self.state)))

    def _save(self):
        with connect(self.db_path) as db:
            self._persist(db)

    def snapshot(self):
        with self.tester.lock:
            return json.loads(json.dumps(self.state))

    def configure(self, supplied):
        keys = {"enabled", "time", "workers", "auto_delete_failed"}
        if not isinstance(supplied, dict) or set(supplied) - keys:
            raise ConfigError("Invalid daily schedule settings.")
        with self.tester.lock:
            if self.state["phase"] != "idle":
                raise ValueError("The daily run is in progress.")
            values = {key: supplied.get(key, self.state[key]) for key in keys}
            if (any(type(values[key]) is not bool for key in ("enabled", "auto_delete_failed"))
                    or type(values["workers"]) is not int or not 1 <= values["workers"] <= MAX_WORKERS
                    or not isinstance(values["time"], str)
                    or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", values["time"])):
                raise ConfigError("Invalid daily schedule settings.")
            changed = any(values[key] != self.state[key] for key in ("time", "enabled"))
            self.state.update(values)
            if changed or not self.state.get("next_run"):
                self.state["next_run"] = next_daily_run(values["time"], utc_now()) if values["enabled"] else None
            self._save()
            return self.snapshot()

    def start(self):
        with self.tester.lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._loop, name="daily-schedule", daemon=True)
            self.thread.start()

    def _loop(self):
        while not self.shutdown.is_set():
            try:
                self.run_due()
            except Exception:
                # Exception text can contain a private source URL or credentials.
                with self.tester.lock:
                    self.state.update(phase="idle", last_error="The daily run could not finish.")
                    try:
                        self._save()
                    except Exception as exc:
                        logging.getLogger(__name__).error("Daily status persistence failed: %s", type(exc).__name__)
            self.shutdown.wait(30)

    def run_due(self, now=None):
        now = now or utc_now()
        with self.tester.lock:
            if (not self.state["enabled"] or self.state["phase"] != "idle"
                    or self.tester.state["running"] or self.syncing
                    or datetime.fromisoformat(self.state["next_run"]) > now):
                return False
            with connect(self.db_path) as db:
                sources = [dict(row) for row in db.execute("SELECT id,url FROM subscriptions WHERE enabled=1 ORDER BY id")]
            reserved = {row["id"] for row in sources} | {-1}
            self.syncing.update(reserved)
            options = {"workers": self.state["workers"], "timeout": 5, "samples": 2,
                       "target": "google", "gemini": True, "country": True,
                       "auto_delete_failed": self.state["auto_delete_failed"]}
            previous = dict(self.state)
            self.state.update(phase="refreshing", last_started=timestamp(now), last_error=None,
                              next_run=next_daily_run(self.state["time"], now))
            try:
                self._save()
            except Exception:
                self.syncing.difference_update(reserved)
                self.state = previous
                raise
        summary = {"subscriptions": 0, "subscription_errors": 0, "tested": 0, "auto_deleted": 0}
        error = None
        try:
            for source in sources:
                if self.shutdown.is_set():
                    error = "The daily run was interrupted."
                    break
                try:
                    text = fetch_subscription(source["url"])
                    with self.tester.lock:
                        with connect(self.db_path) as db:
                            reconcile(db, source["id"], text)
                        self.tester.invalidate()
                    summary["subscriptions"] += 1
                except Exception as exc:
                    message = str(exc) if isinstance(exc, ConfigError) else "Subscription download failed."
                    with self.tester.lock, connect(self.db_path) as db:
                        db.execute("UPDATE subscriptions SET error=? WHERE id=?", (message, source["id"]))
                    summary["subscription_errors"] += 1
            if not error:
                with self.tester.lock:
                    with connect(self.db_path) as db:
                        ids = [row[0] for row in db.execute("SELECT id FROM configs ORDER BY id")]
                    if ids:
                        self.tester.start(ids, options)
                        self.state["phase"] = "testing"
                        self._save()
                if ids:
                    while self.tester.snapshot()["running"]:
                        if self.shutdown.wait(1):
                            error = "The daily run was interrupted."
                            break
                    job = self.tester.snapshot()
                    summary.update(tested=job["done"], auto_deleted=job.get("auto_deleted", 0))
                    if job.get("cancelled"):
                        error = "Daily testing was stopped."
            if not error and summary["subscription_errors"]:
                error = "Some subscriptions could not refresh; saved configurations were tested."
        except Exception:
            error = "Daily testing could not start or finish. Check the Xray service."
        finally:
            with self.tester.lock:
                self.syncing.difference_update(reserved)
                self.state.update(phase="idle", last_completed=timestamp(utc_now()),
                                  last_error=error, last_summary=summary)
                self._save()
        return True
