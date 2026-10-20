import json
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


def test_ten_thousand_queue_is_parallel_bounded_and_cancellable(client, monkeypatch):
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
        assert release.wait(10)
        return tester.result("error" if stop.is_set() else "healthy")
    manager.probe_function = fake
    manager.start(ids, {"workers": 8, "samples": 2, "timeout": 4})
    try:
        wait_until(lambda: len(entered) == 8)
        snapshot = manager.snapshot()
        assert snapshot["active"] == 8 and snapshot["queued"] == 9992
        assert all(o["samples"] == 2 and o["timeout"] == 4 for o in entered)
        changes = client.get("/api/configs?cursor=" + initial["cursor"]).json
        assert not changes["full"] and len(changes["configs"]) == 8
        manager.cancel()
    finally:
        release.set()
    wait_until(lambda: not manager.snapshot()["running"])
    assert len(entered) == 8
    with connect(manager.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM configs WHERE status='pending'").fetchone()[0] == 9992


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


@pytest.mark.parametrize("options", [{"workers": 0}, {"workers": 33}, {"workers": True},
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
    from panel.store import initialize
    path = tmp_path / "panel.sqlite3"
    with sqlite3.connect(path) as db:
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

