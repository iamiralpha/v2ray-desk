import io
import socket
from types import SimpleNamespace

import pytest

import admin
from admin import nginx_config, port_free, random_path, valid_path, valid_port


def test_generated_paths_and_input_validation():
    assert len(random_path(10)) == 10
    assert len(random_path(20)) == 20
    assert valid_path("/a_safe-path/") == "a_safe-path"
    for value in ["../etc", "a;b", "", "x" * 65, "a/b"]:
        with pytest.raises(ValueError):
            valid_path(value)
    for value in [0, 80, 65536]:
        with pytest.raises(ValueError):
            valid_port(value)


def test_busy_port_is_detected():
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))
        sock.listen()
        assert not port_free(sock.getsockname()[1])


def test_prefix_proxy_and_no_root_route():
    text = nginx_config({"port": 48123, "backend_port":18080, "path":"AbCdEf1234567890abcd"})
    assert "listen 48123 ssl;" in text
    assert "location / { return 404; }" in text
    assert "proxy_pass http://127.0.0.1:18080/;" in text
    assert "X-Forwarded-Prefix /AbCdEf1234567890abcd" in text


def test_detect_address_uses_unambiguous_public_interface_without_external_request(monkeypatch):
    monkeypatch.setattr(admin.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="10.0.0.2 93.184.216.34\n"))
    monkeypatch.setattr(admin.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("external lookup was unnecessary"))
    assert admin.detect_address() == "93.184.216.34"


def test_detect_address_falls_back_to_valid_public_ipv4(monkeypatch):
    monkeypatch.setattr(admin.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="10.0.0.2\n"))
    answers = iter([b"10.0.0.10", b"93.184.216.34\n"])
    monkeypatch.setattr(admin.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(next(answers)))
    assert admin.detect_address() == "93.184.216.34"


def test_detect_address_returns_empty_when_public_address_is_unavailable(monkeypatch):
    monkeypatch.setattr(admin.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="192.168.1.2\n"))
    def unavailable(*args, **kwargs):
        raise TimeoutError("offline")
    monkeypatch.setattr(admin.urllib.request, "urlopen", unavailable)
    assert admin.detect_address() == ""


def test_random_port_skips_the_backend_port(monkeypatch):
    numbers = iter([8080, 8081])
    monkeypatch.setattr(admin.secrets, "randbelow", lambda limit: next(numbers))
    monkeypatch.setattr(admin, "port_free", lambda port: True)
    assert admin.random_port(exclude={18080}) == 18081


def test_ssh_menu_uses_green_terminal_style_and_describes_panel_actions(monkeypatch, capsys):
    monkeypatch.setattr(admin, "terminal_colors", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "0")
    admin.menu({"address": "example.com", "port": 12345, "backend_port": 18080, "path": "safe-path"})
    output = capsys.readouterr().out
    assert "\x1b[" in output and "V2RAY DESK" in output
    assert "Panel details" in output and "Check port" in output and "password" in output.lower()
