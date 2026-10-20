"""Preset installation, bounded initial sync and subscription membership."""
import base64
import threading
import time

import pytest
from werkzeug.security import generate_password_hash

from panel.app import create_app
from panel.store import connect, initialize, save_password, setting
from test_panel import client, send, add, LINK


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "Background subscription sync did not finish"
        time.sleep(.01)


def make_app(folder, **config):
    path = initialize(folder)
    if not setting(path, "password_hash"):
        save_password(path, generate_password_hash("test-password-only", method="pbkdf2:sha256:1000"))
    return create_app(folder, {"TESTING": True, "SESSION_COOKIE_SECURE": False, **config})


def authenticated(app):
    result = app.test_client()
    with result.session_transaction() as session:
        session.update(authenticated=True, auth_version=setting(app.config["DB_PATH"], "auth_version"), csrf="test-token")
    return result


@pytest.fixture
def catalog(monkeypatch):
    items = [
        {"key": "barry-1", "provider": "barry", "name": "Barry 1", "url": "https://raw.githubusercontent.com/Barry/Repo/main/Sub1.txt", "enabled": True},
        {"key": "barry-2", "provider": "barry", "name": "Barry 2", "url": "https://raw.githubusercontent.com/Barry/Repo/main/Sub2.txt", "enabled": True},
        {"key": "delta-1", "provider": "delta", "name": "Delta 1", "url": "https://raw.githubusercontent.com/Delta/Repo/main/Sub1.txt", "enabled": False},
    ]
    monkeypatch.setattr("panel.app.PRESETS", items)
    return items


def test_testing_does_not_seed_or_start_downloads_by_default(client):
    assert client.get("/api/subscriptions").json["subscriptions"] == []
    data = client.get("/api/subscriptions/presets").json
    assert len(data["presets"]) == 27
    assert sum(provider["default_enabled"] for provider in data["providers"]) == 6
    assert not data["subscription_sync"]["running"]


def test_thread_start_failure_releases_sources_and_reports_retry(client, monkeypatch, catalog):
    def fail(self):
        raise RuntimeError("cannot start thread")

    monkeypatch.setattr("panel.app.threading.Thread.start", fail)
    response = send(client, "/api/subscriptions/presets", {"provider": "barry"})
    assert response.status_code == 202 and response.json["queued"] == 0
    listing = client.get("/api/subscriptions").json
    assert not listing["subscription_sync"]["running"]
    assert all(item["error"] for item in listing["subscriptions"])


def test_seed_once_preserves_existing_source_and_deleted_sources_stay_deleted(tmp_path, monkeypatch, catalog):
    path = initialize(tmp_path)
    with connect(path) as db:
        db.execute("INSERT INTO subscriptions(name,url,enabled) VALUES (?,?,0)",
                   ("My custom name", "https://github.com/barry/repo/raw/refs/heads/main/Sub1.txt"))
    fetched = []
    monkeypatch.setattr("panel.app.fetch_subscription", lambda url: fetched.append(url) or LINK)
    app = make_app(tmp_path, PRESET_AUTO_SEED=True)
    user = authenticated(app)
    wait_for(lambda: not user.get("/api/configs").json["subscription_sync"]["running"])
    items = user.get("/api/subscriptions").json["subscriptions"]
    assert len(items) == 3 and fetched == [catalog[1]["url"]]
    existing = next(item for item in items if item["name"] == "My custom name")
    assert existing["enabled"] == 0
    added = next(item for item in items if item["name"] == "Barry 2")
    assert send(user, f"/api/subscriptions/{added['id']}", {}, "delete").status_code == 200
    restarted = authenticated(make_app(tmp_path, PRESET_AUTO_SEED=True))
    assert len(restarted.get("/api/subscriptions").json["subscriptions"]) == 2
    assert fetched == [catalog[1]["url"]]


def test_initial_sync_reserves_all_pending_sources_and_uses_one_worker(tmp_path, monkeypatch, catalog):
    entered, release = threading.Event(), threading.Event()
    guard = threading.Lock()
    counts = {"active": 0, "peak": 0, "done": 0}

    def fetch(url):
        with guard:
            counts["active"] += 1
            counts["peak"] = max(counts["peak"], counts["active"])
        entered.set()
        try:
            assert release.wait(5)
            return LINK
        finally:
            with guard:
                counts["active"] -= 1
                counts["done"] += 1

    monkeypatch.setattr("panel.app.fetch_subscription", fetch)
    user = authenticated(make_app(tmp_path, PRESET_AUTO_SEED=True))
    try:
        assert entered.wait(1)
        progress = user.get("/api/configs").json["subscription_sync"]
        assert progress == {"running": True, "total": 2, "done": 0}
        items = user.get("/api/subscriptions").json["subscriptions"]
        assert sum(bool(item["syncing"]) for item in items) == 2
        assert send(user, "/api/test", {"ids": [1]}).status_code == 409
        assert send(user, "/api/subscriptions/presets", {"provider": "barry"}).status_code == 409
    finally:
        release.set()
    wait_for(lambda: not user.get("/api/configs").json["subscription_sync"]["running"])
    assert counts == {"active": 0, "peak": 1, "done": 2}
    assert user.get("/api/subscriptions").json["subscription_sync"] == {"running": False, "total": 2, "done": 2}


def test_background_failure_is_sanitized_and_later_sources_still_sync(tmp_path, monkeypatch, catalog):
    def fetch(url):
        if url == catalog[0]["url"]:
            raise RuntimeError("https://private.example/token-secret")
        return LINK

    monkeypatch.setattr("panel.app.fetch_subscription", fetch)
    user = authenticated(make_app(tmp_path, PRESET_AUTO_SEED=True))
    wait_for(lambda: not user.get("/api/configs").json["subscription_sync"]["running"])
    data = user.get("/api/subscriptions").json
    assert data["subscription_sync"]["done"] == 2
    assert sum(item["config_count"] for item in data["subscriptions"]) == 1
    assert any(item["error"] for item in data["subscriptions"])
    assert "token-secret" not in str(data) and "private.example" not in str(data)


def test_preset_restore_is_authenticated_idempotent_and_hides_urls(client, monkeypatch, catalog):
    monkeypatch.setattr("panel.app.fetch_subscription", lambda url: LINK)
    visitor = client.application.test_client()
    assert visitor.get("/api/subscriptions/presets").status_code == 401
    assert client.post("/api/subscriptions/presets", json={}).status_code == 403
    assert send(client, "/api/subscriptions/presets", {"provider": "missing"}).status_code == 400
    assert send(client, "/api/subscriptions/presets", {"provider": []}).status_code == 400
    added = send(client, "/api/subscriptions/presets", {"provider": "barry"})
    assert added.status_code == 202 and added.json["added"] == added.json["queued"] == 2
    wait_for(lambda: not client.get("/api/configs").json["subscription_sync"]["running"])
    again = send(client, "/api/subscriptions/presets", {"provider": "barry"})
    assert again.json["added"] == again.json["queued"] == 0
    data = client.get("/api/subscriptions/presets").json
    assert "https://" not in str(data) and "url" not in str(data)
    assert sum(item["installed"] for item in data["presets"]) == 2
    installed = [item for item in data["presets"] if item["installed"]]
    keep_id, remove_id = [item["subscription_id"] for item in installed]
    assert send(client, f"/api/subscriptions/{keep_id}",
                {"name": "Keep my settings", "url": catalog[0]["url"], "enabled": False}, "put").status_code == 200
    assert send(client, f"/api/subscriptions/{remove_id}", {}, "delete").status_code == 200
    restored = send(client, "/api/subscriptions/presets", {"provider": "barry"})
    assert restored.json["added"] == restored.json["queued"] == 1
    wait_for(lambda: not client.get("/api/configs").json["subscription_sync"]["running"])
    kept = client.get(f"/api/subscriptions/{keep_id}").json
    assert kept["name"] == "Keep my settings" and kept["enabled"] == 0


def test_preset_restore_enforces_source_quota_atomically(client, catalog):
    with connect(client.application.config["DB_PATH"]) as db:
        db.executemany("INSERT INTO subscriptions(name,url) VALUES (?,?)",
                       [(str(index), f"https://example.com/{index}") for index in range(99)])
    response = send(client, "/api/subscriptions/presets", {"provider": "barry"})
    assert response.status_code == 400
    assert len(client.get("/api/subscriptions").json["subscriptions"]) == 99


def test_membership_and_renaming_invalidate_config_snapshot(client, monkeypatch):
    ids = [send(client, "/api/subscriptions", {"name": name, "url": f"https://example.com/{name}"}).json["id"]
           for name in ("one", "two")]
    monkeypatch.setattr("panel.app.fetch_subscription", lambda url: LINK)
    for sub_id in ids:
        assert send(client, f"/api/subscriptions/{sub_id}/refresh", {}).status_code == 200
    initial = client.get("/api/configs").json
    assert initial["configs"][0]["subscription_ids"] == ids
    assert all(set(item) == {"id", "name", "config_count"} for item in initial["subscriptions"])
    assert send(client, f"/api/subscriptions/{ids[0]}", {"name": "renamed", "url": "https://example.com/one"}, "put").status_code == 200
    updated = client.get("/api/configs?cursor=" + initial["cursor"]).json
    assert updated["full"] and any(item["name"] == "renamed" for item in updated["subscriptions"])
    assert send(client, f"/api/subscriptions/{ids[0]}", {}, "delete").status_code == 200
    assert client.get("/api/configs").json["configs"][0]["subscription_ids"] == [ids[1]]


def test_daily_reservation_blocks_manual_refresh_and_source_changes(client, monkeypatch):
    data = {"name": "disabled", "url": "https://example.com/private-token", "enabled": False}
    sub_id = send(client, "/api/subscriptions", data).json["id"]
    visitor = client.application.test_client()
    assert visitor.get(f"/api/subscriptions/{sub_id}").status_code == 401
    fetched = []
    monkeypatch.setattr("panel.app.fetch_subscription", lambda url: fetched.append(url) or LINK)
    reservation = client.application.extensions["automation"].syncing
    reservation.add(-1)
    try:
        assert send(client, f"/api/subscriptions/{sub_id}/refresh", {}).status_code == 409
        assert send(client, f"/api/subscriptions/{sub_id}", data, "put").status_code == 409
        assert send(client, "/api/subscriptions", data).status_code == 409
        assert send(client, f"/api/subscriptions/{sub_id}", {}, "delete").status_code == 409
        assert not fetched
    finally:
        reservation.discard(-1)


def test_hundred_name_aliases_across_two_feeds_and_manual_import_share_one_config(client, monkeypatch):
    add(client, LINK.replace("#test", "#manual"))
    aliases = "\n".join(LINK.replace("#test", f"#alias-{index}") for index in range(100))
    monkeypatch.setattr("panel.app.fetch_subscription", lambda url: aliases)
    ids = []
    for name in ("first", "second"):
        sub_id = send(client, "/api/subscriptions", {"name": name, "url": f"https://example.com/{name}"}).json["id"]
        ids.append(sub_id)
        result = send(client, f"/api/subscriptions/{sub_id}/refresh", {})
        assert result.status_code == 200 and result.json["total"] == 1 and result.json["added"] == 0
    rows = client.get("/api/configs").json["configs"]
    assert len(rows) == 1 and rows[0]["name"] == "manual" and rows[0]["subscription_ids"] == ids
    with connect(client.application.config["DB_PATH"]) as db:
        assert db.execute("SELECT manual FROM configs").fetchone()[0] == 1


def test_export_base64_preserves_utf8_and_plain_default(client):
    add(client, LINK.replace("#test", "#نام"))
    ids = [client.get("/api/configs").json["configs"][0]["id"]]
    plain = send(client, "/api/export", {"ids": ids})
    encoded = send(client, "/api/export", {"ids": ids, "format": "base64"})
    assert base64.b64decode(encoded.data) == plain.data
    assert "v2ray-configs-base64.txt" in encoded.headers["Content-Disposition"]
    assert send(client, "/api/export", {"ids": ids, "format": "unknown"}).status_code == 400


def test_all_tombstoned_feed_is_valid_and_manual_import_restores_it(client, monkeypatch):
    from panel.parser import parse_uri
    fingerprint = parse_uri(LINK)["fingerprint"]
    with connect(client.application.config["DB_PATH"]) as db:
        db.execute("INSERT INTO rejected_configs(fingerprint) VALUES (?)", (fingerprint,))
    sub_id = send(client, "/api/subscriptions", {"name": "blocked", "url": "https://example.com/blocked"}).json["id"]
    monkeypatch.setattr("panel.app.fetch_subscription", lambda url: LINK)
    response = send(client, f"/api/subscriptions/{sub_id}/refresh", {})
    assert response.status_code == 200 and response.json == {"added": 0, "removed": 0, "total": 0, "invalid": 0}
    assert client.get("/api/configs").json["configs"] == []
    add(client)
    with connect(client.application.config["DB_PATH"]) as db:
        assert db.execute("SELECT COUNT(*) FROM rejected_configs").fetchone()[0] == 0
    assert send(client, f"/api/subscriptions/{sub_id}/refresh", {}).json["total"] == 1


def test_manual_edit_resets_failure_history_and_tombstone(client):
    from panel.parser import parse_uri
    add(client)
    replacement = LINK.replace("example.com", "new.example.com")
    fingerprint = parse_uri(replacement)["fingerprint"]
    with connect(client.application.config["DB_PATH"]) as db:
        db.execute("UPDATE configs SET real_failure_streak=1")
        db.execute("INSERT INTO rejected_configs(fingerprint) VALUES (?)", (fingerprint,))
    config_id = client.get("/api/configs").json["configs"][0]["id"]
    assert send(client, f"/api/configs/{config_id}", {"name": "changed", "uri": replacement}, "put").status_code == 200
    with connect(client.application.config["DB_PATH"]) as db:
        assert db.execute("SELECT real_failure_streak FROM configs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM rejected_configs").fetchone()[0] == 0


@pytest.mark.parametrize("url", [
    "https://raw.githubusercontent.com/BARRY/REPO/refs/heads/main/Sub1.txt",
    "https://github.com/barry/repo/raw/main/Sub1.txt",
    "https://github.com/barry/repo/raw/refs/heads/main/Sub1.txt",
    "https://raw.github.com/barry/repo/main/Sub1.txt",
])
def test_preset_url_aliases_preserve_file_path_case(url):
    from panel.presets import url_identity
    expected = "https://raw.githubusercontent.com/barry/repo/main/Sub1.txt"
    assert url_identity(url) == url_identity(expected)
    assert url_identity(url.replace("Sub1", "sub1")) != url_identity(expected)
