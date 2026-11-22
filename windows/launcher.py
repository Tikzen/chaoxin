"""Stable bootstrap: every payload shares the portable folder's data directory."""
import os
import re
import subprocess
import sys
from pathlib import Path


def active(root):
    try:
        name = (root / ".updates/active").read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return root / "payload"
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{12}", name):
        raise ValueError("更新版本记录损坏")
    return root / ".updates" / name / "payload"


def launch():
    root = Path(sys.executable).resolve().parent
    env = {**os.environ, "STUDY_PORTABLE_WINDOWS": "1", "STUDY_PORTABLE_ROOT": str(root),
           "STUDY_DATA_DIR": os.environ.get("STUDY_DATA_DIR") or str(root / ".data"),
           "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1"}
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    # PyInstaller modifies the DLL search path for its own bootstrap.
    import ctypes
    ctypes.windll.kernel32.SetDllDirectoryW(None)
    while True:
        payload = active(root)
        cmd = [str(payload / "python/python.exe"), "-X", "utf8", str(payload / "run.py"), *sys.argv[1:]]
        result = subprocess.run(cmd, cwd=payload, env=env).returncode
        if result != 75:
            return result


if __name__ == "__main__":
    try:
        sys.exit(launch())
    except (OSError, ValueError) as exc:
        print(f"启动失败，请确认绿色版已完整解压：{exc}")
        if sys.stdin.isatty():
            input("按 Enter 退出…")
        sys.exit(1)
