"""File operations shared by duplicate decision and recovery jobs."""
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

from common import db

def _write_catalog_backup(conn: db.Connection, op_dir: Path, loser_ids: list) -> Path:
    """Write a JSON recovery copy of the affected catalog rows."""
    backup_path = op_dir / "catalog.json"
    if backup_path.exists():
        return backup_path
    payload = []
    video_cols = db.table_columns(conn, "videos")
    sf_cols = db.table_columns(conn, "synced_files")
    vc_cols = db.table_columns(conn, "video_categories")
    for loser in loser_ids:
        video_row = conn.execute("SELECT * FROM videos WHERE id=%s", (loser,)).fetchone()
        files = conn.execute("SELECT * FROM synced_files WHERE video_id=%s", (loser,)).fetchall()
        cats = conn.execute("SELECT * FROM video_categories WHERE video_id=%s", (loser,)).fetchall()
        payload.append({
            "video": dict(zip(video_cols, video_row)) if video_row else None,
            "synced_files": [dict(zip(sf_cols, r)) for r in files],
            "categories": [dict(zip(vc_cols, r)) for r in cats],
        })
    tmp = backup_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    os.replace(tmp, backup_path)
    return backup_path


def _stat_identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _streams_equal(left_file, right_file) -> bool:
    while True:
        chunk_a, chunk_b = left_file.read(1024 * 1024), right_file.read(1024 * 1024)
        if chunk_a != chunk_b:
            return False
        if not chunk_a:
            return True


def _release_handles(left_file, right_file, left_fd, right_fd) -> None:
    if left_file is not None and not left_file.closed:
        left_file.close()
    if right_file is not None and not right_file.closed:
        right_file.close()
    if left_fd is not None:
        os.close(left_fd)
    if right_fd is not None:
        os.close(right_fd)


def _duplicate_file_matches(left: Path, right: Path) -> bool:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        return False
    left_fd = right_fd = None
    left_file = right_file = None
    try:
        left_fd = os.open(left, os.O_RDONLY | nofollow)
        right_fd = os.open(right, os.O_RDONLY | nofollow)
        left_info = os.fstat(left_fd)
        right_info = os.fstat(right_fd)
        if (not stat.S_ISREG(left_info.st_mode) or not stat.S_ISREG(right_info.st_mode)
                or left_info.st_size != right_info.st_size):
            return False
        left_identity = _stat_identity(left_info)
        right_identity = _stat_identity(right_info)
        left_file = os.fdopen(left_fd, "rb")
        left_fd = None
        right_file = os.fdopen(right_fd, "rb")
        right_fd = None
        with left_file, right_file:
            if not _streams_equal(left_file, right_file):
                return False
            left_after = os.fstat(left_file.fileno())
            right_after = os.fstat(right_file.fileno())
            return (
                _stat_identity(left_after) == left_identity
                and _stat_identity(right_after) == right_identity
            )
    except OSError:
        return False
    finally:
        _release_handles(left_file, right_file, left_fd, right_fd)


class _MoveState:
    """Mutable bookkeeping for one _duplicate_move_file call, read by its cleanup."""

    def __init__(self, temp_fd: int):
        self.temp_fd = temp_fd
        self.published = False
        self.published_identity = None
        self.source_fd = None
        self.copied = None
        self.original = None
        self.retirement_dir = None
        self.retired_source = None
        self.source_retired = False
        self.preserve_temp = False


def _copy_source_to_temp(source: Path, temp: Path, nofollow: int,
                         source_identity: tuple, state: _MoveState) -> None:
    state.source_fd = os.open(
        source,
        os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0),
    )
    opened_info = os.fstat(state.source_fd)
    if _stat_identity(opened_info) != source_identity:
        raise RuntimeError("Source file changed before it could be moved.")
    state.copied = os.fdopen(state.temp_fd, "wb")
    state.temp_fd = None
    state.original = os.fdopen(state.source_fd, "rb")
    state.source_fd = None
    with state.copied, state.original:
        shutil.copyfileobj(state.original, state.copied)
        state.copied.flush()
        shutil.copystat(source, temp, follow_symlinks=False)
        copied_info = os.fstat(state.original.fileno())
        if _stat_identity(copied_info) != source_identity:
            raise RuntimeError("Source file changed while it was being copied.")
        os.fsync(state.copied.fileno())


def _fsync_directory(directory: Path) -> None:
    dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _publish_temp(target: Path, temp: Path, state: _MoveState) -> None:
    # os.replace would silently overwrite a path created after the check
    # in _duplicate_move_file. Linking within the target directory is atomic
    # and fails if any file or symlink already occupies the destination.
    temp_info = temp.stat(follow_symlinks=False)
    state.published_identity = (temp_info.st_dev, temp_info.st_ino)
    os.link(temp, target, follow_symlinks=False)
    state.published = True
    target_stat = target.stat(follow_symlinks=False)
    if (target_stat.st_dev, target_stat.st_ino) != state.published_identity:
        raise RuntimeError("Target file changed during publication.")
    _fsync_directory(target.parent)


def _restore_mismatched_source(source: Path, state: _MoveState) -> None:
    # Restore without replacing a path another actor may have created.
    try:
        os.link(state.retired_source, source, follow_symlinks=False)
    except OSError:
        raise RuntimeError(
            "Source changed during file retirement; the moved file was preserved in a recovery directory."
        ) from None
    state.retired_source.unlink()
    state.source_retired = False
    state.retirement_dir.rmdir()
    state.retirement_dir = None
    raise RuntimeError("Source file changed before it could be removed.")


def _retire_source(source: Path, source_identity: tuple, state: _MoveState) -> None:
    current_source = source.stat(follow_symlinks=False)
    if _stat_identity(current_source) != source_identity:
        raise RuntimeError("Source file changed before it could be removed.")
    # Move the current path into a private directory atomically, then
    # verify the moved inode before deleting it. A lstat()+unlink() pair
    # could otherwise delete an operator replacement made between calls.
    state.retirement_dir = Path(tempfile.mkdtemp(
        prefix=f".{source.name}.duplicate-retire-", dir=source.parent,
    ))
    state.retired_source = state.retirement_dir / source.name
    os.rename(source, state.retired_source)
    state.source_retired = True
    retired_stat = state.retired_source.stat(follow_symlinks=False)
    # rename() itself updates ctime on POSIX; compare the stable file
    # identity and content timestamps here while still detecting a path swap.
    if _stat_identity(retired_stat)[:4] != source_identity[:4]:
        _restore_mismatched_source(source, state)


def _drop_retired_source(target: Path, state: _MoveState) -> None:
    current_target = target.stat(follow_symlinks=False)
    if (current_target.st_dev, current_target.st_ino) != state.published_identity:
        raise RuntimeError("Target file changed before the source could be removed.")
    state.retired_source.unlink()
    state.source_retired = False
    state.retirement_dir.rmdir()
    state.retirement_dir = None


def _confirm_target_or_restore(source: Path, target: Path, temp: Path, state: _MoveState) -> None:
    try:
        current_target = target.stat(follow_symlinks=False)
        target_is_published = (current_target.st_dev, current_target.st_ino) == state.published_identity
    except OSError:
        target_is_published = False
    if not target_is_published:
        try:
            # Restore only into a vacant path. If the source directory is
            # on another filesystem or has a concurrent occupant, retain
            # the verified copy rather than overwrite or discard it.
            os.link(temp, source, follow_symlinks=False)
        except OSError:
            state.preserve_temp = True
            raise RuntimeError(
                "Target changed after source retirement; the original file copy was preserved for recovery."
            ) from None
        raise RuntimeError("Target file changed after source retirement; the source was restored.")
    _fsync_directory(source.parent)
    temp.unlink()


def _close_partial_handles(state: _MoveState) -> None:
    for handle in (state.copied, state.original):
        if handle is not None and not handle.closed:
            handle.close()
    if state.temp_fd is not None:
        os.close(state.temp_fd)
    if state.source_fd is not None:
        os.close(state.source_fd)


def _restore_retired_source(source: Path, state: _MoveState) -> None:
    retired_source = state.retired_source
    if (state.source_retired and retired_source is not None
            and (retired_source.exists() or retired_source.is_symlink())):
        try:
            # Create-only restoration preserves any concurrent occupant.
            os.link(retired_source, source, follow_symlinks=False)
            retired_source.unlink()
            if state.retirement_dir is not None:
                state.retirement_dir.rmdir()
        except OSError:
            # Keep the moved file in its private directory for manual recovery.
            pass


def _restore_temp_copy(source: Path, temp: Path, state: _MoveState) -> None:
    if (state.published and not source.exists() and not source.is_symlink()
            and temp.exists()):
        try:
            # Never overwrite a concurrent source occupant. If a hardlink
            # cannot cross filesystems, leave the verified temp copy for
            # manual recovery instead of deleting the only remaining copy.
            os.link(temp, source, follow_symlinks=False)
        except OSError:
            state.preserve_temp = True
    if not state.preserve_temp:
        temp.unlink(missing_ok=True)


def _recover_failed_move(source: Path, temp: Path, state: _MoveState) -> None:
    _close_partial_handles(state)
    _restore_retired_source(source, state)
    _restore_temp_copy(source, temp, state)


def _duplicate_move_file(source: Path, target: Path) -> None:
    """Copy then publish with an atomic create-only link beside the target."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("Safe file moves require O_NOFOLLOW support.")
    if source.is_symlink():
        raise FileExistsError(f"Source file is a symlink: {source.name}")
    source_info = source.stat(follow_symlinks=False)
    if not stat.S_ISREG(source_info.st_mode):
        raise FileExistsError(f"Source path is not a regular file: {source.name}")
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Target file already exists: {target.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.duplicate-tmp-", dir=target.parent)
    temp = Path(temp_name)
    state = _MoveState(fd)
    source_identity = _stat_identity(source_info)
    try:
        _copy_source_to_temp(source, temp, nofollow, source_identity, state)
        _publish_temp(target, temp, state)
        _retire_source(source, source_identity, state)
        _drop_retired_source(target, state)
        _confirm_target_or_restore(source, target, temp, state)
    except BaseException:
        _recover_failed_move(source, temp, state)
        raise

