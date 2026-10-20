"""Download a pinned official Xray release and verify its SHA-256 before install."""
import argparse
import hashlib
import io
import os
import platform
from pathlib import Path
import urllib.request
import zipfile

VERSION = "v26.10.17"
ASSETS = {
    "linux-x86_64": ("Xray-linux-64.zip", "ecdd54eccd24c4c21a8f00968e7dbf7f5996da92383d2321170b560dc4a95158"),
    "linux-aarch64": ("Xray-linux-arm64-v8a.zip", "4ebdad86048fa499e65334f88a120b1ed17e662f1c222afd3514afdf8820cdb1"),
    "windows-amd64": ("Xray-windows-64.zip", "c10a532a9271964f2aa2f4b4c0757605ff529222cb1d5d7ab88f8ecc52e67fb0"),
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
