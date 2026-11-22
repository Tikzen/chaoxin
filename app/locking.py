"""Small cross-platform file locks used for durable shared state."""
from contextlib import contextmanager
import errno
import os
import time


@contextmanager
def descriptor_lock(descriptor, *, blocking=True):
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_END)
        if os.lseek(descriptor, 0, os.SEEK_END) == 0:
            os.write(descriptor, b"\0")
        while True:
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                break
            except OSError as error:
                if not blocking:
                    raise BlockingIOError(errno.EWOULDBLOCK, "file is locked") from error
                time.sleep(0.05)
        try:
            yield
        finally:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl
    operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    fcntl.flock(descriptor, operation)
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def file_lock(path, *, blocking=True):
    path = os.fspath(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
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
