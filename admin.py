#!/opt/v2ray-desk/venv/bin/python
"""Root-only SSH management console. No passwords are stored in this config."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import string
import subprocess
import sys
import urllib.request

CONFIG = Path("/etc/v2ray-desk/config.json")
NGINX = Path("/etc/nginx/conf.d/v2ray-desk.conf")
APP = Path("/opt/v2ray-desk")
DATA = "/var/lib/v2ray-desk"


def valid_port(value):
    port = int(value)
    if not 1024 <= port <= 65535:
        raise ValueError("Use a port between 1024 and 65535.")
    return port


def port_free(port):
    sockets = []
    try:
        for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
            try:
                sock = socket.socket(family)
            except OSError:
                if family == socket.AF_INET6:
                    continue
                raise
            sockets.append(sock)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind((address, port))
        return True
    except OSError:
        return False
    finally:
        for sock in sockets:
            sock.close()


def random_port():
    for _ in range(500):
        port = 10000 + secrets.randbelow(50000)
        if port_free(port):
            return port
    raise ValueError("Could not find a free port.")


def valid_path(value):
    value = value.strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("Path must contain 1-64 letters, digits, underscores or hyphens.")
    return value


def random_path(length=20):
    if length not in (10, 20):
        raise ValueError("Random path length must be 10 or 20.")
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))


def save(config):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG.with_suffix(".new")
    temporary.write_text(json.dumps(config, indent=2) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, CONFIG)


def load():
    return json.loads(CONFIG.read_text())


def url(config):
    address = config["address"]
    if ":" in address:
        address = "[" + address + "]"
    return f"https://{address}:{config['port']}/{config['path']}/"


def nginx_config(config):
    port, backend, path = config["port"], config["backend_port"], config["path"]
    return f"""server {{
    listen {port} ssl;
    listen [::]:{port} ssl;
    server_name _;
    ssl_certificate /etc/v2ray-desk/cert.pem;
    ssl_certificate_key /etc/v2ray-desk/key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 4m;
    access_log off;
    location = /{path} {{ return 308 /{path}/; }}
    location ^~ /{path}/ {{
        proxy_pass http://127.0.0.1:{backend}/;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Prefix /{path};
        proxy_set_header Connection "";
        proxy_http_version 1.1;
        proxy_read_timeout 90s;
    }}
    location / {{ return 404; }}
}}
"""


def apply(config, initial=False):
    previous = NGINX.read_text() if NGINX.exists() else None
    NGINX.write_text(nginx_config(config))
    check = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    if check.returncode:
        if previous is None:
            NGINX.unlink()
        else:
            NGINX.write_text(previous)
        raise ValueError("Nginx validation failed. Previous configuration restored.\n" + check.stderr)
    if not initial:
        completed = subprocess.run(["systemctl", "reload", "nginx"])
        if completed.returncode:
            if previous is not None:
                NGINX.write_text(previous)
                subprocess.run(["systemctl", "reload", "nginx"])
            raise ValueError("Nginx reload failed; previous configuration restored.")
    save(config)


def info(config):
    print("\nV2Ray Desk")
    print("URL:          " + url(config))
    print("HTTPS port:   " + str(config["port"]))
    print("Backend:      127.0.0.1:" + str(config["backend_port"]))
    print("Login path:   /" + config["path"] + "/")
    print("Data:         " + DATA)
    print("Languages:    English / Persian")
    from panel.tester import engine_info
    print("Xray:         " + (engine_info("/opt/v2ray-desk/bin/xray")["version"] or "unavailable"))
    status = subprocess.run(["systemctl", "is-active", "v2ray-desk"], capture_output=True, text=True)
    print("Service:      " + status.stdout.strip())
    print("Password:     securely hashed; reset it to generate a new one.")
    print("SSH menu:     sudo v2ray-desk")


def check_port(config, selected=None):
    port = selected or config["port"]
    print(f"\nPort {port}: {'FREE to bind' if port_free(port) else 'IN USE / listening'}")
    if port == config["port"]:
        try:
            context = ssl.create_default_context(cafile="/etc/v2ray-desk/cert.pem")
            context.check_hostname = False  # Pin our cert while probing the loopback address.
            with urllib.request.urlopen(f"https://127.0.0.1:{port}/{config['path']}/login",
                                        context=context, timeout=8) as response:
                print("Local HTTPS: HTTP", response.status)
        except Exception as exc:
            print("Local HTTPS: unavailable (" + type(exc).__name__ + ")")
    try:
        with socket.create_connection((config["address"], port), timeout=5):
            print("Server-side public-address TCP check: OPEN")
    except OSError:
        print("Server-side public-address TCP check: CLOSED / unreachable")
    print("An external client must also test access; provider firewalls and NAT may differ.")
    if shutil.which("ufw"):
        subprocess.run(["ufw", "status"])


def change_port(config, port):
    if port == config["port"]:
        print("Already using this port.")
        return
    if not port_free(port):
        raise ValueError("This port is already in use. Nothing was changed.")
    old_port = config["port"]
    changed = {**config, "port": port}
    apply(changed)
    config.update(changed)
    # Only add a new rule when UFW is already active; never remove SSH or old rules.
    if shutil.which("ufw"):
        status = subprocess.run(["ufw", "status"], capture_output=True, text=True)
        if "Status: active" in status.stdout:
            subprocess.run(["ufw", "allow", str(port) + "/tcp"], check=True)
    print("Port changed from", old_port, "to", port)
    print("Allow the new port in your provider firewall if applicable.")
    print("Old firewall rules are preserved.")
    print(url(config))


def password_reset(generate=False):
    args = ["runuser", "-u", "v2ray-desk", "--", str(APP / "venv/bin/python"),
            str(APP / "manage.py"), "reset-password", "--data", DATA, "--show-password"]
    if generate:
        args.append("--generate")
    subprocess.run(args, check=True)


def menu(config):
    while True:
        print("\n=== V2Ray Desk / SSH Manager ===")
        print("1) Show panel details\n2) Choose a random free port\n3) Set port manually")
        print("4) Generate a random login path (10 or 20 characters)\n5) Set login path manually")
        print("6) Check port and HTTPS\n7) Generate a new password\n8) Set password manually")
        print("9) Restart panel\n10) Show recent service logs\n0) Exit")
        choice = input("Select: ").strip()
        try:
            if choice == "0":
                break
            elif choice == "1":
                info(config)
            elif choice in {"2", "3"}:
                change_port(config, random_port() if choice == "2" else valid_port(input("New port: ")))
            elif choice in {"4", "5"}:
                path = random_path(int(input("Length [20]: ").strip() or "20")) if choice == "4" else valid_path(input("Path: "))
                changed = {**config, "path": path}
                apply(changed)
                config.update(changed)
                print("New URL: " + url(config))
            elif choice == "6":
                value = input(f"Port [{config['port']}]: ").strip()
                check_port(config, valid_port(value) if value else None)
            elif choice in {"7", "8"}:
                password_reset(choice == "7")
            elif choice == "9":
                subprocess.run(["systemctl", "restart", "v2ray-desk"], check=True)
                info(config)
            elif choice == "10":
                subprocess.run(["journalctl", "-u", "v2ray-desk", "-n", "60", "--no-pager"])
        except (ValueError, subprocess.CalledProcessError) as exc:
            print("Error:", exc)


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo: sudo v2ray-desk")
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", default="menu",
                        choices=["menu", "info", "check", "set-port", "set-path", "password", "configure"])
    parser.add_argument("value", nargs="?")
    parser.add_argument("--address")
    parser.add_argument("--port")
    parser.add_argument("--backend-port", type=int, default=18080)
    parser.add_argument("--path")
    args = parser.parse_args()
    if args.command == "configure":
        config = load() if CONFIG.exists() else {}
        address = args.address or config.get("address", "")
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", address):
            raise ValueError("Invalid IP address or domain.")
        desired = args.port or config.get("port")
        port = random_port() if desired in {None, "random"} else valid_port(desired)
        path_arg = args.path or config.get("path", "random20")
        path = random_path(int(path_arg[6:])) if path_arg in {"random10", "random20"} else valid_path(path_arg)
        apply({"address": address, "port": port, "backend_port": args.backend_port, "path": path}, initial=True)
        print(url(load()))
        return
    config = load()
    if args.command == "info":
        info(config)
    elif args.command == "check":
        check_port(config, valid_port(args.value) if args.value else None)
    elif args.command == "set-port":
        change_port(config, random_port() if args.value == "random" else valid_port(args.value))
    elif args.command == "set-path":
        value = args.value or "random20"
        path = random_path(int(value[6:])) if value in {"random10", "random20"} else valid_path(value)
        config["path"] = path
        apply(config)
        print(url(config))
    elif args.command == "password":
        password_reset(args.value == "random")
    else:
        menu(config)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc))
