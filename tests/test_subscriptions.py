import base64

import pytest

from test_panel import client, send, add, LINK
from panel.parser import ConfigError
from panel.store import connect
from panel.subscriptions import fetch_subscription, validate_url

URL = "https://example.com/sub?token=secret"


def create(client, name="My feed"):
    response = send(client, "/api/subscriptions", {"name": name, "url": URL})
    assert response.status_code == 200
    return response.json["id"]


def sync(client, monkeypatch, sub_id, text):
    monkeypatch.setattr("panel.app.fetch_subscription", lambda url: text)
    return send(client, f"/api/subscriptions/{sub_id}/refresh", {})


def test_subscription_reconcile_deduplicates_and_preserves_manual(client, monkeypatch):
    add(client)
    sub_id = create(client)
    other = LINK.replace("example.com", "other.example.com")
    response = sync(client, monkeypatch, sub_id, base64.b64encode((LINK+"\n"+other+"\n"+other).encode()).decode())
    assert response.json == dict(added=1, removed=0, total=2, invalid=0)
    assert sync(client, monkeypatch, sub_id, LINK).json["removed"] == 1
    assert len(client.get("/api/configs").json["configs"]) == 1
    listing = client.get("/api/subscriptions").json["subscriptions"][0]
    assert listing["config_count"] == 1 and "url" not in listing and "secret" not in str(listing)
    assert client.get(f"/api/subscriptions/{sub_id}").json["url"] == URL


def test_subscription_failure_keeps_previous_records(client, monkeypatch):
    sub_id = create(client)
    sync(client, monkeypatch, sub_id, LINK)
    assert sync(client, monkeypatch, sub_id, "invalid://broken").status_code == 400
    assert len(client.get("/api/configs").json["configs"]) == 1
    assert client.get("/api/subscriptions").json["subscriptions"][0]["error"]


def test_shared_configs_and_delete_options(client, monkeypatch):
    a, b = create(client, "a"), create(client, "b")
    sync(client, monkeypatch, a, LINK)
    sync(client, monkeypatch, b, LINK)
    assert send(client, f"/api/subscriptions/{a}", {"remove_configs": True}, "delete").json["removed"] == 0
    assert send(client, f"/api/subscriptions/{b}", {"remove_configs": True}, "delete").json["removed"] == 1
    c = create(client)
    sync(client, monkeypatch, c, LINK)
    send(client, f"/api/subscriptions/{c}", {}, "delete")
    assert len(client.get("/api/configs").json["configs"]) == 1


def test_edited_subscription_config_survives_sync(client, monkeypatch):
    sub_id = create(client)
    sync(client, monkeypatch, sub_id, LINK)
    config_id = client.get("/api/configs").json["configs"][0]["id"]
    send(client, f"/api/configs/{config_id}", {"name": "Mine", "uri": LINK}, "put")
    sync(client, monkeypatch, sub_id, LINK.replace("example.com", "new.example.com"))
    assert len(client.get("/api/configs").json["configs"]) == 2
    send(client, "/api/delete", {"ids": [config_id]})
    with connect(client.application.config["DB_PATH"]) as db:
        assert not db.execute("SELECT 1 FROM subscription_configs WHERE config_id=?", (config_id,)).fetchone()


def test_subscription_auth_csrf_and_busy(client, monkeypatch):
    visitor = client.application.test_client()
    assert visitor.get("/api/subscriptions").status_code == 401
    assert client.post("/api/subscriptions", json={}).status_code == 403
    sub_id = create(client)
    manager = client.application.extensions["tester"]
    manager.state["running"] = True
    assert sync(client, monkeypatch, sub_id, LINK).status_code == 409
    manager.state["running"] = False


@pytest.mark.parametrize("url", ["file:///etc/passwd","https://u:p@example.com/x","https://example.com:99999","https://example.com/\r\nbad"])
def test_subscription_url_validation(url):
    with pytest.raises(ConfigError):
        validate_url(url)


def test_subscription_private_address_blocked():
    with pytest.raises(ConfigError):
        fetch_subscription("http://127.0.0.1/x")


def test_redirect_revalidates_private_destination(monkeypatch):
    class Response:
        status = 302
        headers = {"Location": "http://127.0.0.1/private"}
        def close(self): pass
    class Pool:
        def __init__(self, host, **kwargs): assert host == "1.1.1.1"
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def request(self, *args, **kwargs):
            assert kwargs["redirect"] is False
            return Response()
    from panel.tester import public_address
    monkeypatch.setattr("panel.subscriptions.public_address",
                        lambda host, port: "1.1.1.1" if host == "example.com" else public_address(host, port))
    monkeypatch.setattr("panel.subscriptions.urllib3.HTTPConnectionPool", Pool)
    with pytest.raises(ConfigError):
        fetch_subscription("http://example.com/feed")


def test_size_limit_and_tls_host_pinning(monkeypatch):
    class Response:
        status = 200
        headers = {}
        def close(self): pass
        def read(self, *a, **k): return b"x" * 65536
    class Pool:
        def __init__(self, host, **kwargs):
            assert host == "1.1.1.1"
            assert kwargs["server_hostname"] == "example.com"
            assert kwargs["assert_hostname"] == "example.com"
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def request(self, *a, **k): return Response()
    monkeypatch.setattr("panel.subscriptions.public_address", lambda *a: "1.1.1.1")
    monkeypatch.setattr("panel.subscriptions.urllib3.HTTPSConnectionPool", Pool)
    with pytest.raises(ConfigError, match="4 MB"):
        fetch_subscription(URL)
