"""Download a pinned official Xray release and verify its SHA-256 before install."""
import argparse
import hashlib
import io
import os
import platform
from pathlib import Path
import urllib.request
import zipfile

VERSION = "v26.10.31"
ASSETS = {
    "linux-x86_64": ("Xray-linux-64.zip", "e4bd9119f50ae7c41658b1abd0679ae06f98bd864a2907ff33f2f2cf7081b214"),
    "linux-aarch64": ("Xray-linux-arm64-v8a.zip", "1811c8f1da0c3b6dc707ebaf1c8390e987ba39859a8f0a7aadd8526157f2ec8d"),
    "windows-amd64": ("Xray-windows-64.zip", "2e04a9995a6f2ad1b28c0bb25c708ecb2695e43a1c9c7889c4b6ac2b48cf7a6a"),
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
