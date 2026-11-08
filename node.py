#!/usr/bin/env python3
"""Interactive node setup and service entry point."""
import argparse
import getpass
import json
import os
import re
import tempfile
from pathlib import Path

import requests

from panel.node_agent import controller_url, fetch_pinned_certificate, run


def replace_private(path, content, encoding):
    # Write a new private regular file and replace any old entry atomically.
    descriptor, temporary = tempfile.mkstemp(prefix=".node-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding) as file:
            file.write(content)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description="V2Ray Desk test node")
    parser.add_argument("command", choices=["configure", "run"])
    parser.add_argument("--data", default="/etc/v2ray-desk-node")
    parser.add_argument("--xray", default="/opt/v2ray-desk-node/bin/xray")
    args = parser.parse_args()
    folder = Path(args.data)
    if args.command == "run":
        run(folder / "config.json", args.xray)
        return
    url = controller_url(input("Main panel HTTPS URL: ").strip())
    fingerprint = input("Panel SHA-256 certificate fingerprint: ").strip()
    token = getpass.getpass("Node token (hidden): ").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise SystemExit("Invalid node token.")
    raw_workers = input("Parallel tests [32]: ").strip() or "32"
    if not raw_workers.isascii() or not raw_workers.isdigit() or not 1 <= int(raw_workers) <= 128:
        raise SystemExit("Parallel tests must be between 1 and 128.")
    certificate = fetch_pinned_certificate(url, fingerprint)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    cert_path = folder / "controller.pem"
    # Include standard roots for publicly trusted certificates; the agent also
    # checks the pinned leaf fingerprint on every TLS connection.
    roots = Path(requests.certs.where()).read_text(encoding="ascii")
    replace_private(cert_path, certificate + "\n" + roots, "ascii")
    config_path = folder / "config.json"
    replace_private(config_path, json.dumps({"url": url, "token": token, "certificate": str(cert_path),
                                             "workers": int(raw_workers)}), "utf-8")
    print("Node configured. Keep its token and configuration private.")


if __name__ == "__main__":
    main()
