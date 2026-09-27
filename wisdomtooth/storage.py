"""Writing the state files every server process on the machine shares.

An MCP client starts one server per session, so the ledger, the account meter
and the advisor store are read and written by several processes at once.
`locked` serialises a read-modify-write across processes with an advisory
lock on a sidecar `.lock` file; `write_atomic` replaces a file in one step, so
a reader never sees half of it.
"""

import contextlib
import os
import tempfile
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl

# How long to wait for another process's lock before going ahead without it.
# State writes are small; a holder this slow is stuck, and a lost record is
# better than a consult that hangs.
LOCK_TIMEOUT_S = 10.0


@contextlib.contextmanager
def locked(path: str, timeout: float = LOCK_TIMEOUT_S):
    """Hold the cross-process lock for `path` while the block runs."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.005)
        yield
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def write_atomic(path: str, text: str, private: bool = False) -> None:
    """Replace `path` with `text` in one step; owner-only if `private`."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".",
                               suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if not private:
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o644)
        _replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _replace(src: str, dst: str) -> None:
    # On Windows the rename fails while another process has `dst` open for
    # reading; that lasts milliseconds, so retry briefly.
    for attempt in range(50):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 49:
                raise
            time.sleep(0.02)


def append_line(path: str, line: str, private: bool = False) -> None:
    """Append one line, creating the file (owner-only if `private`)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    mode = 0o600 if private else 0o644
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")
