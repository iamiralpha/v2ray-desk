from panel.store import connect
import pytest
from test_panel import LINK, add, client, send


def test_public_home_and_copy_are_available_but_admin_is_private(client):
    add(client)
    visitor = client.application.test_client()
    home = visitor.get("/?lang=en")
    assert home.status_code == 200 and "Admin sign in" in home.text
    listing = visitor.get("/api/public/configs")
    assert listing.status_code == 200
    row = listing.json["configs"][0]
    assert "uri" not in row and "host" not in row and "error" not in row
    assert visitor.get(f"/api/public/configs/{row['id']}").json["uri"] == LINK
    for route in ("/api/configs", "/api/subscriptions", "/api/automation", "/api/subscriptions/presets"):
        assert visitor.get(route).status_code == 401
    visitor.get("/login")
    for route, data in [("/api/test", {"ids": [row["id"]]}), ("/api/import", {"text": LINK}),
                        ("/api/delete", {"ids": [row["id"]]})]:
        assert send(visitor, route, data).status_code == 401
    assert "test-btn" in client.get("/").text


def test_public_filters_pagination_and_healthy_delay_sort(client):
    add(client, "\n".join(LINK.replace("example.com", f"node{i}.example.com") for i in range(53)))
    with connect(client.application.config["DB_PATH"]) as db:
        db.execute("UPDATE configs SET country='DE',status='healthy',latency_ms=id+10,gemini_status='http200'")
        db.execute("UPDATE configs SET country='US',gemini_status='blocked' WHERE id=1")
        db.execute("UPDATE configs SET status='failed',latency_ms=NULL WHERE id=2")
    visitor = client.application.test_client()
    result = visitor.get("/api/public/configs").json
    assert len(result["configs"]) == 50 and result["total"] == 53
    assert result["configs"][0]["id"] == 1
    result = visitor.get("/api/public/configs?country=DE&gemini=http200&status=healthy&protocol=vless").json
    assert result["total"] == 51 and result["configs"][0]["id"] == 3
    result = visitor.get("/api/public/configs?page=2").json
    assert len(result["configs"]) == 3 and result["configs"][-1]["status"] == "failed"
    for query in ("page=-1", "page=abc", "status=bogus", "sort=id;DROP TABLE configs", "country='OR1=1"):
        assert visitor.get("/api/public/configs?" + query).status_code == 400


def test_public_copy_not_found_and_prefixed_paths(client):
    visitor = client.application.test_client()
    assert visitor.get("/api/public/configs/999").status_code == 404
    home = visitor.get("/?lang=fa", headers={"X-Forwarded-Prefix": "/custom"})
    assert 'content="/custom"' in home.text
    assert '/custom/login' in home.text and '/custom/static/public.js' in home.text
    assert 'dir="rtl"' in home.text


def test_public_bulk_copy_preserves_selection_order_and_skips_missing(client):
    add(client, "\n".join(LINK.replace("example.com", f"node{i}.example.com") for i in range(3)))
    visitor = client.application.test_client()
    result = visitor.get("/api/public/copy?ids=3,1,3,999").json
    assert [row["id"] for row in result["configs"]] == [3, 1]
    assert result["configs"][0]["uri"] == LINK.replace("example.com", "node2.example.com")
    assert all(set(row) == {"id", "uri"} for row in result["configs"])
    assert visitor.get("/api/public/copy?ids=999").json == {"configs": []}
    assert visitor.get("/api/configs").status_code == 401


@pytest.mark.parametrize("ids", ["", "0", "-1", "1.5", "1,,2", "1;DROP TABLE configs", "9" * 19,
                                    ",".join(str(i) for i in range(1, 502))])
def test_public_bulk_copy_rejects_bad_or_oversized_selection(client, ids):
    visitor = client.application.test_client()
    assert visitor.get("/api/public/copy", query_string={"ids": ids}).status_code == 400
    assert visitor.get("/api/public/configs").status_code == 200


def test_public_bulk_copy_accepts_bounded_selection(client):
    visitor = client.application.test_client()
    ids = ",".join(str(i) for i in range(1, 501))
    assert visitor.get("/api/public/copy", query_string={"ids": ids}).status_code == 200
