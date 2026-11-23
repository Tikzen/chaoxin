"""Runtime paths and child-worker launch shared by source and frozen builds."""
import os
import sys
from pathlib import Path


MAX_POINT_CONCURRENCY = 10


def resource_root():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS).resolve()
    return Path(__file__).resolve().parents[1]


def data_root():
    override = os.environ.get("STUDY_DATA_DIR")
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / ".data"
    return resource_root() / ".data"


def worker_command(*arguments):
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker", *map(str, arguments)]
    return [sys.executable, "-X", "utf8", str(resource_root() / "app" / "worker.py"), *map(str, arguments)]


def worker_popen_options():
    if os.name == "nt":
        return {"creationflags": getattr(__import__("subprocess"), "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}
