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
        def do_GET(self):
            if self.path == "/gemini":
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
        monkeypatch.setattr(tester, "GEMINI_URL", f"http://127.0.0.1:{http.server_port}/gemini")
        auth = base64.b64encode(b"aes-256-gcm:test-password").decode()
        yield f"ss://{auth}@127.0.0.1:{proxy_port}#integration"
    finally:
        process.terminate()
        process.wait(timeout=5)
        http.shutdown()
        http.server_close()


def test_real_proxy_roundtrip_and_gemini_403(real_proxy):
    outcome = tester.probe(real_proxy, XRAY, threading.Event(), timeout=2, samples=3)
    assert outcome["status"] == "healthy"
    assert outcome["latency_ms"] > 0
    assert outcome["successes"] == 3 and outcome["country"] == "DE"
    assert outcome["gemini_status"] == "blocked" and outcome["gemini_code"] == 403


def test_open_tcp_port_with_wrong_credentials_is_not_healthy(real_proxy):
    wrong = base64.b64encode(b"aes-256-gcm:wrong-password").decode()
    original = real_proxy.split("@", 1)[1]
    outcome = tester.probe("ss://" + wrong + "@" + original, XRAY, threading.Event(), timeout=1, samples=1)
    assert outcome["status"] == "failed"
    assert outcome["gemini_status"] == "unreachable"
