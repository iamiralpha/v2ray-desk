import socket

import pytest

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
