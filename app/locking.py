"""Small cross-platform file locks used for durable shared state."""
from contextlib import contextmanager
import errno
import os
import time


@contextmanager
def file_lock(path, *, blocking=True):
    path = os.fspath(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if not blocking:
                        raise BlockingIOError(errno.EWOULDBLOCK, "file is locked") from error
                    time.sleep(0.05)
        else:
            import fcntl

            operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), operation)
        try:
            yield handle
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
