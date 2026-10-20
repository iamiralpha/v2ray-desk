import os
import stat

import pytest

from panel.store import connect, initialize, setting


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Requires POSIX file permissions")


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def assert_private_database(path):
    assert mode(path.parent) == 0o700
    assert mode(path) == 0o600
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        assert sidecar.exists()
        assert mode(sidecar) == 0o600


def test_new_database_and_sidecars_are_private_with_permissive_umask(tmp_path):
    previous = os.umask(0)
    try:
        path = initialize(tmp_path / "data")
        with connect(path) as db:
            db.execute("INSERT INTO settings VALUES ('permission_test','retained')")
            assert_private_database(path)
    finally:
        os.umask(previous)


def test_existing_public_directory_database_and_sidecars_are_restricted(tmp_path):
    path = initialize(tmp_path / "data")
    # Keep another connection open so the existing WAL/SHM files survive the
    # initialization pass, as they can during a restart or password reset.
    with connect(path) as db:
        db.execute("INSERT INTO settings VALUES ('permission_test','retained')")
        db.commit()
        path.parent.chmod(0o755)
        path.chmod(0o666)
        for suffix in ("-wal", "-shm"):
            path.with_name(path.name + suffix).chmod(0o666)

        initialize(path.parent)

        assert_private_database(path)
        assert setting(path, "permission_test") == "retained"
