"""GitHub Releases updater for the portable Windows build."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import ntpath
import zipfile
from pathlib import Path

import requests

from app import store

ASSET_NAME = "chaoxin-windows-x64.zip"
CHECKSUM_NAME = ASSET_NAME + ".sha256"
MAX_RELEASE_BYTES = 600 * 1024 * 1024


def install_root():
    return Path(os.environ.get('STUDY_PORTABLE_ROOT', store.ROOT))


def build_info():
    path = store.ROOT / "build-info.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except (OSError, ValueError):
        pass
    try:
        import tomllib
        with (store.ROOT / "pyproject.toml").open("rb") as file:
            project = tomllib.load(file)["project"]
        return {"version": project.get("version", "0.0.0"), "repository": "Tikzen/chaoxin"}
    except (OSError, ValueError, KeyError):
        return {"version": "0.0.0", "repository": "Tikzen/chaoxin"}


def repository():
    configured = store.setting("update_repository", "")
    value = configured or os.environ.get("STUDY_GITHUB_REPOSITORY") or build_info().get("repository", "")
    value = str(value).strip()
    match = re.fullmatch(r"(?:https://github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", value)
    if not match:
        return ""
    return f"{match.group(1)}/{match.group(2)}"


def version_tuple(value):
    match = re.fullmatch(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", str(value))
    if not match:
        raise ValueError("Release 版本号无效")
    return tuple(map(int, match.groups()))


def _asset_map(release):
    return {item.get("name"): item for item in release.get("assets", []) if isinstance(item, dict)}


def check_release(repo=None):
    repo = repo or repository()
    if not repo:
        return {"configured": False, "repository": "", "current_version": build_info().get("version", "0.0.0")}
    response = requests.get(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "kezhan-portable-updater"},
        timeout=(8, 20),
    )
    if response.status_code == 404:
        return {"configured": True, "repository": repo, "current_version": build_info()["version"],
                "latest_version": None, "update_available": False}
    response.raise_for_status()
    release = response.json()
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        raise ValueError("GitHub 没有可用的正式 Release")
    current = str(build_info().get("version", "0.0.0"))
    tag = str(release.get("tag_name", ""))
    assets = _asset_map(release)
    binary = assets.get(ASSET_NAME)
    checksum = assets.get(CHECKSUM_NAME)
    update_available = version_tuple(tag) > version_tuple(current)
    return {
        "configured": True,
        "repository": repo,
        "current_version": current,
        "latest_version": tag,
        "update_available": update_available,
        "name": str(release.get("name") or tag),
        "notes": str(release.get("body") or ""),
        "release_url": str(release.get("html_url") or ""),
        "asset_available": bool(binary and checksum),
        "asset_size": int(binary.get("size", 0)) if binary else 0,
        "download_url": str(binary.get("browser_download_url", "")) if binary else "",
        "checksum_url": str(checksum.get("browser_download_url", "")) if checksum else "",
    }


def _download(url, destination, limit):
    digest = hashlib.sha256()
    size = 0
    with requests.get(url, headers={"User-Agent": "kezhan-portable-updater"},
                      timeout=(15, 90), stream=True) as response:
        response.raise_for_status()
        length = response.headers.get("Content-Length")
        if length and int(length) > limit:
            raise ValueError("更新包超过允许的大小")
        with destination.open("wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > limit:
                    raise ValueError("更新包超过允许的大小")
                digest.update(chunk)
                output.write(chunk)
    return digest.hexdigest()


def _safe_extract(archive_path, destination):
    required = {"Chaoxin.exe", "payload/python/python.exe", "payload/app/main.py",
                "payload/static/index.html", "payload/build-info.json", "payload/run.py"}
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if len(infos) > 100000 or sum(e.file_size for e in infos) > 2 * 1024**3:
            raise ValueError("解压后的更新包过大")
        found = set()
        for info in infos:
            name = info.filename.rstrip("/")
            parts = name.split("/")
            if (not name or (parts[0] != "payload" and name != "Chaoxin.exe")
                or any(not part or part in (".", "..") or part.endswith((".", " "))
                       or ntpath.isreserved(part) or re.search(r'[<>:"|?*\\\x00-\x1f]', part)
                       for part in parts)
                or info.external_attr >> 28 == 10 or name.casefold() in found):
                raise ValueError("更新包包含不安全的路径")
            found.add(name.casefold())
            target = destination.joinpath(*parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
        if not {name.casefold() for name in required} <= found:
            raise ValueError("更新包缺少程序文件")


def portable():
    return os.name == "nt" and os.environ.get("STUDY_PORTABLE_WINDOWS") == "1" and bool(os.environ.get("STUDY_PORTABLE_ROOT"))


def prepare(release):
    if not portable():
        raise ValueError("一键更新仅适用于 Windows 绿色版")
    if not release.get("update_available") or not release.get("asset_available"):
        raise ValueError("没有可安装的 Windows 更新")
    repo, tag = release["repository"], release["latest_version"]
    version_tuple(tag)
    prefix = f"https://github.com/{repo}/releases/download/{tag}/"
    if release["download_url"] != prefix + ASSET_NAME or release["checksum_url"] != prefix + CHECKSUM_NAME:
        raise ValueError("Release 下载地址无效")
    updates = install_root() / ".updates"
    updates.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=updates) as tmp:
        work = Path(tmp)
        checksum = work / CHECKSUM_NAME
        _download(release["checksum_url"], checksum, 4096)
        expected = checksum.read_text(encoding="ascii").split()
        archive = work / ASSET_NAME
        actual = _download(release["download_url"], archive, MAX_RELEASE_BYTES)
        if not expected or actual != expected[0].lower():
            raise ValueError("更新包 SHA-256 校验失败")
        stage = work / "stage"
        _safe_extract(archive, stage)
        payload = stage / "payload"
        info = json.loads((payload / "build-info.json").read_text(encoding="utf8"))
        if (info.get("version") != tag.removeprefix("v") or info.get("repository") != repo
                or info.get("launcher_protocol") != 1):
            raise ValueError("更新包版本、来源或启动协议不匹配")
        subprocess.run([str(payload / "python/python.exe"), "-X", "utf8", str(payload / "run.py"), "--self-test"],
                       cwd=payload, env={**os.environ, "STUDY_DATA_DIR": str(work / "probe-data")},
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90, check=True)
        key = "v" + info["version"] + "-" + uuid.uuid4().hex[:12]
        stage.rename(updates / key)
        return key


def activate(key):
    updates = install_root() / ".updates"
    marker = updates / "active"
    previous = marker.read_text(encoding="ascii") if marker.exists() else ""
    (updates / "previous").write_text(previous, encoding="ascii")
    temp = updates / "active.new"
    temp.write_text(key, encoding="ascii")
    temp.replace(marker)
