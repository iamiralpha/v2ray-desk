"""Exercise the public bootstrap's archive safety before it runs an installer."""
import io
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


def extract(tmp_path, members):
    archive, destination = tmp_path / "release.tar.gz", tmp_path / "source"
    destination.mkdir()
    with tarfile.open(archive, "w:gz") as output:
        for name, content, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "link":
                info.type, info.linkname = tarfile.SYMTYPE, "../../outside"
                output.addfile(info)
            else:
                info.size = len(content)
                output.addfile(info, io.BytesIO(content))
    script = (Path(__file__).parents[1] / "install-online.sh").read_text(encoding="utf-8")
    embedded = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    return subprocess.run([sys.executable, "-c", embedded, str(archive), str(destination)],
                          capture_output=True, text=True, timeout=8), destination


def test_bootstrap_extracts_normal_release_without_changing_archive_paths(tmp_path):
    result, destination = extract(tmp_path, [("v2ray-desk-main/install.sh", b"installer", "file"),
                                             ("v2ray-desk-main/panel/app.py", b"app", "file")])
    assert result.returncode == 0
    assert (destination / "install.sh").read_bytes() == b"installer"
    assert (destination / "panel/app.py").read_bytes() == b"app"


@pytest.mark.parametrize("member", ["v2ray-desk-main/../../outside", "/etc/important", "other/other-file"])
def test_bootstrap_rejects_paths_outside_one_release_root(tmp_path, member):
    result, _ = extract(tmp_path, [("v2ray-desk-main/install.sh", b"ok", "file"), (member, b"bad", "file")])
    assert result.returncode != 0
    assert not (tmp_path / "outside").exists()


def test_bootstrap_rejects_symlink_members(tmp_path):
    result, _ = extract(tmp_path, [("v2ray-desk-main/link", b"", "link")])
    assert result.returncode != 0
