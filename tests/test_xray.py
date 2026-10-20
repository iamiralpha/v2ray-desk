"""Optional integration tests against the real Xray binary; no live user configs."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from panel import tester
from panel.parser import parse_uri

XRAY = os.environ.get("XRAY_TEST_BIN")
pytestmark = pytest.mark.skipif(not XRAY, reason="Set XRAY_TEST_BIN to run real engine tests")
UUID = "6b8744fb-aeb4-4fc7-938e-adb6b18b835a"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("protocol,transport", [
    ("vless", "tcp"), ("vless", "ws"), ("vless", "grpc"),
    ("vless", "httpupgrade"), ("vless", "xhttp"), ("trojan", "tcp"),
])
def test_engine_accepts_generated_config(tmp_path, protocol, transport):
    uri = f"{protocol}://{UUID}@example.com:443?security=tls&type={transport}&sni=example.com"
    config = tester.make_config(parse_uri(uri), "1.1.1.1", free_port())
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    completed = subprocess.run([XRAY, "run", "-test", "-c", str(path)], capture_output=True, timeout=10)
    assert completed.returncode == 0, completed.stdout.decode(errors="replace")


@pytest.fixture
def real_proxy(tmp_path, monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_GET(self):
            if self.path == "/delay":
                time.sleep(.08)
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif self.path == "/gemini":
                self.send_response(403)
                self.send_header("Content-Length", "9")
                self.end_headers()
                self.wfile.write(b"Forbidden")
            else:
                self.send_response(200)
                self.send_header("Content-Length", str(len(b"ip=1.1.1.1\nloc=DE\n")))
                self.end_headers()
                self.wfile.write(b"ip=1.1.1.1\nloc=DE\n")
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    proxy_port = free_port()
    config = {
        "log": {"loglevel": "none"},
        "inbounds": [{"listen":"127.0.0.1","port":proxy_port,"protocol":"shadowsocks",
                      "settings":{"method":"aes-256-gcm","password":"test-password","network":"tcp"}}],
        "outbounds":[{"protocol":"freedom","settings":{"finalRules":[{"action":"allow","ip":["127.0.0.1/32"]}]}}],
    }
    path = tmp_path / "server.json"
    path.write_text(json.dumps(config))
    process = subprocess.Popen([XRAY, "run", "-c", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                with socket.create_connection(("127.0.0.1", proxy_port), timeout=.2):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.05)
        monkeypatch.setattr(tester, "public_address", lambda *args: "127.0.0.1")
        monkeypatch.setattr(tester, "TRACE_URL", f"http://127.0.0.1:{http.server_port}/trace")
        monkeypatch.setitem(tester.DELAY_TARGETS, "google", f"http://127.0.0.1:{http.server_port}/delay")
        monkeypatch.setattr(tester, "GEMINI_URL", f"http://127.0.0.1:{http.server_port}/gemini")
        auth = base64.b64encode(b"aes-256-gcm:test-password").decode()
        yield f"ss://{auth}@127.0.0.1:{proxy_port}#integration"
    finally:
        process.terminate()
        process.wait(timeout=5)
        http.shutdown()
        http.server_close()


def test_real_proxy_roundtrip_and_gemini_403(real_proxy):
    outcome = tester.probe(real_proxy, XRAY, threading.Event(), timeout=2, samples=2)
    assert outcome["status"] == "healthy"
    assert outcome["latency_ms"] >= 70
    assert len(json.loads(outcome["delay_samples"])) == 2
    assert outcome["delay_version"] == 3 and outcome["delay_target"] == "google"
    assert outcome["successes"] == 2 and outcome["country"] == "DE"
    assert outcome["gemini_status"] == "blocked" and outcome["gemini_code"] == 403


def test_open_tcp_port_with_wrong_credentials_is_not_healthy(real_proxy):
    wrong = base64.b64encode(b"aes-256-gcm:wrong-password").decode()
    original = real_proxy.split("@", 1)[1]
    outcome = tester.probe("ss://" + wrong + "@" + original, XRAY, threading.Event(), timeout=1, samples=2)
    assert outcome["status"] == "failed"
    assert outcome["gemini_status"] == "unreachable"


def test_bulk_shared_core_keeps_nodes_isolated(real_proxy, tmp_path, monkeypatch):
    from panel.store import initialize, connect
    wrong = base64.b64encode(b"aes-256-gcm:wrong-password").decode()
    bad_uri = "ss://" + wrong + "@" + real_proxy.split("@", 1)[1]
    db_path = initialize(tmp_path / "bulk")
    with connect(db_path) as db:
        for uri in (real_proxy, bad_uri):
            parsed = parse_uri(uri)
            db.execute("INSERT INTO configs(uri,name,protocol,host,port,fingerprint) VALUES(?,?,?,?,?,?)",
                       [parsed[key] for key in ("uri", "name", "protocol", "host", "port", "fingerprint")])
    original = subprocess.Popen
    cores = []
    def tracked(args, **kwargs):
        process = original(args, **kwargs)
        if "run" in args and "-test" not in args:
            cores.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", tracked)
    manager = tester.TestManager(db_path, XRAY)
    manager.start([1, 2], {"workers": 2, "timeout": 2, "country": False, "gemini": False})
    deadline = time.monotonic() + 10
    while manager.snapshot()["running"] and time.monotonic() < deadline:
        time.sleep(.05)
    assert not manager.snapshot()["running"] and manager.snapshot()["done"] == 2
    with connect(db_path) as db:
        rows = db.execute("SELECT status,latency_ms FROM configs ORDER BY id").fetchall()
    assert rows[0]["status"] == "healthy"
    assert rows[1]["status"] == "failed" and rows[1]["latency_ms"] is None
    assert len(cores) == 1 and cores[0].poll() is not None


def test_invalid_engine_settings_do_not_fail_valid_neighbor(real_proxy, tmp_path):
    from panel.store import initialize, connect
    bad = f"vless://{UUID}@example.com:443?encryption=invalid-secret-value"
    db_path = initialize(tmp_path / "invalid-batch")
    with connect(db_path) as db:
        for uri in (real_proxy, bad):
            parsed = parse_uri(uri)
            db.execute("INSERT INTO configs(uri,name,protocol,host,port,fingerprint) VALUES(?,?,?,?,?,?)",
                       [parsed[k] for k in ("uri", "name", "protocol", "host", "port", "fingerprint")])
    manager = tester.TestManager(db_path, XRAY)
    assert manager.engine["available"] and manager.engine["version"]
    manager.start([1, 2], {"workers": 2, "timeout": 2, "country": False, "gemini": False})
    deadline = time.monotonic() + 15
    while manager.snapshot()["running"] and time.monotonic() < deadline:
        time.sleep(.05)
    assert not manager.snapshot()["running"] and manager.snapshot()["done"] == 2
    with connect(db_path) as db:
        rows = db.execute("SELECT status,error FROM configs ORDER BY id").fetchall()
    assert rows[0]["status"] == "healthy"
    assert rows[1]["status"] == "invalid" and "encryption" in rows[1]["error"]
    assert "invalid-secret-value" not in rows[1]["error"] and UUID not in rows[1]["error"]
