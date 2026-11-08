import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import threading

import pytest
import requests

from panel import node_agent
from panel.node_agent import NodeClient, controller_url, fetch_pinned_certificate, run_batch
from panel.store import connect
from test_xray import real_proxy
from test_nodes import app_and_clients, create_node, bearer
from werkzeug.serving import make_server

XRAY = os.environ.get("XRAY_TEST_BIN")
OPENSSL = shutil.which("openssl") or (r"C:\Program Files\Git\usr\bin\openssl.exe" if Path(r"C:\Program Files\Git\usr\bin\openssl.exe").exists() else None)


@pytest.mark.parametrize("url", ["http://example.com", "https://user:pass@example.com", "https://example.com/?token=x",
                                 "https://example.com/#part", "https://example.com:abc", "https://example.com/a\nb"])
def test_node_rejects_unsafe_controller_addresses(url):
    with pytest.raises(ValueError):
        controller_url(url)


@pytest.mark.parametrize("rows", [None, [{"id": 1, "uri": "x"}] * 2,
                                   [{"id": True, "uri": "x"}], [{"id": 1, "uri": "x", "extra": 0}]])
def test_node_rejects_malformed_batches(rows):
    with pytest.raises(ValueError):
        node_agent.validate_batch(rows)


@pytest.mark.skipif(not XRAY, reason="Set XRAY_TEST_BIN to run real engine tests")
def test_node_runs_real_xray_and_returns_separate_result(real_proxy):
    result = run_batch([{"id": 899, "uri": real_proxy}], XRAY, workers=1)
    assert result[0]["id"] == 899 and result[0]["status"] == "healthy"
    assert result[0]["latency_ms"] >= 70


def test_invalid_link_is_reported_without_running_xray():
    assert run_batch([{"id": 42, "uri": "not-a-link"}], "no-xray") == [
        {"id": 42, "status": "invalid", "latency_ms": None}]


@pytest.mark.skipif(not XRAY or not OPENSSL, reason="Requires Xray and OpenSSL executables")
def test_authenticated_worker_roundtrip_tests_only_healthy_source(app_and_clients, real_proxy, tmp_path):
    app, admin, remote, csrf = app_and_clients
    from panel.parser import parse_uri
    source = parse_uri(real_proxy)
    with connect(app.config["DB_PATH"]) as db:
        cursor = db.execute("""INSERT INTO configs(uri,name,protocol,host,port,fingerprint,status,checked_at,latency_ms)
            VALUES(?,?,?,?,?,?,'healthy','2026-11-01T00:00:00+00:00',123)""",
            [source[key] for key in ("uri", "name", "protocol", "host", "port", "fingerprint")])
        config_id = cursor.lastrowid
    node, token = create_node(admin, csrf)
    cert, key = tmp_path / "server-cert.pem", tmp_path / "server-key.pem"
    subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True, timeout=20)
    server = make_server("127.0.0.1", 0, app, ssl_context=(str(cert), str(key)), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = NodeClient(f"https://127.0.0.1:{server.server_port}", token, cert)
        assert client.once(XRAY, 1, threading.Event())
        assert not client.once(XRAY, 1, threading.Event())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    with connect(app.config["DB_PATH"]) as db:
        primary = db.execute("SELECT status,latency_ms FROM configs WHERE id=?", (config_id,)).fetchone()
        observed = db.execute("SELECT status,latency_ms FROM node_results WHERE node_id=?", (node["id"],)).fetchone()
    assert tuple(primary) == ("healthy", 123)
    assert observed["status"] == "healthy" and observed["latency_ms"] >= 70


@pytest.mark.skipif(not OPENSSL, reason="OpenSSL executable unavailable")
def test_fingerprint_must_match_before_token_is_sent(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True, timeout=20)
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"https://127.0.0.1:{server.server_port}/prefix"
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
    try:
        with pytest.raises(ValueError, match="does not match"):
            fetch_pinned_certificate(url, "00" * 32)
        assert seen == []
        assert fetch_pinned_certificate(url, digest) == cert.read_text()
        formatted = "sha256 Fingerprint=" + ":".join(digest[i:i+2] for i in range(0, 64, 2))
        assert fetch_pinned_certificate(url, formatted) == cert.read_text()
        client = NodeClient(url, "A" * 43, cert)
        assert client.post("/api/node/pull", {}) == {}
        assert seen == ["Bearer " + "A" * 43]

        # Even if another certificate is trusted in the same CA bundle, the
        # token must never be sent when the peer differs from the pinned leaf.
        other_cert, other_key = tmp_path / "other.pem", tmp_path / "other.key"
        subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                        "-keyout", str(other_key), "-out", str(other_cert)], check=True, capture_output=True, timeout=20)
        bundle = tmp_path / "bundle.pem"
        bundle.write_text(cert.read_text() + "\n" + other_cert.read_text())
        other = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        other_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        other_context.load_cert_chain(other_cert, other_key)
        other.socket = other_context.wrap_socket(other.socket, server_side=True)
        other_thread = threading.Thread(target=other.serve_forever, daemon=True)
        other_thread.start()
        try:
            pinned = NodeClient(f"https://127.0.0.1:{other.server_port}", "A" * 43, bundle)
            with pytest.raises(requests.exceptions.SSLError):
                pinned.post("/api/node/pull", {})
            assert seen == ["Bearer " + "A" * 43]
        finally:
            other.shutdown()
            other.server_close()
            other_thread.join(timeout=1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
