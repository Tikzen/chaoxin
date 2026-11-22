"""Exercise a real Windows read handle, including permanent sharing denial."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from app.state_io import write_json


def main():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        path.write_bytes(b'{"old":true}')
        # Prove this exact sharing mode reproduces the failure without retries.
        probe = path.with_suffix(".new")
        probe.write_bytes(b"{}")
        with path.open("rb") as reader:
            try:
                os.replace(probe, path)
            except PermissionError as exc:
                assert exc.winerror in (5, 32, 33)
            else:
                raise AssertionError("Expected Windows to deny replacing an open file")
            def release():
                time.sleep(.15)
                reader.close()
            thread = threading.Thread(target=release)
            thread.start()
            try:
                write_json(path, {"progress": 42})
            finally:
                thread.join()
        assert json.loads(path.read_bytes()) == {"progress": 42}
        with path.open("rb"):
            try:
                write_json(path, {"progress": 99})
            except PermissionError:
                pass
            else:
                raise AssertionError("Expected persistent denial to surface")
        assert json.loads(path.read_bytes()) == {"progress": 42}
        assert not list(path.parent.glob("*.tmp"))


if __name__ == "__main__":
    main()
