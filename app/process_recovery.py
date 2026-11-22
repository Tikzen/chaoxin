"""Reattach surviving macOS workers after a controller exits unexpectedly."""
import os
import shlex
import shutil
import signal
import subprocess
import time
import sys
from pathlib import Path

from app import store


class InspectionUnavailable(Exception):
    pass


def identity(pid):
    try:
        if os.name == 'nt':
            import psutil
            try:
                process = psutil.Process(pid)
                if process.status() == psutil.STATUS_ZOMBIE:
                    return None
                return (round(process.create_time(), 3), process.exe()), subprocess.list2cmdline(process.cmdline())
            except psutil.NoSuchProcess:
                return None
            except psutil.AccessDenied as error:
                raise InspectionUnavailable() from error
        result = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'stat=,lstart=,args='],
                                capture_output=True, text=True, timeout=2)
        fields = result.stdout.strip().split(None, 6)
        if result.returncode:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return None
            raise InspectionUnavailable()
        if len(fields) != 7:
            raise InspectionUnavailable()
        if fields[0].startswith('Z'):
            return None
        return tuple(fields[1:6]), fields[6]
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InspectionUnavailable() from error


class AttachedWorker:
    _study_group = True

    def __init__(self, pid, signature):
        self.pid, self.signature = pid, signature
        self.returncode = None

    @classmethod
    def attach(cls, pid, path):
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
            return None
        signature = identity(pid)
        if signature is None:
            return None
        try:
            if os.name == 'nt':
                import psutil
                executable = Path(signature[0][1]).resolve()
                if executable != Path(sys.executable).resolve():
                    return None
                command = signature[1]
                if '--worker' not in command and str(store.ROOT / 'app' / 'worker.py') not in command:
                    return None
                if '--study-task-id=' + Path(path).name not in command:
                    return None
                if Path(psutil.Process(pid).cwd()).resolve() != Path(path).resolve() or identity(pid) != signature:
                    return None
            else:
                args = shlex.split(signature[1])
                if (not args or not Path(args[0]).name.lower().startswith('python')
                        or (args[-1] != str(store.ROOT / 'app/worker.py') and args[-2:] != [str(store.ROOT / 'app/worker.py'), '--study-task-id=' + Path(path).name]) or os.getpgid(pid) != pid):
                    return None
                lsof = shutil.which('lsof')
                if not lsof:
                    raise InspectionUnavailable()
                result = subprocess.run([lsof, '-a', '-p', str(pid), '-d', 'cwd', '-Fn'],
                                        capture_output=True, text=True, timeout=3)
                directories = [Path(line[1:]).resolve() for line in result.stdout.splitlines() if line.startswith('n')]
                if result.returncode:
                    raise InspectionUnavailable()
                if directories != [Path(path).resolve()] or identity(pid) != signature:
                    return None
        except ProcessLookupError:
            return None
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise InspectionUnavailable() from error
        return cls(pid, signature)

    def poll(self):
        try:
            current = identity(self.pid)
        except InspectionUnavailable:
            return self.returncode
        if self.returncode is None and current != self.signature:
            # An adopted process is not our child: the durable state file is
            # authoritative for success/failure, rather than waitpid's code.
            self.returncode = 0
        return self.returncode

    def signal(self, value):
        try:
            if self.returncode is None and identity(self.pid) == self.signature:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(self.pid), '/T', '/F'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                else:
                    os.kill(self.pid, value)
        except (ProcessLookupError, InspectionUnavailable):
            pass

    def terminate(self):
        self.signal(signal.SIGTERM)

    def kill(self):
        self.signal(getattr(signal, "SIGKILL", signal.SIGTERM))

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while self.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired('attached worker', timeout)
            time.sleep(.05)
        return self.returncode

    def safe_group(self):
        try:
            current = identity(self.pid)
        except InspectionUnavailable:
            return False
        return current is None or current == self.signature
