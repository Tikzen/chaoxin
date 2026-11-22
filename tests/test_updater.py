import hashlib
import json
from pathlib import Path
import shutil
from zipfile import ZipFile
import pytest
from app import updater


def test_only_stable_numeric_tags():
    assert updater.version_tuple("v1.10.0") > updater.version_tuple("v1.9.99")
    for tag in ("latest", "v1.2.3-rc1", "v1.2.3/.."):
        with pytest.raises(ValueError):
            updater.version_tuple(tag)


def test_update_requires_correct_hash_and_successful_probe_before_switch(tmp_path, monkeypatch):
    package = tmp_path / "incoming.zip"
    meta = {"version": "1.0.0", "repository": "Tikzen/inn4xxt", "launcher_protocol": 1}
    with ZipFile(package, "w") as archive:
        for name in ("Chaoxin.exe", "payload/app/main.py", "payload/static/index.html", "payload/run.py", "payload/python/python.exe"):
            archive.writestr(name, "placeholder")
        archive.writestr("payload/build-info.json", json.dumps(meta))
    prefix = "https://github.com/Tikzen/inn4xxt/releases/download/v1.0.0/"
    release = dict(update_available=True, asset_available=True, latest_version="v1.0.0", repository=meta["repository"],
                   download_url=prefix+updater.ASSET_NAME, checksum_url=prefix+updater.CHECKSUM_NAME)
    monkeypatch.setattr(updater, "portable", lambda: True)
    monkeypatch.setattr(updater, "install_root", lambda: tmp_path)
    checksum = "bad"
    actual = hashlib.sha256(package.read_bytes()).hexdigest()
    def download(url, path, limit):
        if url.endswith(".sha256"):
            path.write_text(checksum)
        else:
            shutil.copyfile(package, path)
        return actual
    monkeypatch.setattr(updater, "_download", download)
    with pytest.raises(ValueError, match="SHA-256"):
        updater.prepare(release)
    checksum = actual
    def probe(args, **opts):
        assert Path(opts["env"]["STUDY_DATA_DIR"]).is_relative_to(tmp_path / ".updates")
        raise RuntimeError("test failure")
    monkeypatch.setattr(updater.subprocess, "run", probe)
    with pytest.raises(RuntimeError):
        updater.prepare(release)
    assert not list((tmp_path/".updates").iterdir())
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **kw: None)
    key = updater.prepare(release)
    assert not (tmp_path/".updates/active").exists()
    updater.activate(key)
    assert (tmp_path/".updates/active").read_text() == key
    assert (tmp_path/".updates"/key/"payload/run.py").is_file()


@pytest.mark.parametrize("name", ["payload/../x", "payload/f:stream", "payload/CON", "payload/x. ", "payload/a\\x", ".data/master.key"])
def test_release_cannot_write_outside_payload(tmp_path, name):
    zip_path = tmp_path / "bad.zip"
    with ZipFile(zip_path, "w") as z:
        z.writestr(name, "invalid")
    with pytest.raises(ValueError, match="不安全"):
        updater._safe_extract(zip_path, tmp_path/"out")
