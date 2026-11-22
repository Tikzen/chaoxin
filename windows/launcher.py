"""Tiny console launcher for the self-contained Windows portable folder."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(sys.executable).resolve().parent
    python = root / "python" / "python.exe"
    entry = root / "run.py"
    if not python.is_file() or not entry.is_file():
        print("课栈文件不完整。请完整解压整个 Windows 绿色版文件夹后再运行。")
        input("按 Enter 退出…")
        return 1
    try:
        info = json.loads((root / "build-info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    environment = os.environ.copy()
    environment["STUDY_PORTABLE_WINDOWS"] = "1"
    environment["STUDY_LAUNCHER_PID"] = str(os.getpid())
    if info.get("repository"):
        environment["STUDY_GITHUB_REPOSITORY"] = info["repository"]
    command = [str(python), "-X", "utf8", str(entry), *sys.argv[1:]]
    try:
        child = subprocess.Popen(command, cwd=root, env=environment)
        return child.wait()
    except (OSError, KeyboardInterrupt) as error:
        print(f"启动课栈失败：{error}")
        input("按 Enter 退出…")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
