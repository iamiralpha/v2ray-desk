import json
import sqlite3
import threading
import time
from contextlib import contextmanager

import pytest

from panel import tester
from panel.parser import parse_uri
from panel.store import connect, initialize
from test_xray import XRAY, real_proxy


LINK = "vless://6b8744fb-aeb4-4fc7-938e-adb6b18b835a@example.com:443?security=tls"
DELAY_COLUMNS = "id,status,error,latency_ms,checked_at,successes,samples,delay_target,delay_samples,delay_codes"


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "Timed out waiting for test phase"
        time.sleep(.01)


def make_manager(tmp_path, monkeypatch, count):
    monkeypatch.setattr(tester, "engine_info", lambda _: {"available": True, "version": "synthetic"})
    monkeypatch.setattr(tester.shutil, "which", lambda _: "/synthetic-xray")
    monkeypatch.setattr(tester, "public_address", lambda *args: "1.1.1.1")
    db_path = initialize(tmp_path / "data")
    with connect(db_path) as db:
        for index in range(1, count + 1):
            parsed = parse_uri(LINK.replace("example.com", f"node{index}.example.com"))
            db.execute("""INSERT INTO configs(uri,name,protocol,host,port,fingerprint,real_failure_streak)
                VALUES (?,?,?,?,?,?,1)""", [parsed[key] for key in ("uri", "name", "protocol", "host", "port", "fingerprint")])
    manager = tester.TestManager(db_path)
    @contextmanager
    def core(prepared, executable, stop):
        yield {row["id"]: row["id"] for row in prepared}
    monkeypatch.setattr(tester, "shared_core", core)
    return manager, list(range(1, count + 1))


def delay_rows(manager):
    with connect(manager.db_path) as db:
        return [tuple(row) for row in db.execute(f"SELECT {DELAY_COLUMNS} FROM configs ORDER BY id")]


def healthy_delay(port, stop, **options):
    assert options["gemini"] is False and options["country"] is False
    return tester.result("healthy", latency_ms=port + 10, successes=2, samples=2,
                         delay_target="google", delay_samples=json.dumps([port + 11, port + 10]),
                         delay_codes="[204, 204]")


def test_all_delays_persist_before_bounded_parallel_metadata(tmp_path, monkeypatch):
    manager, ids = make_manager(tmp_path, monkeypatch, 260)
    delay_blocked, release_delay, release_metadata = threading.Event(), threading.Event(), threading.Event()
    metadata, cores, resolutions = [], [], []
    guard = threading.Lock()
    active_cores = peak_cores = 0
    active_metadata = peak_metadata = 0

    def resolve(host, port):
        with guard:
            resolutions.append(host)
        return "1.1.1.1"

    @contextmanager
    def core(prepared, executable, stop):
        nonlocal active_cores, peak_cores
        active_cores += 1
        peak_cores = max(peak_cores, active_cores)
        cores.append((manager.snapshot()["phase"], len(prepared)))
        assert all(row["address"] == "1.1.1.1" for row in prepared)
        try:
            yield {row["id"]: row["id"] for row in prepared}
        finally:
            active_cores -= 1

    def measure(port, target, timeout, stop):
        if port == ids[-1]:
            delay_blocked.set()
            assert release_delay.wait(20)
        return port + 10, [port + 11, port + 10], [204, 204]

    def gemini(port, timeout, stop):
        nonlocal active_metadata, peak_metadata
        with guard:
            metadata.append(port)
            active_metadata += 1
            peak_metadata = max(peak_metadata, active_metadata)
        try:
            if manager.snapshot().get("phase") == "metadata":
                assert release_metadata.wait(20)
            return "blocked", 403
        finally:
            with guard:
                active_metadata -= 1

    monkeypatch.setattr(tester, "public_address", resolve)
    monkeypatch.setattr(tester, "shared_core", core)
    monkeypatch.setattr(tester, "measure_delay", measure)
    monkeypatch.setattr(tester, "lookup_country", lambda *args: ("DE", "1.1.1.1"))
    monkeypatch.setattr(tester, "probe_gemini", gemini)
    manager.start(ids, {"workers": 100})
    try:
        assert delay_blocked.wait(12)
        wait_until(lambda: manager.snapshot()["done"] == 259)
        assert metadata == []
        snapshot = manager.snapshot()
        assert snapshot["phase"] == "delay" and snapshot["phase_done"] == 259 and snapshot["phase_total"] == 260
        with connect(manager.db_path) as db:
            assert db.execute("SELECT COUNT(*) FROM configs WHERE status='healthy' AND checked_at IS NOT NULL").fetchone()[0] == 259
            assert db.execute("SELECT COUNT(*) FROM configs WHERE gemini_status='untested'").fetchone()[0] == 260
        release_delay.set()
        wait_until(lambda: len(metadata) == 100)
        snapshot = manager.snapshot()
        assert snapshot["running"] and snapshot["phase"] == "metadata"
        assert snapshot["done"] == snapshot["total"] == 260
        assert snapshot["phase_done"] == 0 and snapshot["phase_total"] == 260
        assert snapshot["active"] == 100 and snapshot["queued"] == 160
        assert snapshot["rate"] == 0 and snapshot["eta"] is None
        original_delays = delay_rows(manager)
    finally:
        release_delay.set()
        release_metadata.set()
        wait_until(lambda: not manager.snapshot()["running"], timeout=20)
    assert delay_rows(manager) == original_delays
    assert cores == [(phase, size) for phase in ("delay", "metadata") for size in (128, 128, 4)]
    assert len(resolutions) == 260 and len(metadata) == len(set(metadata)) == 260
    assert peak_metadata == 100 and peak_cores == 1 and active_cores == 0
    assert manager.snapshot()["phase"] == "idle" and manager.snapshot()["phase_done"] == 260
    with connect(manager.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM configs WHERE status='healthy' AND country='DE' AND gemini_status='blocked' AND gemini_code=403").fetchone()[0] == 260


def test_stop_during_metadata_preserves_delays_and_untested_metadata(tmp_path, monkeypatch):
    manager, ids = make_manager(tmp_path, monkeypatch, 6)
    entered = threading.Event()
    def delay(port, stop, **options):
        return (tester.result("failed", real_delay_connection_failure=True, samples=2) if port == 1
                else healthy_delay(port, stop, **options))
    def metadata(port, timeout, stop):
        entered.set()
        assert stop.wait(10)
        return "unreachable", None
    monkeypatch.setattr(tester, "probe_connection", delay)
    monkeypatch.setattr(tester, "probe_gemini", metadata)
    manager.start(ids, {"workers": 2, "country": False, "auto_delete_failed": True})
    try:
        assert entered.wait(5)
        original = delay_rows(manager)
        outcomes = dict(manager._outcomes)
        assert manager.snapshot()["done"] == 6 and manager.snapshot()["phase"] == "metadata"
    finally:
        manager.cancel()
        wait_until(lambda: not manager.snapshot()["running"])
    assert delay_rows(manager) == original and manager._outcomes == outcomes
    assert manager.snapshot()["cancelled"] and manager.snapshot()["phase_done"] == 0
    assert manager.snapshot()["auto_deleted"] == 0
    with connect(manager.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM configs WHERE gemini_status='untested' AND real_failure_streak=1").fetchone()[0] == 6


def test_stop_during_delay_never_starts_metadata(tmp_path, monkeypatch):
    manager, ids = make_manager(tmp_path, monkeypatch, 6)
    entered = threading.Event()
    metadata = []
    def delay(port, stop, **options):
        if port == 6:
            entered.set()
            assert stop.wait(10)
            return tester.result("error", "Cancelled")
        return healthy_delay(port, stop, **options)
    monkeypatch.setattr(tester, "probe_connection", delay)
    monkeypatch.setattr(tester, "probe_gemini", lambda *args: metadata.append(args))
    manager.start(ids, {"workers": 2, "country": False, "auto_delete_failed": True})
    try:
        assert entered.wait(5)
        wait_until(lambda: manager.snapshot()["done"] == 5)
    finally:
        manager.cancel()
        wait_until(lambda: not manager.snapshot()["running"])
    assert metadata == [] and manager.snapshot()["done"] == 6
    with connect(manager.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM configs WHERE real_failure_streak=1").fetchone()[0] == 6


@pytest.mark.parametrize("when", ["startup", "after_probes"])
def test_metadata_core_failure_keeps_delay_outcomes_and_disables_deletion(tmp_path, monkeypatch, when):
    manager, ids = make_manager(tmp_path, monkeypatch, 2)
    cores, saved = [], []
    @contextmanager
    def core(prepared, executable, stop):
        phase = manager.snapshot()["phase"]
        cores.append(phase)
        if phase == "metadata":
            saved.extend(delay_rows(manager))
            if when == "startup":
                raise OSError("Synthetic unavailable core")
        yield {row["id"]: row["id"] for row in prepared}
        if phase == "metadata":
            raise tester.CoreStoppedError("Synthetic core crash")
    def delay(port, stop, **options):
        return (tester.result("failed", real_delay_connection_failure=True, samples=2) if port == 1
                else healthy_delay(port, stop, **options))
    monkeypatch.setattr(tester, "shared_core", core)
    monkeypatch.setattr(tester, "probe_connection", delay)
    monkeypatch.setattr(tester, "probe_gemini", lambda *args: ("blocked", 403))
    manager.start(ids, {"workers": 2, "country": False, "auto_delete_failed": True})
    wait_until(lambda: not manager.snapshot()["running"])
    assert cores == ["delay", "metadata"] and delay_rows(manager) == saved
    assert manager._outcomes == {1: "failed", 2: "healthy"} and manager._job_unsafe
    assert manager.snapshot()["done"] == manager.snapshot()["phase_done"] == 2
    assert manager.snapshot()["auto_deleted"] == 0
    with connect(manager.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM configs WHERE real_failure_streak=1").fetchone()[0] == 2


@pytest.mark.parametrize("country,gemini,expected_total", [(False, False, 0), (True, False, 1),
                                                         (False, True, 2), (True, True, 2)])
def test_metadata_eligibility_and_disabled_pass(tmp_path, monkeypatch, country, gemini, expected_total):
    manager, ids = make_manager(tmp_path, monkeypatch, 4)
    country_calls, gemini_calls, cores = [], [], []
    def resolve(host, port):
        if host == "node2.example.com":
            raise tester.ConfigError("Synthetic DNS failure")
        return "1.1.1.1"
    def validate(prepared, executable):
        if any(row["id"] == 1 for row in prepared):
            raise tester.InvalidConfigError("Synthetic invalid setting")
    @contextmanager
    def core(prepared, executable, stop):
        validate(prepared, executable)
        cores.append(manager.snapshot()["phase"])
        yield {row["id"]: row["id"] for row in prepared}
    def delay(port, stop, **options):
        return (tester.result("failed", real_delay_connection_failure=True, samples=2) if port == 3
                else healthy_delay(port, stop, **options))
    def country_probe(port, *args):
        country_calls.append(port)
        return "DE", "1.1.1.1"
    def gemini_probe(port, *args):
        gemini_calls.append(port)
        return "blocked", 403
    monkeypatch.setattr(tester, "public_address", resolve)
    monkeypatch.setattr(tester, "validate_batch", validate)
    monkeypatch.setattr(tester, "shared_core", core)
    monkeypatch.setattr(tester, "probe_connection", delay)
    monkeypatch.setattr(tester, "lookup_country", country_probe)
    monkeypatch.setattr(tester, "probe_gemini", gemini_probe)
    manager.start(ids, {"workers": 2, "country": country, "gemini": gemini})
    wait_until(lambda: not manager.snapshot()["running"])
    assert cores == ["delay"] + (["metadata"] if expected_total else [])
    assert country_calls == ([4] if country else [])
    assert sorted(gemini_calls) == ([3, 4] if gemini else [])
    assert manager.snapshot()["done"] == 4
    if expected_total:
        assert manager.snapshot()["phase_done"] == manager.snapshot()["phase_total"] == expected_total
    with connect(manager.db_path) as db:
        assert [row[0] for row in db.execute("SELECT status FROM configs ORDER BY id")] == ["invalid", "error", "failed", "healthy"]


def test_metadata_request_failures_do_not_replace_delay_failure_classification(tmp_path, monkeypatch):
    manager, ids = make_manager(tmp_path, monkeypatch, 2)
    def delay(port, stop, **options):
        return (tester.result("failed", real_delay_connection_failure=True, samples=2) if port == 1
                else healthy_delay(port, stop, **options))
    def unavailable(*args):
        raise OSError("Synthetic metadata failure")
    monkeypatch.setattr(tester, "probe_connection", delay)
    monkeypatch.setattr(tester, "probe_gemini", unavailable)
    monkeypatch.setattr(tester, "lookup_country", unavailable)
    manager.start(ids, {"workers": 2, "auto_delete_failed": True})
    wait_until(lambda: not manager.snapshot()["running"])
    assert manager.snapshot()["done"] == 2 and manager.snapshot()["auto_deleted"] == 1
    assert not manager._job_unsafe
    with connect(manager.db_path) as db:
        rows = db.execute("SELECT id,status,latency_ms,gemini_status,real_failure_streak FROM configs").fetchall()
        assert db.execute("SELECT COUNT(*) FROM rejected_configs").fetchone()[0] == 1
    assert [tuple(row) for row in rows] == [(2, "healthy", 12, "unreachable", 0)]


def test_metadata_database_contention_does_not_block_stop(tmp_path, monkeypatch):
    manager, ids = make_manager(tmp_path, monkeypatch, 1)
    ready, release, writing, cancelled = (threading.Event() for _ in range(4))
    def metadata(*args):
        ready.set()
        assert release.wait(5)
        return "blocked", 403
    class ObservedConnection:
        def __init__(self, db):
            self.db = db
        def execute(self, sql, *args):
            if manager.state["phase"] == "metadata" and (sql == "BEGIN IMMEDIATE" or sql.startswith("UPDATE configs")):
                writing.set()
            return self.db.execute(sql, *args)
        def __getattr__(self, name):
            return getattr(self.db, name)
    @contextmanager
    def observed_connect(path):
        with connect(path) as db:
            yield ObservedConnection(db)
    monkeypatch.setattr(tester, "connect", observed_connect)
    monkeypatch.setattr(tester, "probe_connection", healthy_delay)
    monkeypatch.setattr(tester, "probe_gemini", metadata)
    manager.start(ids, {"workers": 1, "country": False})
    blocker, cancelling = None, None
    try:
        assert ready.wait(5)
        blocker = sqlite3.connect(manager.db_path)
        blocker.execute("BEGIN IMMEDIATE")
        release.set()
        assert writing.wait(3)
        def cancel():
            manager.cancel()
            cancelled.set()
        cancelling = threading.Thread(target=cancel)
        cancelling.start()
        assert cancelled.wait(.5), "SQLite writer contention must not hold the manager lock"
    finally:
        release.set()
        if blocker:
            blocker.rollback()
            blocker.close()
        if cancelling:
            cancelling.join(5)
        manager.cancel()
        wait_until(lambda: not manager.snapshot()["running"])
    with connect(manager.db_path) as db:
        row = db.execute("SELECT status,gemini_status FROM configs").fetchone()
    assert tuple(row) == ("healthy", "untested")


@pytest.mark.skipif(not XRAY, reason="Set XRAY_TEST_BIN to run real engine tests")
def test_real_shared_cores_run_delay_then_metadata(real_proxy, tmp_path, monkeypatch):
    db_path = initialize(tmp_path / "two-pass")
    with connect(db_path) as db:
        for index in range(2):
            parsed = parse_uri(real_proxy)
            parsed["fingerprint"] += str(index)
            db.execute("INSERT INTO configs(uri,name,protocol,host,port,fingerprint) VALUES (?,?,?,?,?,?)",
                       [parsed[key] for key in ("uri", "name", "protocol", "host", "port", "fingerprint")])
    manager = tester.TestManager(db_path, XRAY)
    original = tester.subprocess.Popen
    cores, original_delays = [], []
    def tracked(args, **kwargs):
        process = original(args, **kwargs)
        if "run" in args and "-test" not in args:
            phase = manager.snapshot()["phase"]
            cores.append((phase, process))
            if phase == "metadata":
                assert manager.snapshot()["done"] == 2
                original_delays.extend(delay_rows(manager))
        return process
    monkeypatch.setattr(tester.subprocess, "Popen", tracked)
    manager.start([1, 2], {"workers": 2, "timeout": 2})
    wait_until(lambda: not manager.snapshot()["running"], timeout=15)
    assert [phase for phase, process in cores] == ["delay", "metadata"]
    assert all(process.poll() is not None for phase, process in cores)
    assert delay_rows(manager) == original_delays
    assert manager.snapshot()["done"] == manager.snapshot()["phase_done"] == 2
    with connect(db_path) as db:
        rows = db.execute("SELECT status,latency_ms,country,gemini_status,gemini_code FROM configs").fetchall()
    assert all(row["status"] == "healthy" and row["latency_ms"] >= 70 and row["country"] == "DE"
               and row["gemini_status"] == "blocked" and row["gemini_code"] == 403 for row in rows)
