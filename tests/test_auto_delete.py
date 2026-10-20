import sqlite3
import threading
import time
from contextlib import contextmanager

import pytest

from panel import tester
from panel.parser import ConfigError, parse_uri
from panel.store import connect, initialize


LINK = "vless://6b8744fb-aeb4-4fc7-938e-adb6b18b835a@example.com:443?security=tls"


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(tester, "engine_info", lambda _: {"available": True, "version": "test"})
    monkeypatch.setattr(tester.shutil, "which", lambda _: "/synthetic-xray")
    path = initialize(tmp_path / "data")
    parsed = parse_uri(LINK)
    with connect(path) as db:
        db.execute("INSERT INTO configs(uri,name,protocol,host,port,fingerprint) VALUES (?,?,?,?,?,?)",
                   [parsed[key] for key in ("uri", "name", "protocol", "host", "port", "fingerprint")])
    return tester.TestManager(path)


def failure(**values):
    return tester.result("failed", real_delay_connection_failure=True, **values)


def wait_finished(manager):
    deadline = time.monotonic() + 5
    while manager.snapshot()["running"] and time.monotonic() < deadline:
        time.sleep(.01)
    assert not manager.snapshot()["running"]


def run_job(manager, outcome, enabled=True):
    manager.probe_function = lambda *args, **kwargs: dict(outcome)
    with connect(manager.db_path) as db:
        ids = [row[0] for row in db.execute("SELECT id FROM configs")]
    manager.start(ids, {"workers": 2, "auto_delete_failed": enabled})
    wait_finished(manager)
    assert manager.snapshot()["done"] == len(ids)


def stored(manager):
    with connect(manager.db_path) as db:
        row = db.execute("SELECT * FROM configs ORDER BY id").fetchone()
    return dict(row) if row else None


def test_two_completed_failure_jobs_delete_atomically_and_invalidate_cursor(manager):
    original = stored(manager)
    run_job(manager, failure(samples=3))
    assert stored(manager)["real_failure_streak"] == 1
    assert manager.snapshot()["auto_deleted"] == 0
    cursor = manager.delta(None)[0]
    run_job(manager, failure(samples=3))
    assert stored(manager) is None
    assert manager.snapshot()["auto_deleted"] == 1
    assert manager.delta(cursor)[1] is None
    with connect(manager.db_path) as db:
        assert db.execute("SELECT fingerprint FROM rejected_configs").fetchone()[0] == original["fingerprint"]


def test_success_resets_consecutive_failures_even_when_gemini_is_blocked(manager):
    run_job(manager, failure())
    run_job(manager, tester.result("healthy", latency_ms=12, gemini_status="blocked", gemini_code=403))
    assert stored(manager)["real_failure_streak"] == 0
    run_job(manager, failure())
    assert stored(manager)["real_failure_streak"] == 1


def test_disabled_option_tracks_failures_without_deleting(manager):
    run_job(manager, failure(), enabled=False)
    run_job(manager, failure(), enabled=False)
    assert stored(manager)["real_failure_streak"] == 2
    assert manager.snapshot()["auto_deleted"] == 0
    with connect(manager.db_path) as db:
        assert not db.execute("SELECT 1 FROM rejected_configs").fetchone()


@pytest.mark.parametrize("outcome", [tester.result("invalid"), tester.result("error"),
                                    tester.result("failed"), tester.result("pending")])
def test_unverified_errors_do_not_increment_or_delete(manager, outcome):
    run_job(manager, failure())
    run_job(manager, outcome)
    assert stored(manager)["real_failure_streak"] == 1
    assert manager.snapshot()["auto_deleted"] == 0


def test_received_http_response_breaks_failure_streak_after_incomplete_measurement(manager):
    run_job(manager, failure())
    run_job(manager, tester.result("failed", real_delay_response_received=True,
                                  delay_codes="[403]", real_delay_connection_failure=False))
    assert stored(manager)["real_failure_streak"] == 0
    run_job(manager, failure())
    assert stored(manager)["real_failure_streak"] == 1


def test_cancelled_job_cannot_delete_already_completed_failed_rows(manager):
    run_job(manager, failure())
    with connect(manager.db_path) as db:
        db.execute("INSERT INTO configs(uri,name,protocol,host,port,fingerprint) VALUES (?, 'second','vless','second.test',443,'second')",
                   (LINK.replace("example.com", "second.test"),))
    entered = threading.Event()
    def probe(uri, executable, stop, **options):
        if "second.test" in uri:
            entered.set()
            stop.wait(5)
        return failure()
    manager.probe_function = probe
    manager.start([1, 2], {"workers": 1, "auto_delete_failed": True})
    assert entered.wait(3)
    assert manager.snapshot()["done"] == 1
    assert stored(manager)["real_failure_streak"] == 1
    manager.cancel()
    wait_finished(manager)
    assert stored(manager)["real_failure_streak"] == 1
    assert manager.snapshot()["auto_deleted"] == 0
    with connect(manager.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM configs").fetchone()[0] == 2


def test_auto_delete_removes_all_memberships_and_keeps_subscriptions(manager):
    with connect(manager.db_path) as db:
        db.executemany("INSERT INTO subscriptions(id,name,url) VALUES (?,?,?)",
                       [(1, "a", "https://a.example/feed"), (2, "b", "https://b.example/feed")])
        db.executemany("INSERT INTO subscription_configs VALUES (?,1)", [(1,), (2,)])
    run_job(manager, failure())
    run_job(manager, failure())
    with connect(manager.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM subscription_configs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM rejected_configs").fetchone()[0] == 1


def test_core_crash_disqualifies_all_failure_outcomes_without_double_counting(manager, monkeypatch):
    run_job(manager, failure())
    manager.probe_function = tester.probe
    monkeypatch.setattr(tester, "public_address", lambda *args: "1.1.1.1")
    monkeypatch.setattr(tester, "probe_connection", lambda *args, **kwargs: failure())
    @contextmanager
    def failed_core(prepared, executable, stop):
        yield {row["id"]: 12345 for row in prepared}
        raise tester.CoreStoppedError("Synthetic core failure")
    monkeypatch.setattr(tester, "shared_core", failed_core)
    manager.start([1], {"auto_delete_failed": True})
    wait_finished(manager)
    assert stored(manager)["status"] == "error"
    assert stored(manager)["real_failure_streak"] == 1
    assert manager.snapshot()["done"] == 1
    assert manager.snapshot()["auto_deleted"] == 0


def test_streak_and_tombstone_rollback_together_on_deletion_failure(manager):
    with connect(manager.db_path) as db:
        db.execute("UPDATE configs SET real_failure_streak=1")
        db.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON configs BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
    manager._outcomes = {1: "failed"}
    with pytest.raises(sqlite3.IntegrityError):
        manager._finish_failure_streaks({"auto_delete_failed": True})
    assert stored(manager)["real_failure_streak"] == 1
    with connect(manager.db_path) as db:
        assert not db.execute("SELECT 1 FROM rejected_configs").fetchone()


@pytest.mark.parametrize("value", [1, 0, "true", None, []])
def test_auto_delete_option_requires_boolean(value):
    assert tester.DEFAULT_OPTIONS["auto_delete_failed"] is False
    with pytest.raises(ConfigError):
        tester.test_options({"auto_delete_failed": value})


def test_streak_survives_restart_without_recounting_saved_result(manager):
    run_job(manager, failure())
    restarted = tester.TestManager(manager.db_path)
    assert stored(restarted)["real_failure_streak"] == 1
    run_job(restarted, failure())
    assert stored(restarted) is None


def test_worker_thread_start_failure_releases_job_without_counting(manager, monkeypatch):
    run_job(manager, failure())
    with monkeypatch.context() as patch:
        def unavailable_thread(self):
            raise RuntimeError("synthetic thread limit")
        patch.setattr(threading.Thread, "start", unavailable_thread)
        with pytest.raises(RuntimeError, match="synthetic thread limit"):
            manager.start([1], {"auto_delete_failed": True})
    assert not manager.snapshot()["running"]
    assert stored(manager)["real_failure_streak"] == 1
    assert manager.snapshot()["auto_deleted"] == 0
    run_job(manager, tester.result("healthy"))
    assert stored(manager)["real_failure_streak"] == 0
