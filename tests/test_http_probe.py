"""Metadata deadlines, cancellation and decoded limits against local endpoints."""
import gzip
import gc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socketserver
import threading
import time
import warnings

import aiohttp
import pytest

from panel import http_probe, tester


TRACE = b"ip=1.1.1.1\nloc=DE\n"


def test_already_cancelled_operation_is_closed_without_running():
    entered = []

    async def operation():
        entered.append(True)

    stop = threading.Event()
    stop.set()
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        with pytest.raises(TimeoutError):
            http_probe.run_bounded(operation(), 1, stop)
        gc.collect()
    assert not entered
    assert not [item for item in recorded if "was never awaited" in str(item.message)]


@pytest.fixture
def metadata_endpoint(monkeypatch):
    released = threading.Event()
    disconnected = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.close_connection = True
            try:
                if self.path == "/slow-headers":
                    headers = b"HTTP/1.1 200 OK\r\nX-Padding: " + b"a" * 200 + b"\r\nContent-Length: 0\r\n\r\n"
                    for value in headers:
                        self.connection.sendall(bytes([value]))
                        if released.wait(.02):
                            return
                    return
                body = TRACE
                encoding = None
                if self.path == "/slow-body":
                    body += b"padding=" + b"x" * 200
                elif self.path.startswith("/gzip"):
                    encoding = "gzip"
                    body = gzip.compress(b"x" * 20000 if self.path == "/gzip-oversize" else body)
                    if self.path == "/gzip-truncated":
                        body = body[:-8]
                    elif self.path == "/gzip-corrupt":
                        body = body[:-8] + bytes([body[-8] ^ 1]) + body[-7:]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                if encoding:
                    self.send_header("Content-Encoding", encoding)
                self.end_headers()
                if self.path == "/slow-body":
                    for value in body:
                        self.wfile.write(bytes([value]))
                        self.wfile.flush()
                        if released.wait(.02):
                            return
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                disconnected.set()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
    thread.start()
    # Real Xray tests separately cover the SOCKS transport. These fixtures
    # exercise actual HTTP framing, streaming and cancellation deterministically.
    monkeypatch.setattr(http_probe.ProxyConnector, "from_url", lambda *args, **kwargs: aiohttp.TCPConnector())
    try:
        yield f"http://127.0.0.1:{server.server_port}", disconnected
    finally:
        released.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize("operation,path", [
    ("country", "/slow-body"), ("country", "/slow-headers"), ("gemini", "/slow-headers"),
])
def test_metadata_deadline_bounds_slow_headers_and_body(metadata_endpoint, monkeypatch, operation, path):
    url, disconnected = metadata_endpoint
    monkeypatch.setattr(tester, "TRACE_URL" if operation == "country" else "GEMINI_URL", url + path)
    started = time.monotonic()
    outcome = tester.lookup_country(1, .12) if operation == "country" else tester.probe_gemini(1, .12)
    assert time.monotonic() - started < 1
    assert outcome == ((None, None) if operation == "country" else ("unreachable", None))
    assert disconnected.wait(1), "Timed-out request must release its connection"


@pytest.mark.parametrize("operation,path", [("country", "/slow-body"), ("gemini", "/slow-headers")])
def test_stop_cancels_metadata_and_releases_connection(metadata_endpoint, monkeypatch, operation, path):
    url, disconnected = metadata_endpoint
    monkeypatch.setattr(tester, "TRACE_URL" if operation == "country" else "GEMINI_URL", url + path)
    stop = threading.Event()
    timer = threading.Timer(.15, stop.set)
    timer.start()
    started = time.monotonic()
    try:
        outcome = tester.lookup_country(1, 20, stop) if operation == "country" else tester.probe_gemini(1, 20, stop)
        assert time.monotonic() - started < 1
        assert outcome == ((None, None) if operation == "country" else ("unreachable", None))
        assert disconnected.wait(1), "Cancelled request must release its connection"
    finally:
        timer.cancel()


def test_metadata_accepts_valid_gzip_body(metadata_endpoint, monkeypatch):
    url, _ = metadata_endpoint
    monkeypatch.setattr(tester, "TRACE_URL", url + "/gzip-valid")
    assert tester.lookup_country(1, 1) == ("DE", "1.1.1.1")


def test_metadata_enforces_decoded_gzip_limit(metadata_endpoint):
    url, _ = metadata_endpoint
    with pytest.raises(http_probe.BodyTooLarge):
        http_probe.proxy_get(1, url + "/gzip-oversize", 1, limit=16384)


@pytest.mark.parametrize("path", ["/gzip-truncated", "/gzip-corrupt"])
def test_invalid_gzip_is_metadata_failure(metadata_endpoint, monkeypatch, path):
    url, _ = metadata_endpoint
    monkeypatch.setattr(tester, "TRACE_URL", url + path)
    assert tester.lookup_country(1, 1) == (None, None)


def test_socks_metadata_failure_preserves_successful_delay(monkeypatch):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(2)
            header = self.rfile.read(2)
            if len(header) != 2:
                return
            self.rfile.read(header[1])
            self.wfile.write(b"\x05\x00")
            header = self.rfile.read(4)
            if len(header) != 4:
                return
            size = {1: 4, 4: 16}.get(header[3])
            if header[3] == 3:
                size = self.rfile.read(1)[0]
            self.rfile.read(size + 2)
            self.wfile.write(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
    thread.start()
    monkeypatch.setattr(tester, "measure_delay", lambda *args: (10, [20, 10], [200, 200]))
    try:
        outcome = tester.probe_connection(server.server_address[1], threading.Event(), timeout=1)
        assert outcome["status"] == "healthy" and outcome["latency_ms"] == 10
        assert outcome["country"] is None and outcome["exit_ip"] is None
        assert outcome["gemini_status"] == "unreachable" and outcome["gemini_code"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
