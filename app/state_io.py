"""Publish complete JSON snapshots even when a Windows reader briefly holds the old file."""
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import time


def write_json(path, value):
    path = Path(path)
    stream = NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp",
                                delete=False, mode="w", encoding="utf-8")
    tmp = Path(stream.name)
    try:
        with stream:
            json.dump(value, stream, ensure_ascii=False)
        deadline = time.monotonic() + 1
        delay = .01
        while True:
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:
                # Windows may report ACCESS_DENIED or SHARING_VIOLATION for a
                # destination opened without FILE_SHARE_DELETE. Never unlink it.
                remaining = deadline - time.monotonic()
                if getattr(exc, "winerror", None) not in (5, 32, 33) or remaining <= 0:
                    raise
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, .1)
    finally:
        tmp.unlink(missing_ok=True)
