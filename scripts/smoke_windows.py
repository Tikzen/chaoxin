"""Start the extracted executable under a Unicode path, verify UI and local login."""
import http.cookiejar
import json
import sys
import urllib.error
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.request
from zipfile import ZipFile

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "绿色版 smoke"
    with ZipFile("dist/chaoxin-windows-x64.zip") as z:
        z.extractall(root)
    verify_upgrade = "--verify-published" in sys.argv
    if verify_upgrade:
        # Model an older installed version; the updater must fetch the real published ZIP.
        info_path = root / "payload/build-info.json"
        info = json.loads(info_path.read_text(encoding="utf8"))
        target_version = info["version"]
        info["version"] = "0.0.1"
        info_path.write_text(json.dumps(info), encoding="utf8")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    proc = subprocess.Popen([str(root/"Chaoxin.exe"), "--no-browser", "--port", str(port)], cwd=tmp, stdout=subprocess.DEVNULL, env={**os.environ, "DEMO_TICK_SECONDS": ".02", "PYTHONUTF8": "1"})
    try:
        for _ in range(120):
            if proc.poll() is not None:
                raise ValueError("Launcher exited before readiness")
            try:
                client.open(base + "/api/health", timeout=2).close()
                break
            except OSError:
                time.sleep(.5)
        token = (root/".data/access-token").read_text()
        client.open(urllib.request.Request(base + "/api/auth", data=json.dumps({"token": token}).encode(), headers={"Content-Type":"application/json"})).close()
        assert b"<!doctype html" in client.open(base).read().lower()
        assert json.load(client.open(base + "/api/dashboard"))["accounts"] == []
        def post(path, data):
            return json.load(client.open(urllib.request.Request(base + path, data=json.dumps(data).encode(), headers={"Content-Type":"application/json"})))
        account = post("/api/demo", {})["account_id"]
        courses = json.load(client.open(base + "/api/dashboard"))["courses"]
        course = next(c for c in courses if c["total"] > c["done"])
        task = post("/api/tasks", {"course_ids":[course["id"]], "point_concurrency": 2})["task_ids"][0]
        try:
            post("/api/updates/install", {"version": "v999.0.0"})
        except urllib.error.HTTPError as error:
            assert error.code == 409
        else:
            raise AssertionError("Updater failed to reject an active task")
        for _ in range(90):
            tasks = json.load(client.open(base + "/api/dashboard"))["tasks"]
            current = next(t for t in tasks if t["id"] == task)
            if current["status"] == "completed":
                break
            if current["status"] not in ("queued", "running"):
                raise AssertionError((current["status"], current["state"].get("error")))
            time.sleep(1)
        else:
            raise AssertionError("Demo did not complete")
        if verify_upgrade:
            offered = json.load(client.open(base + "/api/updates/check"))
            assert offered["update_available"] and offered["latest_version"] == "v" + target_version
            assert post("/api/updates/install", {"version": offered["latest_version"]})["restarting"]
            for _ in range(90):
                try:
                    data = json.load(client.open(base + "/api/dashboard", timeout=3))
                    if data["version"] == target_version:
                        assert any(a["id"] == account for a in data["accounts"])
                        assert any(t["id"] == task and t["status"] == "completed" for t in data["tasks"])
                        assert (root / ".data/access-token").read_text() == token
                        break
                except OSError:
                    pass
                time.sleep(1)
            else:
                raise AssertionError("New release failed to start with original data")
    finally:
        subprocess.run(["taskkill", "/pid", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL)
        proc.wait(timeout=30)
