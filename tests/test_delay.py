"""Real HTTP behavior of the two-request timer, without external websites."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import socketserver

import aiohttp
import pytest

from panel import delay


@pytest.fixture
def endpoint(monkeypatch):
    events = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_GET(self):
            events.append((self.client_address, time.monotonic(), self.path))
            if self.path == "/broken" and len(events) == 2:
                self.connection.shutdown(2)
                self.connection.close()
                return
            time.sleep(.09 if len(events) == 1 else .025)
            slow = self.path == "/slow"
            self.send_response(403 if self.path == "/403" else 200)
            self.send_header("Content-Length", "20" if slow else "1")
            self.end_headers()
            try:
                if slow:
                    for _ in range(20):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(.1)
                else:
                    self.wfile.write(b"x")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Only this unit fixture substitutes the SOCKS connector. Real Xray tests
    # separately exercise the complete proxy path and per-node isolation.
    monkeypatch.setattr(delay.ProxyConnector, "from_url", lambda *a, **k: aiohttp.TCPConnector())
    try:
        yield f"http://127.0.0.1:{server.server_port}", events
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("path,code", [("/delay", 200), ("/403", 403)])
def test_two_gets_reuse_connection_return_minimum_and_preserve_status(endpoint, path, code):
    url, events = endpoint
    value, timings, codes = delay.measure_delay(1, url + path, 2, threading.Event())
    assert len(events) == 2 and len(timings) == 2
    assert events[0][0] == events[1][0]  # Same server-side TCP connection.
    # Windows clocks can quantize two measurements by roughly 16 ms each.
    assert events[1][1] - events[0][1] >= .15  # First response + 100 ms.
    assert 15 <= timings[1] < timings[0] and value == timings[1]
    assert codes == [code, code]  # HttpClient GET times non-2xx too.


def test_one_deadline_includes_full_body_and_both_requests(endpoint):
    url, _ = endpoint
    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        delay.measure_delay(1, url + "/slow", .35, threading.Event())
    assert time.monotonic() - started < 1


def test_deadline_includes_second_wait(endpoint):
    url, events = endpoint
    with pytest.raises(asyncio.TimeoutError):
        delay.measure_delay(1, url + "/delay", .26, threading.Event())
    assert len(events) == 2


def test_cancellation_interrupts_inflight_body(endpoint):
    url, _ = endpoint
    stop = threading.Event()
    timer = threading.Timer(.2, stop.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            delay.measure_delay(1, url + "/slow", 10, stop)
        assert time.monotonic() - started < 1
    finally:
        timer.cancel()


@pytest.fixture
def stalled_tls_proxy():
    """SOCKS succeeds but the remote side never answers the TLS ClientHello."""
    released = threading.Event()
    clients_closed = threading.Event()
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(3)
            version, count = self.rfile.read(2)
            self.rfile.read(count)
            self.wfile.write(b"\x05\x00")
            header = self.rfile.read(4)
            if len(header) != 4:
                return
            size = {1:4,4:16}.get(header[3])
            if header[3] == 3:
                size = self.rfile.read(1)[0]
            self.rfile.read(size + 2)
            self.wfile.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
            try:
                while not released.is_set():
                    if not self.connection.recv(8192):
                        clients_closed.set()
                        break
            except (TimeoutError, ConnectionResetError, ConnectionAbortedError):
                pass
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1], clients_closed
    finally:
        released.set()
        server.shutdown()
        server.server_close()


def test_stalled_tls_obeys_total_deadline_and_releases_socket(stalled_tls_proxy):
    port, closed = stalled_tls_proxy
    started = time.monotonic()
    with pytest.raises((TimeoutError, aiohttp.ClientError)):
        delay.measure_delay(port, "https://example.com/generate_204", .3, threading.Event())
    assert time.monotonic() - started < 1.5
    assert closed.wait(.5)


def test_stop_cancels_stalled_tls_without_waiting_for_handshake(stalled_tls_proxy):
    port, closed = stalled_tls_proxy
    stop = threading.Event()
    timer = threading.Timer(.2, stop.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            delay.measure_delay(port, "https://example.com/generate_204", 20, stop)
        assert time.monotonic() - started < 1.5
        assert closed.wait(.5)
    finally:
        timer.cancel()
