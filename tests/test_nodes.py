import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

from panel.app import create_app
from panel.parser import parse_uri
from panel.store import connect, initialize, save_password


@pytest.fixture
def app_and_clients(tmp_path):
    db = initialize(tmp_path)
    save_password(db, generate_password_hash("a-secure-test-password"))
    app = create_app(tmp_path, {"TESTING": True, "SESSION_COOKIE_SECURE": False})
    admin = app.test_client()
    admin.get("/login")
    with admin.session_transaction() as session:
        csrf = session["csrf"]
    assert admin.post("/login", data={"password": "a-secure-test-password", "csrf_token": csrf}).status_code == 302
    with admin.session_transaction() as session:
        csrf = session["csrf"]
    return app, admin, app.test_client(), {"X-CSRF-Token": csrf}


def add_config(db_path, suffix, status, checked="2026-11-01T01:00:00+00:00"):
    uri = f"vless://6b8744fb-aeb4-4fc7-938e-adb6b18b835a@node{suffix}.example.com:443?security=tls#node"
    parsed = parse_uri(uri)
    with connect(db_path) as db:
        cursor = db.execute("""INSERT INTO configs(uri,name,protocol,host,port,fingerprint,status,checked_at,latency_ms)
            VALUES (?,?,?,?,?,?,?,?,?)""", [parsed[k] for k in ("uri", "name", "protocol", "host", "port", "fingerprint")]
            + [status, checked, 80 if status == "healthy" else None])
        return cursor.lastrowid


def create_node(admin, csrf, name="ایران", location="iran"):
    response = admin.post("/api/nodes", json={"name": name, "location": location}, headers=csrf)
    assert response.status_code == 201
    return response.json["node"], response.json["token"]


def bearer(token):
    return {"Authorization": "Bearer " + token}


def test_enrollment_is_admin_only_and_token_is_only_shown_once(app_and_clients):
    app, admin, guest, csrf = app_and_clients
    assert guest.post("/api/nodes", json={"name": "Iran"}).status_code in (401, 403)
    assert admin.post("/api/nodes", json={"name": "Iran"}).status_code == 403
    node, token = create_node(admin, csrf)
    assert len(token) >= 40
    listing = admin.get("/api/nodes").json
    assert listing["nodes"][0]["name"] == "ایران"
    assert token not in json.dumps(listing)
    with connect(app.config["DB_PATH"]) as db:
        stored = db.execute("SELECT token_hash FROM nodes WHERE id=?", (node["id"],)).fetchone()[0]
    assert token not in stored and stored == hashlib.sha256(token.encode()).hexdigest()
    assert guest.post("/api/node/pull", headers=bearer("wrong"), json={}).status_code == 401


def test_node_receives_only_currently_healthy_configs_and_reports_separately(app_and_clients):
    app, admin, node_client, csrf = app_and_clients
    db_path = app.config["DB_PATH"]
    healthy = add_config(db_path, 1, "healthy")
    failed = add_config(db_path, 2, "failed")
    pending = add_config(db_path, 3, "pending")
    node, token = create_node(admin, csrf)
    received = node_client.post("/api/node/pull", json={}, headers=bearer(token))
    assert received.status_code == 200
    batch = received.json
    assert [entry["id"] for entry in batch["configs"]] == [healthy]
    assert batch["configs"][0]["uri"].startswith("vless://") and batch["lease"]
    result = {"lease": batch["lease"], "results": [{"id": healthy, "status": "healthy", "latency_ms": 42}]}
    assert node_client.post("/api/node/report", json=result, headers=bearer(token)).status_code == 200
    with connect(db_path) as db:
        assert tuple(db.execute("SELECT status,latency_ms FROM configs WHERE id=?", (healthy,)).fetchone()) == ("healthy", 80)
        assert db.execute("SELECT status,latency_ms FROM node_results WHERE node_id=? AND config_id=?", (node["id"], healthy)).fetchone()[:] == ("healthy", 42)
    assert node_client.post("/api/node/pull", json={}, headers=bearer(token)).json["configs"] == []
    public = node_client.get("/api/public/configs").json
    assert public["nodes"][0]["id"] == node["id"]
    rows = {r["id"]: r for r in public["configs"]}
    assert rows[healthy]["node_results"][str(node["id"])]["latency_ms"] == 42
    assert rows[failed]["node_results"] == rows[pending]["node_results"] == {}


def test_public_node_filter_uses_current_results_before_paginating(app_and_clients):
    app, admin, guest, csrf = app_and_clients
    db_path = app.config["DB_PATH"]
    configs = [add_config(db_path, number, "healthy") for number in range(55)]
    node, _ = create_node(admin, csrf)
    second_iran, _ = create_node(admin, csrf, name="شیراز")
    other, _ = create_node(admin, csrf, name="Outside", location="other")
    with connect(db_path) as db:
        rows = {row["id"]: row for row in db.execute("SELECT id,fingerprint,checked_at FROM configs")}
        results = [(node["id"], config, rows[config]["fingerprint"], rows[config]["checked_at"],
                    "healthy", 25, rows[config]["checked_at"]) for config in configs[:52]]
        results += [
            (node["id"], configs[52], rows[configs[52]]["fingerprint"], rows[configs[52]]["checked_at"], "failed", None, rows[configs[52]]["checked_at"]),
            (node["id"], configs[53], rows[configs[53]]["fingerprint"], "2020-01-01", "healthy", 20, "2020-01-01"),
            (node["id"], configs[54], "outdated-fingerprint", rows[configs[54]]["checked_at"], "healthy", 20, rows[configs[54]]["checked_at"]),
            (second_iran["id"], configs[52], rows[configs[52]]["fingerprint"], rows[configs[52]]["checked_at"], "healthy", 30, rows[configs[52]]["checked_at"]),
            (other["id"], configs[54], rows[configs[54]]["fingerprint"], rows[configs[54]]["checked_at"], "healthy", 30, rows[configs[54]]["checked_at"]),
        ]
        db.executemany("""INSERT INTO node_results(node_id,config_id,fingerprint,source_checked_at,status,latency_ms,checked_at)
                          VALUES (?,?,?,?,?,?,?)""", results)
    first = guest.get(f"/api/public/configs?node_id={node['id']}").json
    second = guest.get(f"/api/public/configs?node_id={node['id']}&page=2").json
    assert first["total"] == 52 and first["pages"] == 2 and len(first["configs"]) == 50
    assert [row["id"] for row in second["configs"]] == configs[50:52]
    assert guest.get(f"/api/public/ids?node_id={node['id']}").json["ids"] == configs[:52]
    assert [row["id"] for row in guest.get(f"/api/public/configs?node_id={second_iran['id']}").json["configs"]] == [configs[52]]
    iran = guest.get("/api/public/configs?node_region=iran&page=2").json
    assert iran["total"] == 53 and [row["id"] for row in iran["configs"]] == configs[50:53]
    assert guest.get("/api/public/ids?node_region=iran").json["ids"] == configs[:53]
    assert guest.get(f"/api/public/configs?node_id={node['id']}&status=failed").json["total"] == 0
    for invalid in ("0", "-1", "1%20OR%201=1", "9" * 100):
        assert guest.get(f"/api/public/configs?node_id={invalid}").status_code == 400
    assert guest.get(f"/api/public/configs?node_id={node['id']}&node_region=iran").status_code == 400
    assert guest.get("/api/public/configs?node_region=invalid").status_code == 400


def test_node_location_is_editable_by_admin_and_old_nodes_are_not_assumed_iran(app_and_clients):
    app, admin, guest, csrf = app_and_clients
    legacy = admin.post("/api/nodes", json={"name": "Existing node"}, headers=csrf).json["node"]
    assert legacy["location"] == "other"
    assert guest.put(f"/api/nodes/{legacy['id']}", json={"location": "iran"}).status_code in (401, 403)
    assert admin.put(f"/api/nodes/{legacy['id']}", json={"location": "iran"}).status_code == 403
    assert admin.put(f"/api/nodes/{legacy['id']}", json={"location": "elsewhere"}, headers=csrf).status_code == 400
    assert admin.put(f"/api/nodes/{legacy['id']}", json={"location": "iran"}, headers=csrf).status_code == 200
    assert guest.get("/api/public/configs").json["nodes"][0]["location"] == "iran"


def test_existing_node_schema_migrates_without_guessing_location(tmp_path):
    database = tmp_path / "panel.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE nodes(id INTEGER PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, last_seen TEXT)")
        db.execute("INSERT INTO nodes VALUES (1,'Legacy','hash','2026-01-01',NULL)")
    initialize(tmp_path)
    initialize(tmp_path)
    with connect(database) as db:
        assert tuple(db.execute("SELECT name,token_hash,location FROM nodes").fetchone()) == ("Legacy", "hash", "other")


def test_two_primary_results_in_the_same_second_get_distinct_timestamps(monkeypatch):
    from panel import tester
    moments = iter([datetime(2026, 11, 1, 1, 0, 0, 100000, tzinfo=timezone.utc),
                    datetime(2026, 11, 1, 1, 0, 0, 300000, tzinfo=timezone.utc)])
    class Clock:
        @classmethod
        def now(cls, _timezone):
            return next(moments)
    monkeypatch.setattr(tester, "datetime", Clock)
    assert tester.result("healthy")["checked_at"] != tester.result("healthy")["checked_at"]


def test_stale_report_or_unassigned_ids_cannot_modify_results(app_and_clients):
    app, admin, node_client, csrf = app_and_clients
    db_path = app.config["DB_PATH"]
    first = add_config(db_path, 1, "healthy")
    second = add_config(db_path, 2, "failed")
    node, token = create_node(admin, csrf)
    batch = node_client.post("/api/node/pull", json={}, headers=bearer(token)).json
    invalid = {"lease": batch["lease"], "results": [{"id": second, "status": "healthy", "latency_ms": 2}]}
    assert node_client.post("/api/node/report", json=invalid, headers=bearer(token)).status_code in (400, 409)
    with connect(db_path) as db:
        db.execute("UPDATE configs SET status='failed',latency_ms=NULL WHERE id=?", (first,))
    late = {"lease": batch["lease"], "results": [{"id": first, "status": "healthy", "latency_ms": 3}]}
    response = node_client.post("/api/node/report", json=late, headers=bearer(token))
    assert response.status_code == 200 and response.json["accepted"] == 0
    with connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM node_results").fetchone()[0] == 0
    assert node_client.post("/api/node/pull", json={}, headers=bearer(token)).json["configs"] == []


def test_lease_expires_retests_after_primary_update_and_revocation(app_and_clients):
    app, admin, node_client, csrf = app_and_clients
    db_path = app.config["DB_PATH"]
    config = add_config(db_path, 1, "healthy")
    node, token = create_node(admin, csrf)
    batch = node_client.post("/api/node/pull", json={}, headers=bearer(token)).json
    assert node_client.post("/api/node/pull", json={}, headers=bearer(token)).json["configs"] == []
    with connect(db_path) as db:
        db.execute("UPDATE node_assignments SET expires_at=?", (int(time.time()) - 1,))
    batch = node_client.post("/api/node/pull", json={}, headers=bearer(token)).json
    result = {"lease": batch["lease"], "results": [{"id": config, "status": "failed", "latency_ms": None}]}
    assert node_client.post("/api/node/report", json=result, headers=bearer(token)).json["accepted"] == 1
    with connect(db_path) as db:
        db.execute("UPDATE configs SET checked_at='2026-11-01T01:00:00.500000+00:00' WHERE id=?", (config,))
    public = node_client.get("/api/public/configs").json
    assert public["configs"][0]["node_results"] == {}
    assert [row["id"] for row in node_client.post("/api/node/pull", json={}, headers=bearer(token)).json["configs"]] == [config]
    assert admin.delete(f"/api/nodes/{node['id']}", headers=csrf).status_code == 200
    assert node_client.post("/api/node/pull", json={}, headers=bearer(token)).status_code == 401


@pytest.mark.parametrize("status,latency", [([], 2), ("healthy", True), ("healthy", float('nan')),
                                            ("healthy", 10**400), ("failed", 2), ("pending", None)])
def test_malformed_node_results_are_rejected_without_writes(app_and_clients, status, latency):
    app, admin, node_client, csrf = app_and_clients
    config = add_config(app.config["DB_PATH"], 1, "healthy")
    _, token = create_node(admin, csrf)
    batch = node_client.post("/api/node/pull", json={}, headers=bearer(token)).json
    result = {"lease": batch["lease"], "results": [{"id": config, "status": status, "latency_ms": latency}]}
    assert node_client.post("/api/node/report", json=result, headers=bearer(token)).status_code == 400
    with connect(app.config["DB_PATH"]) as db:
        assert db.execute("SELECT COUNT(*) FROM node_results").fetchone()[0] == 0


def test_invalid_second_result_cannot_partially_write_first(app_and_clients):
    app, admin, node_client, csrf = app_and_clients
    good = add_config(app.config["DB_PATH"], 1, "healthy")
    other = add_config(app.config["DB_PATH"], 2, "failed")
    _, token = create_node(admin, csrf)
    batch = node_client.post("/api/node/pull", json={}, headers=bearer(token)).json
    result = {"lease": batch["lease"], "results": [{"id": good, "status": "healthy", "latency_ms": 7},
                                                    {"id": other, "status": "healthy", "latency_ms": 8}]}
    assert node_client.post("/api/node/report", json=result, headers=bearer(token)).status_code == 409
    with connect(app.config["DB_PATH"]) as db:
        assert db.execute("SELECT COUNT(*) FROM node_results").fetchone()[0] == 0


def test_ten_thousand_configs_are_pulled_in_bounded_nonoverlapping_batches(app_and_clients):
    app, admin, node_client, csrf = app_and_clients
    with connect(app.config["DB_PATH"]) as db:
        db.executemany("""INSERT INTO configs(uri,name,protocol,host,port,fingerprint,status,checked_at)
            VALUES (?,?,?,?,?,?,?,?)""", [(f"vless://example-{i}", f"Config {i}", "vless", "example.com", 443,
                                            f"{i:064x}", "healthy" if i < 8500 else "failed",
                                            "2026-11-01T01:00:00+00:00") for i in range(10000)])
    node, token = create_node(admin, csrf)
    first = node_client.post("/api/node/pull", json={}, headers=bearer(token)).json
    second = node_client.post("/api/node/pull", json={}, headers=bearer(token)).json
    a, b = {row["id"] for row in first["configs"]}, {row["id"] for row in second["configs"]}
    assert len(a) == len(b) == 128 and a.isdisjoint(b)
    with connect(app.config["DB_PATH"]) as db:
        assert db.execute("""SELECT COUNT(*) FROM node_assignments a JOIN configs c ON c.id=a.config_id
                             WHERE c.status!='healthy'""").fetchone()[0] == 0
        db.execute("""INSERT INTO node_results(node_id,config_id,fingerprint,source_checked_at,status,latency_ms,checked_at)
            SELECT ?,id,fingerprint,checked_at,'healthy',25,checked_at FROM configs WHERE status='healthy'""", (node["id"],))
    snapshot = node_client.get("/api/public/ids?node_region=iran")
    assert snapshot.status_code == 200 and len(snapshot.json["ids"]) == 8500
    assert len(set(snapshot.json["ids"])) == 8500 and "uri" not in snapshot.json
