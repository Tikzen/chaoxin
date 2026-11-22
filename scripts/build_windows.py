"""Assemble a relocatable runtime from uv's managed CPython and locked wheels."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.platform != "win32":
        sys.exit("Build on a Windows x64 runner")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf8"))["project"]["version"]
    ref = os.environ.get("GITHUB_REF", "")
    if ref.startswith("refs/tags/") and ref != "refs/tags/v" + version:
        sys.exit("Tag and package version differ")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        stage = work / "portable"
        payload = stage / "payload"
        python = payload / "python"
        env = {**os.environ, "UV_PYTHON_INSTALL_DIR": str(work / "cpython")}
        subprocess.run(["uv", "python", "install", "--no-bin", "3.13"], env=env, check=True)
        exe = subprocess.check_output(["uv", "python", "find", "--system", "--managed-python", "3.13"], env=env, text=True).strip()
        if not Path(exe).resolve().is_relative_to(work):
            sys.exit("Expected isolated managed Python")
        shutil.copytree(Path(exe).parent, python, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        req = work / "requirements.txt"
        subprocess.run(["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "-o", str(req)], stdout=subprocess.DEVNULL, check=True)
        subprocess.run(["uv", "pip", "install", "--python", str(python / "python.exe"), "--target", str(python / "Lib/site-packages"), "-r", str(req)], check=True)
        tracked = subprocess.check_output(["git", "ls-files", "-z", "--", "app", "static", "vendor", "LICENSE", "run.py"])
        for entry in tracked.decode("utf8").split("\0"):
            if entry:
                dest = payload / entry
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / entry, dest)
        (payload / "build-info.json").write_text(json.dumps(dict(version=version, repository="Tikzen/chaoxin", launcher_protocol=1)), encoding="utf8")
        subprocess.run([sys.executable, "-m", "PyInstaller", "--onefile", "--clean", "--name", "Chaoxin", "--distpath", str(stage), "--workpath", str(work/"boot"), "--specpath", tmp, str(ROOT/"windows/launcher.py")], cwd=tmp, check=True)
        output = ROOT / "dist/chaoxin-windows-x64.zip"
        output.parent.mkdir(exist_ok=True)
        with ZipFile(output, "w", ZIP_DEFLATED, compresslevel=5) as z:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    z.write(path, path.relative_to(stage))
        with output.open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        Path(str(output) + ".sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")


if __name__ == "__main__":
    main()
