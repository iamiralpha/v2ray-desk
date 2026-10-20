import json
import threading
from datetime import datetime, timezone

import pytest

from panel.automation import AutomationManager, CONFIG_KEY, next_daily_run
from panel.parser import ConfigError
from panel.store import connect, initialize
from test_panel import LINK, add, client, send


class FakeTester:
    def __init__(self):
        self.lock = threading.RLock()
        self.state = {"running": False, "done": 0, "cancelled": False, "auto_deleted": 0}
        self.calls = []

    def invalidate(self):
        pass

    def snapshot(self):
        return dict(self.state)

    def start(self, ids, options):
        self.calls.append((ids, options))
        self.state["done"] = len(ids)


def due_scheduler(tmp_path):
    path = initialize(tmp_path)
    tester, syncing = FakeTester(), set()
    scheduler = AutomationManager(path, tester, syncing)
    scheduler.state["next_run"] = "2020-01-01T00:00:00+00:00"
    return scheduler, tester, syncing


def test_tehran_day_boundary_and_exact_time():
    now = datetime(2026, 10, 21, 23, 29, tzinfo=timezone.utc)
    assert next_daily_run("03:00", now) == "2026-10-21T23:30:00+00:00"
    assert next_daily_run("03:00", now.replace(minute=30)) == "2026-10-22T23:30:00+00:00"


def test_refreshes_enabled_sources_then_tests_all_and_only_once(tmp_path, monkeypatch):
    scheduler, tester, syncing = due_scheduler(tmp_path)
    with connect(scheduler.db_path) as db:
        db.execute("INSERT INTO subscriptions(name,url) VALUES ('on','https://example.com/on')")
        db.execute("INSERT INTO subscriptions(name,url,enabled) VALUES ('off','https://example.com/off',0)")
    calls = []

    def fetch(url):
        assert -1 in syncing and 1 in syncing
        assert not tester.calls
        calls.append(url)
        return LINK

    monkeypatch.setattr("panel.automation.fetch_subscription", fetch)
    assert scheduler.run_due()
    assert calls == ["https://example.com/on"]
    ids, options = tester.calls[0]
    assert len(ids) == 1
    assert options["workers"] == 100 and options["country"] and options["gemini"]
    assert not options["auto_delete_failed"]
    assert not syncing
    assert not scheduler.run_due()
    assert scheduler.snapshot()["last_summary"] == {"subscriptions": 1, "subscription_errors": 0, "tested": 1, "auto_deleted": 0}
    restored = AutomationManager(scheduler.db_path, tester, syncing)
    assert not restored.run_due()


@pytest.mark.parametrize("busy", ["test", "sync", "disabled"])
def test_daily_run_waits_for_manual_work_and_disabled_does_nothing(tmp_path, busy):
    scheduler, tester, syncing = due_scheduler(tmp_path)
    if busy == "test":
        tester.state["running"] = True
    elif busy == "sync":
        syncing.add(99)
    else:
        scheduler.configure({"enabled": False})
    original = scheduler.snapshot()["next_run"]
    assert not scheduler.run_due()
    assert scheduler.snapshot()["next_run"] == original
    assert not tester.calls


def test_source_failure_keeps_saved_configs_and_proceeds(tmp_path, monkeypatch):
    scheduler, tester, syncing = due_scheduler(tmp_path)
    from panel.subscriptions import reconcile
    with connect(scheduler.db_path) as db:
        db.execute("INSERT INTO subscriptions(name,url) VALUES ('feed','https://example.com/private-token')")
        reconcile(db, 1, LINK)

    def fail(url):
        raise RuntimeError(url)

    monkeypatch.setattr("panel.automation.fetch_subscription", fail)
    assert scheduler.run_due()
    assert tester.calls[0][0] == [1]
    assert scheduler.snapshot()["last_summary"]["subscription_errors"] == 1
    assert "private-token" not in json.dumps(scheduler.snapshot())
    with connect(scheduler.db_path) as db:
        assert "private-token" not in db.execute("SELECT error FROM subscriptions").fetchone()[0]
    assert not syncing


def test_missing_core_releases_reservation_and_does_not_retry_same_day(tmp_path, monkeypatch):
    scheduler, tester, syncing = due_scheduler(tmp_path)
    from panel.subscriptions import reconcile
    with connect(scheduler.db_path) as db:
        db.execute("INSERT INTO subscriptions(name,url,enabled) VALUES ('saved','https://example.com',0)")
        reconcile(db, 1, LINK)
    monkeypatch.setattr(tester, "start", lambda *_: (_ for _ in ()).throw(FileNotFoundError("private-error")))
    assert scheduler.run_due()
    assert not syncing and scheduler.snapshot()["phase"] == "idle"
    assert "private-error" not in scheduler.snapshot()["last_error"]
    assert not scheduler.run_due()


def test_interrupted_run_is_reported_on_restart(tmp_path):
    scheduler, tester, syncing = due_scheduler(tmp_path)
    state = scheduler.snapshot()
    state["phase"] = "testing"
    with connect(scheduler.db_path) as db:
        db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(state), CONFIG_KEY))
    restored = AutomationManager(scheduler.db_path, tester, syncing)
    assert restored.snapshot()["phase"] == "idle"
    assert "restart" in restored.snapshot()["last_error"]


def test_persistence_failure_releases_queue_reservation(tmp_path, monkeypatch):
    scheduler, tester, syncing = due_scheduler(tmp_path)
    monkeypatch.setattr(scheduler, "_save", lambda: (_ for _ in ()).throw(OSError("database unavailable")))
    with pytest.raises(OSError):
        scheduler.run_due()
    assert not syncing and not tester.calls
    assert scheduler.snapshot()["phase"] == "idle"


def test_loop_survives_unavailable_error_persistence(tmp_path, monkeypatch):
    scheduler, _, syncing = due_scheduler(tmp_path)

    def stop_and_fail():
        scheduler.shutdown.set()
        raise OSError("unavailable")

    monkeypatch.setattr(scheduler, "run_due", stop_and_fail)
    monkeypatch.setattr(scheduler, "_save", lambda: (_ for _ in ()).throw(OSError("unavailable")))
    scheduler._loop()
    assert not syncing and scheduler.snapshot()["phase"] == "idle"


@pytest.mark.parametrize("bad", [{"time": "24:00"}, {"time": "3:00"}, {"workers": 129},
                                {"workers": True}, {"enabled": 1}, {"auto_delete_failed": "false"},
                                {"url": "http://example.com"}])
def test_schedule_validation(tmp_path, bad):
    scheduler, _, _ = due_scheduler(tmp_path)
    before = scheduler.snapshot()
    with pytest.raises(ConfigError):
        scheduler.configure(bad)
    assert scheduler.snapshot() == before


def test_schedule_routes_require_auth_csrf_and_save_settings(client):
    visitor = client.application.test_client()
    assert visitor.get("/api/automation").status_code == 401
    assert client.put("/api/automation", json={"enabled": False}).status_code == 403
    response = send(client, "/api/automation", {"enabled": True, "time": "04:15", "workers": 128,
                                              "auto_delete_failed": True}, "put")
    assert response.status_code == 200
    assert response.json["workers"] == 128 and response.json["auto_delete_failed"]
    assert client.get("/api/automation").json["time"] == "04:15"
    assert client.application.extensions["automation"].thread is None
