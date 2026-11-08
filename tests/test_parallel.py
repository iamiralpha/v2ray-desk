import json
import subprocess
import threading
import time
from contextlib import contextmanager

import pytest

from test_panel import client, send, add, LINK
from panel import tester
from panel.store import connect


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for worker state")
        time.sleep(.01)


@pytest.mark.parametrize("workers", [8, 100, 128])
def test_ten_thousand_queue_is_parallel_bounded_and_cancellable(client, monkeypatch, workers):
    manager = client.application.extensions["tester"]
    monkeypatch.setattr(tester.shutil, "which", lambda _: "/xray")
    with connect(manager.db_path) as db:
        db.executemany("""INSERT INTO configs(uri,name,protocol,host,port,fingerprint)
            VALUES (?,?, 'vless','example.com',443,?)""",
                       [(LINK, str(i), str(i)) for i in range(10000)])
    initial = client.get("/api/configs").json
    ids = [r["id"] for r in initial["configs"]]
    entered, release = [], threading.Event()
    guard = threading.Lock()
    def fake(uri, executable, stop, **options):
        with guard:
            entered.append(options)
        assert release.wait(20)
        return tester.result("error" if stop.is_set() else "healthy")
    manager.probe_function = fake
    manager.start(ids, {"workers": workers, "samples": 2, "timeout": 4})
    try:
        wait_until(lambda: len(entered) == workers, timeout=10)
        snapshot = manager.snapshot()
        assert snapshot["active"] == workers and snapshot["queued"] == 10000 - workers
        assert all(o["samples"] == 2 and o["timeout"] == 4 for o in entered)
        changes = client.get("/api/configs?cursor=" + initial["cursor"]).json
        assert not changes["full"] and len(changes["configs"]) == workers
    finally:
        manager.cancel()
        release.set()
    wait_until(lambda: not manager.snapshot()["running"], timeout=10)
    assert len(entered) == workers
    assert manager.snapshot()["done"] == workers and manager.snapshot()["active"] == 0
    with connect(manager.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM configs WHERE status='pending'").fetchone()[0] == 10000 - workers
        assert db.execute("SELECT COUNT(*) FROM configs WHERE status='error' AND checked_at IS NOT NULL").fetchone()[0] == workers


def test_hundred_workers_persist_results_across_bounded_shared_batches(client, monkeypatch):
    manager = client.application.extensions["tester"]
    with connect(manager.db_path) as db:
        db.executemany("""INSERT INTO configs(uri,name,protocol,host,port,fingerprint)
            VALUES (?,?, 'vless','example.com',443,?)""", [(LINK, str(i), str(i)) for i in range(300)])
        ids = [row[0] for row in db.execute("SELECT id FROM configs ORDER BY id")]
    entered = []
    batches = []
    release = threading.Event()
    guard = threading.Lock()
    active = peak = active_cores = peak_cores = 0

    @contextmanager
    def core(prepared, executable, stop):
        nonlocal active_cores, peak_cores
        active_cores += 1
        peak_cores = max(peak_cores, active_cores)
        batches.append(len(prepared))
        try:
            yield {row["id"]: 10000 + row["id"] for row in prepared}
        finally:
            active_cores -= 1

    def probe(port, stop, **options):
        nonlocal active, peak
        with guard:
            entered.append(port)
            active += 1
            peak = max(peak, active)
        try:
            assert release.wait(20)
            return tester.result("healthy", latency_ms=port, successes=2, samples=2)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(tester.shutil, "which", lambda _: "/xray")
    monkeypatch.setattr(tester, "public_address", lambda *args: "1.1.1.1")
    monkeypatch.setattr(tester, "shared_core", core)
    monkeypatch.setattr(tester, "probe_connection", probe)
    manager.start(ids, {"workers": 100, "country": False, "gemini": False})
    try:
        wait_until(lambda: len(entered) == 100, timeout=10)
        assert manager.snapshot()["active"] == 100
        assert manager.snapshot()["queued"] == 200
    finally:
        release.set()
    wait_until(lambda: not manager.snapshot()["running"], timeout=15)
    assert peak == 100 and active == 0
    assert len(entered) == len(set(entered)) == 300
    assert batches == [128, 128, 44] and peak_cores == 1 and active_cores == 0
    assert manager.snapshot()["done"] == 300
    with connect(manager.db_path) as db:
        rows = db.execute("SELECT id,status,latency_ms,samples,successes FROM configs ORDER BY id").fetchall()
    assert all(row["status"] == "healthy" and row["latency_ms"] == 10000 + row["id"]
               and row["samples"] == row["successes"] == 2 for row in rows)


def prepared_batch(client, monkeypatch, count=128):
    manager = client.application.extensions["tester"]
    with connect(manager.db_path) as db:
        db.executemany("""INSERT INTO configs(uri,name,protocol,host,port,fingerprint,real_failure_streak)
            VALUES (?,?, 'vless','example.com',443,?,1)""", [(LINK, str(i), str(i)) for i in range(count)])
        ids = [row[0] for row in db.execute("SELECT id FROM configs ORDER BY id")]
    monkeypatch.setattr(tester.shutil, "which", lambda _: "/xray")
    monkeypatch.setattr(tester, "public_address", lambda *args: "1.1.1.1")
    return manager, ids


def test_invalid_isolation_keeps_hundred_valid_workers_in_one_core(client, monkeypatch):
    manager, ids = prepared_batch(client, monkeypatch)
    invalid = {ids[0], ids[64], ids[-1]}
    validations, running_cores, entered = [], [], []
    release, guard = threading.Event(), threading.Lock()

    def validate(prepared, executable):
        assert not entered, "Network testing must wait until isolation is complete"
        validations.append([row["id"] for row in prepared])
        if any(row["id"] in invalid for row in prepared):
            raise tester.InvalidConfigError("Synthetic invalid settings")

    @contextmanager
    def core(prepared, executable, stop):
        if any(row["id"] in invalid for row in prepared):
            raise tester.InvalidConfigError("Synthetic invalid settings")
        running_cores.append([row["id"] for row in prepared])
        yield {row["id"]: 10000 + row["id"] for row in prepared}

    def probe(port, stop, **options):
        with guard:
            entered.append(port)
        assert release.wait(15)
        return tester.result("healthy", latency_ms=100, successes=2, samples=2)

    monkeypatch.setattr(tester, "validate_batch", validate)
    monkeypatch.setattr(tester, "shared_core", core)
    monkeypatch.setattr(tester, "probe_connection", probe)
    manager.start(ids, {"workers": 100, "country": False, "gemini": False, "auto_delete_failed": True})
    try:
        wait_until(lambda: len(entered) == 100)
        assert manager.snapshot()["active"] == 100
    finally:
        release.set()
        wait_until(lambda: not manager.snapshot()["running"], timeout=10)
    assert validations and running_cores == [[config_id for config_id in ids if config_id not in invalid]]
    assert len(entered) == len(set(entered)) == 125
    assert manager.snapshot()["done"] == 128 and manager.snapshot()["auto_deleted"] == 0
    with connect(manager.db_path) as db:
        rows = db.execute("SELECT id,status,real_failure_streak FROM configs ORDER BY id").fetchall()
    assert all(row["status"] == ("invalid" if row["id"] in invalid else "healthy") for row in rows)
    assert all(row["real_failure_streak"] == (1 if row["id"] in invalid else 0) for row in rows)


def test_stop_during_invalid_isolation_does_not_launch_survivor_core(client, monkeypatch):
    manager, ids = prepared_batch(client, monkeypatch)
    validating, release = threading.Event(), threading.Event()
    attempts = []

    @contextmanager
    def core(prepared, executable, stop):
        attempts.append(len(prepared))
        raise tester.InvalidConfigError("Synthetic invalid settings")
        yield  # This context never starts a running core.

    def validate(prepared, executable):
        validating.set()
        assert release.wait(10)

    monkeypatch.setattr(tester, "validate_batch", validate)
    monkeypatch.setattr(tester, "shared_core", core)
    manager.start(ids, {"workers": 100, "auto_delete_failed": True})
    try:
        assert validating.wait(3)
        manager.cancel()
    finally:
        release.set()
        wait_until(lambda: not manager.snapshot()["running"])
    assert attempts == [128]
    assert manager.snapshot()["cancelled"] and manager.snapshot()["done"] == 0
    with connect(manager.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM configs WHERE status='pending' AND real_failure_streak=1").fetchone()[0] == 128


@pytest.mark.parametrize("failure", [OSError("Synthetic resource failure"),
                                     subprocess.TimeoutExpired("xray", 12),
                                     tester.ConfigError("Synthetic startup failure")])
@pytest.mark.parametrize("during_isolation", [False, True])
def test_environment_failure_does_not_recursively_launch_cores(client, monkeypatch, failure, during_isolation):
    manager, ids = prepared_batch(client, monkeypatch)
    attempts, validations = [], []

    @contextmanager
    def core(prepared, executable, stop):
        attempts.append(len(prepared))
        if during_isolation:
            raise tester.InvalidConfigError("Synthetic invalid settings")
        raise failure
        yield  # No running core exists after either failure.

    def validate(prepared, executable):
        validations.append(len(prepared))
        raise failure

    monkeypatch.setattr(tester, "validate_batch", validate)
    monkeypatch.setattr(tester, "shared_core", core)
    manager.start(ids, {"workers": 100, "auto_delete_failed": True})
    wait_until(lambda: not manager.snapshot()["running"])
    assert attempts == [128] and validations == ([64] if during_isolation else [])
    assert manager._job_unsafe and manager.snapshot()["done"] == 128
    assert manager.snapshot()["auto_deleted"] == 0
    with connect(manager.db_path) as db:
        rows = db.execute("SELECT status,error,real_failure_streak FROM configs").fetchall()
        assert db.execute("SELECT COUNT(*) FROM rejected_configs").fetchone()[0] == 0
    assert len(rows) == 128
    assert all(row["status"] == "error" and row["real_failure_streak"] == 1 for row in rows)
    assert all("Synthetic" not in row["error"] for row in rows)


def test_stop_during_core_validation_does_not_start_process(monkeypatch):
    stop = threading.Event()
    prepared = [{"id": 1, "parsed": tester.parse_uri(LINK), "address": "1.1.1.1"}]
    monkeypatch.setattr(tester, "engine_validation", lambda *args: stop.set())
    def unexpected_process(*args, **kwargs):
        pytest.fail("Stop during validation must prevent core startup")
    monkeypatch.setattr(tester.subprocess, "Popen", unexpected_process)
    with tester.shared_core(prepared, "/xray", stop) as ports:
        assert ports == {}


def test_recombined_validation_failure_does_not_repeat_isolation(client, monkeypatch):
    manager, ids = prepared_batch(client, monkeypatch, count=4)
    attempts, validations = [], []

    @contextmanager
    def core(prepared, executable, stop):
        attempts.append(len(prepared))
        raise tester.InvalidConfigError("Synthetic inconsistent engine failure")
        yield

    def validate(prepared, executable):
        validations.append(len(prepared))

    monkeypatch.setattr(tester, "validate_batch", validate)
    monkeypatch.setattr(tester, "shared_core", core)
    manager.start(ids, {"workers": 4, "auto_delete_failed": True})
    wait_until(lambda: not manager.snapshot()["running"])
    assert attempts == [4, 4] and validations == [2, 2]
    assert manager._job_unsafe and manager.snapshot()["done"] == 4
    with connect(manager.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM configs WHERE status='error' AND real_failure_streak=1").fetchone()[0] == 4


@pytest.mark.parametrize("returncode,diagnostic", [(-9, b""), (3221225477, b""),
    (2, b"fatal error: runtime memory failure with password=secret"),
    (1, b"permission denied reading configuration")])
def test_validation_process_crash_is_unsafe_without_recursive_retries(client, monkeypatch, returncode, diagnostic):
    manager, ids = prepared_batch(client, monkeypatch, count=4)
    validations = []
    def crashed(args, **kwargs):
        validations.append(args)
        return subprocess.CompletedProcess(args, returncode, b"", diagnostic)
    monkeypatch.setattr(tester.subprocess, "run", crashed)
    manager.start(ids, {"workers": 4, "auto_delete_failed": True})
    wait_until(lambda: not manager.snapshot()["running"])
    assert len(validations) == 1 and manager._job_unsafe
    assert manager.snapshot()["done"] == 4 and manager.snapshot()["auto_deleted"] == 0
    with connect(manager.db_path) as db:
        rows = db.execute("SELECT status,error,real_failure_streak FROM configs").fetchall()
    assert all(row["status"] == "error" and row["real_failure_streak"] == 1 for row in rows)
    assert all("secret" not in row["error"] for row in rows)


def test_single_probe_reports_validation_crash_as_error(monkeypatch):
    monkeypatch.setattr(tester, "public_address", lambda *args: "1.1.1.1")
    monkeypatch.setattr(tester.subprocess, "run", lambda args, **kwargs:
                        subprocess.CompletedProcess(args, -9, b"", b""))
    outcome = tester.probe(LINK, "/xray", threading.Event())
    assert outcome["status"] == "error"


def test_completed_results_are_not_held_behind_slow_first_worker(client, monkeypatch):
    add(client, LINK + "\n" + LINK.replace("example.com", "other.example.com"))
    manager = client.application.extensions["tester"]
    monkeypatch.setattr(tester.shutil, "which", lambda _: "/xray")
    release = threading.Event()
    def fake(uri, *args, **kwargs):
        if "other.example.com" not in uri:
            assert release.wait(10)
        return tester.result("healthy", latency_ms=100)
    manager.probe_function = fake
    ids = [r["id"] for r in client.get("/api/configs").json["configs"]]
    manager.start(ids, {"workers": 2})
    try:
        wait_until(lambda: manager.snapshot()["done"] == 1)
        assert manager.snapshot()["running"]
        rows = client.get("/api/configs").json["configs"]
        assert any(r["status"] == "healthy" for r in rows)
    finally:
        release.set()
    wait_until(lambda: not manager.snapshot()["running"])


@pytest.mark.parametrize("failure", ["invalid_label", "resolver_error", "resolver_unicode_error"])
def test_bad_host_does_not_abandon_valid_batch_neighbor(client, monkeypatch, failure):
    manager = client.application.extensions["tester"]
    bad_host = "a" * 64 + ".example.com" if failure == "invalid_label" else "unavailable.example.com"
    # An older database may contain a hostname now rejected by the parser.
    with connect(manager.db_path) as db:
        for name, host in [("bad", bad_host), ("valid", "example.com")]:
            db.execute("""INSERT INTO configs(uri,name,protocol,host,port,fingerprint)
                VALUES (?,?, 'vless', ?,443,?)""", (LINK.replace("example.com", host), name, host, name))
        ids = [row[0] for row in db.execute("SELECT id FROM configs ORDER BY id")]

    def resolve(host, *args, **kwargs):
        if host == bad_host:
            assert failure != "invalid_label", "Invalid labels must be rejected before DNS resolution"
            error = UnicodeError if failure == "resolver_unicode_error" else OSError
            raise error("Synthetic resolver failure")
        return [(2, 1, 6, "", ("1.1.1.1", 443))]

    @contextmanager
    def core(prepared, executable, stop):
        yield {row["id"]: 12345 for row in prepared}

    monkeypatch.setattr(tester.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(tester.shutil, "which", lambda _: "/xray")
    monkeypatch.setattr(tester, "shared_core", core)
    monkeypatch.setattr(tester, "probe_connection", lambda *args, **kwargs: tester.result("healthy", latency_ms=100))
    manager.start(ids, {"workers": 2, "country": False, "gemini": False})
    wait_until(lambda: not manager.snapshot()["running"])
    assert manager.snapshot()["done"] == 2
    with connect(manager.db_path) as db:
        rows = db.execute("SELECT name,status,error FROM configs ORDER BY id").fetchall()
    assert rows[0]["status"] == ("invalid" if failure == "invalid_label" else "error")
    assert rows[0]["error"]
    assert rows[1]["status"] == "healthy"


@pytest.mark.parametrize("options", [{"workers": 0}, {"workers": 129}, {"workers": True},
                                    {"timeout": 100}, {"samples": 3}, {"target": []},
                                    {"target": "http://127.0.0.1"}, {"gemini": "yes"}, []])
def test_invalid_options_are_rejected(client, options):
    add(client)
    assert send(client, "/api/test", {"ids": [1], "options": options}).status_code == 400


def test_cursor_invalidation_and_unchanged_list(client):
    add(client)
    initial = client.get("/api/configs").json
    delta = client.get("/api/configs?cursor=" + initial["cursor"]).json
    assert delta["configs"] == [] and not delta["full"]
    send(client, "/api/delete", {"ids": [initial["configs"][0]["id"]]})
    delta = client.get("/api/configs?cursor=" + initial["cursor"]).json
    assert delta["configs"] == [] and delta["full"]
    assert client.get("/api/configs?cursor=invalid").json["full"]


def test_upgrade_preserves_existing_measurement_and_backfills_transport(tmp_path):
    import sqlite3
    from contextlib import closing
    from panel.store import initialize
    path = tmp_path / "panel.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TABLE configs (
            id INTEGER PRIMARY KEY,uri TEXT NOT NULL,name TEXT NOT NULL,protocol TEXT NOT NULL,
            host TEXT NOT NULL,port INTEGER NOT NULL,fingerprint TEXT NOT NULL UNIQUE,
            status TEXT DEFAULT 'pending',latency_ms REAL,country TEXT,exit_ip TEXT,
            checked_at TEXT,error TEXT,successes INTEGER DEFAULT 0,samples INTEGER DEFAULT 0,
            created_at TEXT)""")
        db.execute("""INSERT INTO configs(id,uri,name,protocol,host,port,fingerprint,status,latency_ms,country)
            VALUES (1,?,'saved','vless','example.com',443,'fingerprint','healthy',123,'DE')""", (LINK,))
    initialize(tmp_path)
    initialize(tmp_path)
    with connect(path) as db:
        row = db.execute("SELECT * FROM configs").fetchone()
    assert row["latency_ms"] == 123 and row["country"] == "DE" and row["uri"] == LINK
    assert row["delay_version"] == 1 and row["transport"] == "ws" and row["security"] == "tls"

