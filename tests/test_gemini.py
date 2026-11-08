"""Gemini redirect policy and session behavior against isolated HTTP fixtures."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import aiohttp
import pytest

from panel import http_probe, tester


@pytest.fixture
def gemini_endpoint(monkeypatch):
    state = SimpleNamespace(routes={}, requests=[], targets=[], headers=[], released=threading.Event())

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            state.requests.append((self.path, self.headers.get("Cookie", "")))
            state.headers.append(dict(self.headers))
            reply = state.routes.get(self.path, {"code": 200})
            if callable(reply):
                reply = reply(self)
            if state.released.wait(reply.get("delay", 0)):
                return
            try:
                self.send_response(reply["code"])
                for name, value in reply.get("headers", {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", "1000000000" if reply.get("slow_body") else "0")
                self.end_headers()
                if reply.get("slow_body"):
                    state.released.wait(2)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
    thread.start()
    base = f"http://localhost:{server.server_port}"
    original_get = aiohttp.ClientSession.get

    def mapped_get(client, url, **kwargs):
        # Only this fixture maps logical destinations to loopback. Production
        # still uses the verified HTTPS URL through its SOCKS connector.
        state.targets.append(str(url))
        parsed = urlsplit(url)
        return original_get(client, base + (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""), **kwargs)

    def connector(url, **kwargs):
        assert url == "socks5://127.0.0.1:12345" and kwargs == {"rdns": True}
        return aiohttp.TCPConnector()

    monkeypatch.setattr(http_probe.ProxyConnector, "from_url", connector)
    monkeypatch.setattr(aiohttp.ClientSession, "get", mapped_get)
    try:
        yield state
    finally:
        state.released.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def redirect(location, **values):
    return {"code": 302, "headers": {"location": location}, **values}


def test_gemini_redirect_keeps_cookie_and_accepts_lowercase_location(gemini_endpoint):
    state = gemini_endpoint
    state.routes["/app"] = {"code": 302, "headers": {"location": "/ready", "Set-Cookie": "gate=pass; Path=/"}}
    state.routes["/ready"] = lambda request: {"code": 200} if "gate=pass" in request.headers.get("Cookie", "") else redirect("/app")
    assert tester.probe_gemini(12345, 1) == ("http200", 200)
    assert [path for path, _ in state.requests] == ["/app", "/ready"]
    assert "gate=pass" in state.requests[-1][1]


def test_gemini_request_uses_browser_like_html_headers(gemini_endpoint):
    assert tester.probe_gemini(12345, 1) == ("http200", 200)
    headers = gemini_endpoint.headers[0]
    assert headers["User-Agent"].startswith("Mozilla/5.0 ")
    assert "text/html" in headers["Accept"]
    assert headers["Accept-Encoding"] == "identity"


@pytest.mark.parametrize("location,expected", [
    ("https://consent.google.com/m", "redirect"),
    ("https://www.google.com/sorry/index", "redirect"),
    ("https://accounts.google.com/ServiceLogin", "login"),
])
def test_interstitial_redirect_is_classified_without_fetching_it(gemini_endpoint, location, expected):
    state = gemini_endpoint
    state.routes["/app"] = redirect(location)
    assert tester.probe_gemini(12345, 1) == (expected, 302)
    assert state.targets == [tester.GEMINI_URL]


@pytest.mark.parametrize("location", [
    "http://gemini.google.com/ready", "https://example.com/ready",
    "https://gemini.google.com.example.com/ready", "https://127.0.0.1/ready",
    "https://user@accounts.google.com/login", "https://user:password@gemini.google.com/ready",
    "https://gemini.google.com:8443/ready", "https://accounts.google.com:8443/login",
    "https://gemini.google.com:invalid/ready", "https://[invalid",
    "https://gemini.google.com/re\tady", "",
])
def test_unsafe_or_malformed_redirect_is_not_contacted(gemini_endpoint, location):
    state = gemini_endpoint
    state.routes["/app"] = redirect(location)
    assert tester.probe_gemini(12345, 1) == ("redirect", 302)
    assert state.targets == [tester.GEMINI_URL]


def test_redirect_without_location_is_still_a_response(gemini_endpoint):
    gemini_endpoint.routes["/app"] = {"code": 302}
    assert tester.probe_gemini(12345, 1) == ("redirect", 302)
    assert len(gemini_endpoint.targets) == 1


@pytest.mark.parametrize("location", ["/ready", "https://gemini.google.com:443/ready"])
def test_same_gemini_host_redirect_is_followed(gemini_endpoint, location):
    state = gemini_endpoint
    state.routes["/app"] = redirect(location)
    assert tester.probe_gemini(12345, 1) == ("http200", 200)
    assert len(state.targets) == 2


def test_gemini_headers_succeed_without_reading_huge_stalled_body(gemini_endpoint):
    gemini_endpoint.routes["/app"] = {"code": 200, "slow_body": True}
    started = time.monotonic()
    assert tester.probe_gemini(12345, .2) == ("http200", 200)
    assert time.monotonic() - started < .5


def test_redirect_chain_uses_one_total_deadline(gemini_endpoint):
    gemini_endpoint.routes["/app"] = redirect("/app", delay=.09)
    started = time.monotonic()
    assert tester.probe_gemini(12345, .12) == ("unreachable", None)
    assert time.monotonic() - started < .7
    assert len(gemini_endpoint.targets) < 6


def test_stop_during_redirect_chain_releases_request(gemini_endpoint):
    gemini_endpoint.routes["/app"] = redirect("/app", delay=.2)
    stop = threading.Event()
    timer = threading.Timer(.08, stop.set)
    timer.start()
    started = time.monotonic()
    try:
        assert tester.probe_gemini(12345, 10, stop) == ("unreachable", None)
        assert time.monotonic() - started < .6
        assert len(gemini_endpoint.targets) == 1
    finally:
        timer.cancel()


def test_redirect_loop_stops_after_six_requests(gemini_endpoint):
    gemini_endpoint.routes["/app"] = redirect("/app")
    assert tester.probe_gemini(12345, 1) == ("redirect", 302)
    assert len(gemini_endpoint.targets) == 6
