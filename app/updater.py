"""GitHub Releases updater for the portable Windows build."""
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath

import requests

from app import store

ASSET_NAME = "chaoxin-windows-x64.zip"
CHECKSUM_NAME = ASSET_NAME + ".sha256"
MAX_RELEASE_BYTES = 600 * 1024 * 1024


def install_root():
    return store.ROOT


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
        return {"version": project.get("version", "0.0.0"), "repository": ""}
    except (OSError, ValueError, KeyError):
        return {"version": "0.0.0", "repository": ""}


def repository():
    configured = store.setting("update_repository", "")
    value = configured or os.environ.get("STUDY_GITHUB_REPOSITORY") or build_info().get("repository", "")
    value = str(value).strip()
    match = re.fullmatch(r"(?:https://github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", value)
    if not match:
        return ""
    return f"{match.group(1)}/{match.group(2)}"


def version_tuple(value):
    numbers = re.findall(r"\d+", str(value))
    return tuple(int(number) for number in numbers[:4]) or (0,)


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
        raise ValueError("该仓库还没有已发布的 GitHub Release")
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


def _trusted_asset_url(url, repo):
    return (url.startswith(f"https://github.com/{repo}/releases/download/")
            and "?" not in url and "#" not in url)


def _safe_extract(archive_path, destination):
    required = {"Chaoxin.exe", "python/python.exe", "app/main.py", "static/index.html"}
    top_levels = {"Chaoxin.exe", "python", "app", "static", "vendor", "scripts", "README-Windows.txt", "LICENSE", "build-info.json", "run.py"}
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        found = set()
        for info in archive.infolist():
            name = PurePosixPath(info.filename.replace("\\", "/"))
            if name.is_absolute() or any(part in ("..", "") for part in name.parts):
                raise ValueError("更新包包含不安全的路径")
            if not name.parts or name.parts[0] not in top_levels:
                raise ValueError("更新包包含未知文件")
            relative = name.as_posix()
            if relative in required:
                found.add(relative)
            target = destination.joinpath(*name.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
        if not required.issubset(found):
            raise ValueError("更新包缺少程序文件")


def download_release(release):
    if os.name != "nt" or os.environ.get("STUDY_PORTABLE_WINDOWS") != "1":
        raise ValueError("一键更新仅适用于 Windows 免安装版")
    if not release.get("configured") or not release.get("update_available"):
        raise ValueError("当前没有可下载的更新")
    if not release.get("asset_available"):
        raise ValueError("该 Release 未附带 Windows x64 更新包或校验文件")
    if release.get("asset_size", 0) > MAX_RELEASE_BYTES:
        raise ValueError("更新包超过允许的大小")
    if not (_trusted_asset_url(release.get("download_url", ""), release["repository"])
            and _trusted_asset_url(release.get("checksum_url", ""), release["repository"])):
        raise ValueError("Release 下载地址无效")
    tag = re.sub(r"[^A-Za-z0-9._-]", "_", release["latest_version"])
    updates = install_root() / ".updates"
    updates.mkdir(parents=True, exist_ok=True)
    working = updates / ("download-" + tag)
    shutil.rmtree(working, ignore_errors=True)
    working.mkdir()
    archive_path = working / ASSET_NAME
    actual = _download(release["download_url"], archive_path, MAX_RELEASE_BYTES)
    checksum_path = working / CHECKSUM_NAME
    _download(release["checksum_url"], checksum_path, 4096)
    expected_match = re.search(r"\b([a-fA-F0-9]{64})\b", checksum_path.read_text(encoding="utf-8", errors="replace"))
    if not expected_match or actual.lower() != expected_match.group(1).lower():
        shutil.rmtree(working, ignore_errors=True)
        raise ValueError("更新包 SHA-256 校验失败")
    stage = updates / ("ready-" + tag)
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    try:
        _safe_extract(archive_path, stage)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    (stage / "release.json").write_text(json.dumps({"version": release["latest_version"],
        "repository": release["repository"], "sha256": actual}, ensure_ascii=False), encoding="utf-8")
    shutil.rmtree(working, ignore_errors=True)
    return {"downloaded": True, "version": release["latest_version"], "stage": str(stage)}


def pending_update():
    updates = install_root() / ".updates"
    candidates = sorted(updates.glob("ready-*/release.json"), key=lambda p: p.stat().st_mtime, reverse=True) if updates.exists() else []
    if not candidates:
        return None
    try:
        value = json.loads(candidates[0].read_text(encoding="utf-8"))
        value["stage"] = str(candidates[0].parent)
        return value
    except (OSError, ValueError):
        return None
