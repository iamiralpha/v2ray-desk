import base64
import json
import socket
import threading
import time
from contextlib import nullcontext

import pytest
import requests
from werkzeug.security import generate_password_hash

from panel.app import create_app
from panel.parser import ConfigError, import_lines, parse_uri
from panel.store import connect, initialize, save_password
from panel.tester import TestManager as Manager, make_config, probe_gemini, public_address, result

UUID = "6b8744fb-aeb4-4fc7-938e-adb6b18b835a"
LINK = f"vless://{UUID}@example.com:443?security=tls&type=ws&path=%2Fws#test"
PASSWORD = "Test-only-password123!"


@pytest.fixture
def client(tmp_path):
    db = initialize(tmp_path)
    save_password(db, generate_password_hash(PASSWORD))
    app = create_app(tmp_path, {"TESTING": True, "SESSION_COOKIE_SECURE": False})
    client = app.test_client()
    client.get("/login")
    with client.session_transaction() as session:
        token = session["csrf"]
    response = client.post("/login", data={"password": PASSWORD, "csrf_token": token})
    assert response.status_code == 302
    return client


def send(client, url, data, method="post"):
    with client.session_transaction() as session:
        token = session["csrf"]
    return getattr(client, method)(url, json=data, headers={"X-CSRF-Token": token})


def add(client, text=LINK):
    response = send(client, "/api/import", {"text": text})
    assert response.status_code == 200
    return response.json


def test_import_duplicate_ignores_renaming(client):
    data = add(client, LINK + "\n" + LINK.replace("#test", "#other") + "\nbroken")
    assert (data["added"], data["duplicates"], len(data["errors"])) == (1, 1, 1)
    assert "uri" not in client.get("/api/configs").json["configs"][0]


def test_base64_and_vmess():
    vmess = {"v":"2", "ps":"node", "add":"example.com", "port":"443", "id":UUID,
             "aid":"0", "net":"ws", "type":"none", "host":"cdn.example.com", "path":"/a", "tls":"tls"}
    raw = "vmess://" + base64.b64encode(json.dumps(vmess).encode()).decode()
    parsed = parse_uri(raw)
    assert parsed["outbound"]["streamSettings"]["wsSettings"]["headers"]["Host"] == "cdn.example.com"
    subscription = base64.b64encode((raw + "\n" + LINK).encode()).decode()
    assert len(import_lines(subscription)) == 2


@pytest.mark.parametrize("link", [
    LINK.replace("security=tls", "security=bad"),
    LINK + "&unused=1",
    LINK.replace("&path=%2Fws", "&allowInsecure=1"),
    LINK.replace("type=ws", "type=kcp"),
    LINK.replace(":443?", ":70000?"),
])
def test_reject_bad_inputs(link):
    # Unknown query parameters must occur before the fragment.
    if "&unused" in link:
        link = link.replace("#test", "")
    with pytest.raises(ConfigError):
        parse_uri(link)


def test_shadowsocks_and_reality():
    auth = base64.urlsafe_b64encode(b"aes-256-gcm:password").decode().rstrip("=")
    ss = parse_uri(f"ss://{auth}@example.com:8388#name")
    assert ss["outbound"]["settings"]["servers"][0]["password"] == "password"
    key = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
    reality = parse_uri(f"vless://{UUID}@example.com:443?security=reality&pbk={key}&sid=ab&type=tcp&flow=xtls-rprx-vision")
    assert reality["outbound"]["streamSettings"]["realitySettings"]["publicKey"] == key


@pytest.mark.parametrize("query,reason", [
    ("security=reality&pbk=bad", "32-byte"),
    ("security=reality&pbk=" + "A" * 43 + "&sid=abc", "even-length"),
    ("security=tls&type=ws&flow=xtls-rprx-vision", "Vision"),
    ("security=none&type=tcp&flow=xtls-rprx-vision", "Vision"),
])
def test_reject_invalid_security_settings(query, reason):
    with pytest.raises(ConfigError, match=reason):
        parse_uri(f"vless://{UUID}@example.com:443?{query}")


def test_reject_invalid_ss2022_key():
    auth = base64.b64encode(b"2022-blake3-aes-128-gcm:not-a-valid-key").decode()
    with pytest.raises(ConfigError, match="Base64 keys"):
        parse_uri(f"ss://{auth}@example.com:443")


def test_csv_report_order_no_credentials_and_formula_protection(client):
    import csv
    import io
    add(client, LINK + "\n" + LINK.replace("example.com", "example.net"))
    with connect(client.application.config["DB_PATH"]) as db:
        db.execute("UPDATE configs SET name=?,country='DE',gemini_code=403 WHERE id=1",
                   (' \t=HYPERLINK("https://example.com","click")',))
    report = send(client, "/api/report", {"ids": [1, 2]})
    assert report.status_code == 200 and report.mimetype == "text/csv"
    data = list(csv.DictReader(io.StringIO(report.data.decode("utf-8-sig"))))
    assert [r["id"] for r in data] == ["1", "2"]
    assert data[0]["name"].startswith("'") and data[0]["gemini_code"] == "403"
    assert data[0]["country"] == "DE"
    assert UUID not in report.text and "vless://" not in report.text
    assert send(client, "/api/report", {"ids": []}).status_code == 400
    assert client.post("/api/report", json={"ids": [1]}).status_code == 403


def test_auth_and_csrf(client):
    assert client.post("/api/delete", json={"ids": [1]}).status_code == 403
    visitor = client.application.test_client()
    assert visitor.get("/api/configs").status_code == 401
    assert visitor.get("/").status_code == 302
    assert client.get("/").headers["X-Frame-Options"] == "DENY"


def test_edit_resets_measurements_and_export_order(client):
    add(client, LINK + "\n" + LINK.replace("example.com", "example.net"))
    rows = client.get("/api/configs").json["configs"]
    first, second = rows[0]["id"], rows[1]["id"]
    with connect(client.application.config["DB_PATH"]) as db:
        db.execute("UPDATE configs SET status='healthy',latency_ms=10,country='DE',gemini_status='blocked',gemini_code=403")
    response = send(client, f"/api/configs/{first}", {"name": "changed", "uri": LINK.replace("example.com", "example.org")}, "put")
    assert response.status_code == 200
    row = client.get("/api/configs").json["configs"][0]
    assert row["status"] == "pending" and row["latency_ms"] is None
    assert row["country"] is None and row["gemini_status"] == "untested"
    exported = send(client, "/api/export", {"ids": [second, first]}).text.splitlines()
    assert exported[0] == LINK
    assert "example.org" in exported[1]


def test_failed_delete_never_deletes_untested_or_errors(client):
    add(client, "\n".join(LINK.replace("example.com", f"node{i}.example.com") for i in range(4)))
    rows = client.get("/api/configs").json["configs"]
    ids = [row["id"] for row in rows]
    with connect(client.application.config["DB_PATH"]) as db:
        db.execute("UPDATE configs SET status='failed' WHERE id=?", (ids[0],))
        db.execute("UPDATE configs SET status='error' WHERE id=?", (ids[1],))
    assert send(client, "/api/delete", {"ids": ids, "failed_only": True}).json["deleted"] == 1
    assert len(client.get("/api/configs").json["configs"]) == 3


def test_password_change_revokes_other_sessions(client):
    other = client.application.test_client()
    other.get("/login")
    with other.session_transaction() as session:
        token = session["csrf"]
    other.post("/login", data={"csrf_token": token, "password": PASSWORD})
    response = send(client, "/api/password", {"current": PASSWORD, "password": PASSWORD + "new"})
    assert response.status_code == 200
    assert other.get("/api/configs").status_code == 401


def test_login_rate_limit(client):
    visitor = client.application.test_client()
    visitor.get("/login")
    with visitor.session_transaction() as session:
        token = session["csrf"]
    for _ in range(5):
        assert visitor.post("/login", data={"csrf_token": token, "password": "wrong"}).status_code == 401
    assert visitor.post("/login", data={"csrf_token": token, "password": PASSWORD}).status_code == 429


def test_mutation_blocked_during_test(client):
    manager = client.application.extensions["tester"]
    manager.state["running"] = True
    try:
        assert send(client, "/api/import", {"text": LINK}).status_code == 409
        assert send(client, "/api/delete", {"ids": [1]}).status_code == 409
    finally:
        manager.state["running"] = False


def test_restart_recovers_testing_status(tmp_path):
    db_path = initialize(tmp_path)
    with connect(db_path) as db:
        p = parse_uri(LINK)
        db.execute("INSERT INTO configs(uri,name,protocol,host,port,fingerprint,status) VALUES (?,?,?,?,?,?,'testing')",
                   [p[k] for k in ("uri","name","protocol","host","port","fingerprint")])
    Manager(db_path)
    with connect(db_path) as db:
        assert db.execute("SELECT status FROM configs").fetchone()[0] == "error"


def test_private_address_and_rebinding_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2,1,6,"",("127.0.0.1",443))])
    with pytest.raises(ConfigError):
        public_address("example.com", 443)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2,1,6,"",("1.1.1.1",443)), (2,1,6,"",("10.0.0.1",443))])
    with pytest.raises(ConfigError):
        public_address("example.com", 443)


def test_only_proxy_outbound_and_host_preserved():
    config = make_config(parse_uri(LINK), "1.1.1.1", 12345)
    assert len(config["outbounds"]) == 1
    assert config["outbounds"][0]["protocol"] == "vless"
    assert config["outbounds"][0]["settings"]["vnext"][0]["address"] == "1.1.1.1"
    assert config["outbounds"][0]["streamSettings"]["wsSettings"]["headers"]["Host"] == "example.com"
    assert config["inbounds"][0]["listen"] == "127.0.0.1"


class FakeResponse:
    def __init__(self, code, location=None):
        self.status_code = code
        self.headers = {"Location": location} if location else {}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


@pytest.mark.parametrize("code,location,expected", [
    (403, None, "blocked"), (429, None, "limited"), (200, None, "http200"),
    (302, "https://accounts.google.com/login", "login"),
    (302, "http://example.com", "redirect"), (500, None, "other"),
])
def test_gemini_classification_and_no_direct_fallback(monkeypatch, code, location, expected):
    class Client:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, **kwargs):
            assert self.trust_env is False
            assert self.proxies["https"] == "socks5h://127.0.0.1:12345"
            assert kwargs["allow_redirects"] is False
            return FakeResponse(code, location)
    monkeypatch.setattr(requests, "Session", Client)
    assert probe_gemini(12345, 2) == (expected, code)


def test_background_job_persists_result(client, monkeypatch):
    add(client)
    manager = client.application.extensions["tester"]
    monkeypatch.setattr("panel.tester.shutil.which", lambda value: "/xray")
    manager.probe_function = lambda *a, **k: result("healthy", latency_ms=123, country="DE", successes=3,
                                             samples=3, gemini_status="blocked", gemini_code=403)
    row_id = client.get("/api/configs").json["configs"][0]["id"]
    assert send(client, "/api/test", {"ids": [row_id]}).status_code == 202
    deadline = time.monotonic() + 3
    while manager.snapshot()["running"] and time.monotonic() < deadline:
        time.sleep(.02)
    assert not manager.snapshot()["running"]
    row = client.get("/api/configs").json["configs"][0]
    assert row["latency_ms"] == 123 and row["gemini_code"] == 403


def test_english_ui_errors_and_user_data_are_preserved(client):
    page = client.get("/?lang=en").text
    assert '<html lang="en" dir="ltr">' in page
    assert "All countries" in page and "Gemini / HTTP" in page
    response = send(client, "/api/import", {"text":"broken://value"})
    assert response.json["errors"][0]["error"].startswith("Only vmess")
    add(client, LINK.replace("#test", "#نامشخص"))
    row = client.get("/api/configs").json["configs"][0]
    assert row["name"] == "نامشخص"


def test_prefixed_urls_and_login_form(client):
    prefix = "/AbCdEf1234567890abcd"
    page = client.get("/", headers={"X-Forwarded-Prefix":prefix}).text
    assert f'content="{prefix}"' in page
    assert f'href="{prefix}/static/style.css"' in page
    assert f'href="{prefix}/?lang=en"' in page
    visitor = client.application.test_client()
    login = visitor.get("/login", headers={"X-Forwarded-Prefix":prefix}).text
    assert f'action="{prefix}/login"' in login
