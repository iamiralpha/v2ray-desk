#!/usr/bin/env bash
# Download the public project and run its interactive Ubuntu installer.
set -euo pipefail
umask 077
mode="${1:-panel}"
if [[ "$mode" != panel && "$mode" != node ]]; then
  echo "Usage: sudo bash install-online.sh [panel|node]" >&2
  exit 2
fi
if [[ $EUID -ne 0 ]]; then
  echo "Run this installer with sudo." >&2
  exit 1
fi
command -v curl >/dev/null || { echo "Install curl first: sudo apt-get install -y curl" >&2; exit 1; }
command -v python3 >/dev/null || { echo "Python 3 is required." >&2; exit 1; }
ref="${V2RAY_DESK_REF:-main}"
if [[ ! "$ref" =~ ^[A-Za-z0-9._-]{1,80}$ || "$ref" == *..* ]]; then
  echo "Invalid release reference." >&2
  exit 1
fi
tmpdir="$(mktemp -d)"
trap 'rm -rf -- "$tmpdir"' EXIT
url="https://codeload.github.com/iamiralpha/v2ray-desk/tar.gz/$ref"
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
  --connect-timeout 15 --max-time 120 --max-filesize 20971520 "$url" -o "$tmpdir/source.tar.gz"
mkdir "$tmpdir/source"
# Extract only bounded ordinary files and directories. Never follow archive links
# or allow archive paths to escape the private temporary folder.
python3 - "$tmpdir/source.tar.gz" "$tmpdir/source" <<'PY'
import pathlib, sys, tarfile
archive, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
total = 0
with tarfile.open(archive, 'r|gz') as source:
    count = 0
    top = None
    for item in source:
        count += 1
        if count > 1000:
            raise SystemExit('Invalid source archive.')
        path = pathlib.PurePosixPath(item.name)
        if path.is_absolute() or '..' in path.parts or not path.parts:
            raise SystemExit('Unsafe source archive path.')
        if top is None:
            top = path.parts[0]
        if path.parts[0] != top or not (item.isfile() or item.isdir()):
            raise SystemExit('Unsafe source archive member.')
        if item.isfile() and len(path.parts) < 2:
            raise SystemExit('Invalid source archive root.')
        total += item.size
        if item.size > 10 * 1024 * 1024 or total > 64 * 1024 * 1024:
            raise SystemExit('Source archive is too large.')
        target = root.joinpath(*path.parts[1:])
        if item.isdir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = source.extractfile(item)
            if stream is None:
                raise SystemExit('Cannot read source archive.')
            with stream, target.open('wb') as output:
                while chunk := stream.read(65536):
                    output.write(chunk)
    if not count:
        raise SystemExit('Invalid source archive.')
PY
if [[ "$mode" == node ]]; then
  bash "$tmpdir/source/install-node.sh"
else
  bash "$tmpdir/source/install.sh"
fi
