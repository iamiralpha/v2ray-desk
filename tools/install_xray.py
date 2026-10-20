"""Download a pinned official Xray release and verify its SHA-256 before install."""
import argparse
import hashlib
import io
import os
import platform
from pathlib import Path
import urllib.request
import zipfile

VERSION = "v26.9.9"
ASSETS = {
    "linux-x86_64": ("Xray-linux-64.zip", "1eb9175d0f0a8f8149c9230a7fc5ae66ce332ed20a53155ce61fe62e3f58b7df"),
    "linux-aarch64": ("Xray-linux-arm64-v8a.zip", "3e38d72dfc5eb65c91df0e5583e9b6676c32232041da47de6ae73946b526d66c"),
    "windows-amd64": ("Xray-windows-64.zip", "244deaba2098c2964e49bba90df3707777e5f5f428a82d2f29604015f24beec2"),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    key = platform.system().lower() + "-" + platform.machine().lower()
    if key not in ASSETS:
        raise SystemExit("Supported platforms: Linux x86_64 / aarch64 and Windows AMD64.")
    name, digest = ASSETS[key]
    url = f"https://github.com/XTLS/Xray-core/releases/download/{VERSION}/{name}"
    print(f"Downloading official Xray {VERSION}: {name}", flush=True)
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read(100 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != digest:
        raise SystemExit("SHA-256 mismatch; refusing installation.")
    binary = "xray.exe" if platform.system() == "Windows" else "xray"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        content = archive.read(binary)
    destination = Path(args.destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".download")
    temporary.write_bytes(content)
    temporary.chmod(0o755)
    os.replace(temporary, destination)
    print("Verified Xray installed.", flush=True)


if __name__ == "__main__":
    main()
