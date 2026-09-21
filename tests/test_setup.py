import hashlib
import io
import json
import platform
import tarfile

import pytest

from mba.setup import install


def archive(tmp_path, version="0.2.0", extra=None):
    path = tmp_path / "runtime.tar.gz"
    rows = {
        "mba-media": b"test binary",
        "runtime.json": json.dumps(
            {
                "version": version,
                "system": platform.system(),
                "machine": platform.machine(),
            }
        ).encode(),
    }
    if extra:
        rows[extra] = b"escape"
    with tarfile.open(path, "w:gz") as tar:
        for name, data in rows.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_setup_checks_checksum_platform_version_and_archive_members(tmp_path):
    path, sha = archive(tmp_path)
    with pytest.raises(ValueError, match="sha256"):
        install(tmp_path / "installed", media_archive=path, sha256="wrong")
    target = install(tmp_path / "installed", media_archive=path, sha256=sha)
    assert (target / "mba-media").read_bytes() == b"test binary"
    path, sha = archive(tmp_path, version="99")
    with pytest.raises(ValueError, match="mismatch"):
        install(tmp_path / "installed", media_archive=path, sha256=sha)
    path, sha = archive(tmp_path, extra="../escape")
    with pytest.raises(ValueError, match="Unexpected"):
        install(tmp_path / "installed", media_archive=path, sha256=sha)
    assert not (tmp_path / "escape").exists()
