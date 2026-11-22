"""Start the extracted executable under a Unicode path, verify UI and local login."""
import http.cookiejar
import json
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
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    proc = subprocess.Popen([str(root/"Chaoxin.exe"), "--no-browser", "--port", str(port)], cwd=tmp, stdout=subprocess.DEVNULL)
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
    finally:
        subprocess.run(["taskkill", "/pid", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL)
        proc.wait(timeout=30)
