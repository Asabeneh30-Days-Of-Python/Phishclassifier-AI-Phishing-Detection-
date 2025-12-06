# api/io_utils.py
"""
Small filesystem helpers used by persistence and tasks for atomic writes,
ownership normalization, and safe chmod/chown attempts.

Functions:
- atomic_write(path, data, mode=0o644, owner_uid=None, owner_gid=None)
"""
import os
import tempfile
import errno
import logging

logger = logging.getLogger(__name__)


def atomic_write(path: str, data: str, mode: int = 0o644, owner_uid: int | None = None, owner_gid: int | None = None) -> None:
    """
    Atomically write `data` to `path`.

    Steps:
    - create temp file in same directory
    - write, flush, fsync
    - try fchown on fd if owner args provided
    - close and os.replace to final path
    - chmod final path and best-effort chown

    Raises exceptions on write/replace failures.
    """
    dirpath = os.path.dirname(path) or "."
    os.makedirs(dirpath, exist_ok=True)

    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=dirpath, text=True)
        # write bytes
        if isinstance(data, str):
            b = data.encode("utf-8")
        else:
            b = data
        os.write(fd, b)
        try:
            os.fsync(fd)
        except Exception:
            logger.debug("atomic_write: fsync failed for %s", tmp_path, exc_info=True)
        # attempt fchown on fd
        if owner_uid is not None or owner_gid is not None:
            try:
                os.fchown(fd, owner_uid if owner_uid is not None else -1, owner_gid if owner_gid is not None else -1)
            except Exception:
                logger.debug("atomic_write: fchown failed for %s", tmp_path, exc_info=True)
        os.close(fd)
        fd = None
        # atomic replace
        os.replace(tmp_path, path)
        tmp_path = None
        try:
            os.chmod(path, mode)
        except Exception:
            logger.debug("atomic_write: chmod failed for %s", path, exc_info=True)
        # best-effort chown
        if owner_uid is not None and owner_gid is not None:
            try:
                os.chown(path, owner_uid, owner_gid)
            except Exception:
                logger.debug("atomic_write: chown failed for %s", path, exc_info=True)
    except Exception:
        # cleanup and re-raise
        try:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise
