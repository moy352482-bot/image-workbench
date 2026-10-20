"""本机进程/线程互斥；进程退出后由操作系统释放，不依赖删除锁文件。"""

import os
from pathlib import Path
import threading
import time


class LockBusyError(ValueError):
    pass


_registry_guard = threading.Lock()
_registry = {}


class FileLock:
    def __init__(self, path: Path, *, timeout: float = 0):
        self.path = Path(path)
        self.timeout = timeout
        self.stream = None
        with _registry_guard:
            self.local = _registry.setdefault(str(self.path.absolute()).lower(), threading.Lock())

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        if not self.local.acquire(timeout=self.timeout):
            raise LockBusyError("已有操作正在进行")
        try:
            self.stream = self.path.open("a+b")
            if self.path.stat().st_size == 0:
                self.stream.write(b"0")
                self.stream.flush()
            while True:
                try:
                    self.stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LockBusyError("已有执行进程持有锁")
                    time.sleep(0.05)
        except BaseException:
            if self.stream:
                self.stream.close()
                self.stream = None
            self.local.release()
            raise

    def __exit__(self, *args):
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None
            self.local.release()
