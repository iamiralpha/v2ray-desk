import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from panel import subscriptions
from panel.parser import ConfigError


@pytest.fixture
def slow_source(monkeypatch):
    stopped = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if self.path == "/headers":
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                    while not stopped.wait(.03):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", "10000")
                    self.end_headers()
                    while not stopped.wait(.03):
                        self.wfile.write(b"x")
                        self.wfile.flush()
            except OSError:
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Only the isolated fixture bypasses the public-address restriction.
    monkeypatch.setattr(subscriptions, "public_address", lambda *args: "127.0.0.1")
    monkeypatch.setattr(subscriptions, "DOWNLOAD_TIMEOUT", .25)
    yield f"http://example.com:{server.server_port}"
    stopped.set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.mark.parametrize("path", ["/headers", "/body"])
def test_absolute_deadline_interrupts_trickle_response(slow_source, path):
    started = time.monotonic()
    with pytest.raises(ConfigError, match="timed out"):
        subscriptions.fetch_subscription(slow_source + path)
    assert time.monotonic() - started < 1


def test_dns_stall_does_not_hold_http_worker_after_deadline(monkeypatch):
    release = threading.Event()
    entered = threading.Event()
    def resolve(*args):
        entered.set()
        assert release.wait(3)
        return "1.1.1.1"
    monkeypatch.setattr(subscriptions, "public_address", resolve)
    monkeypatch.setattr(subscriptions, "DOWNLOAD_TIMEOUT", .2)
    started = time.monotonic()
    try:
        with pytest.raises(ConfigError, match="timed out"):
            subscriptions.fetch_subscription("https://example.com/sub?token=private")
        assert entered.is_set() and time.monotonic() - started < 1
    finally:
        release.set()
