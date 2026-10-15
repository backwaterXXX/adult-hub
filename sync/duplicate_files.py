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
        left_identity = (left_info.st_dev, left_info.st_ino, left_info.st_size,
                         left_info.st_mtime_ns, left_info.st_ctime_ns)
        right_identity = (right_info.st_dev, right_info.st_ino, right_info.st_size,
                          right_info.st_mtime_ns, right_info.st_ctime_ns)
        left_file = os.fdopen(left_fd, "rb")
        left_fd = None
        right_file = os.fdopen(right_fd, "rb")
        right_fd = None
        with left_file, right_file:
            while True:
                chunk_a, chunk_b = left_file.read(1024 * 1024), right_file.read(1024 * 1024)
                if chunk_a != chunk_b:
                    return False
                if not chunk_a:
                    break
            left_after = os.fstat(left_file.fileno())
            right_after = os.fstat(right_file.fileno())
            return (
                (left_after.st_dev, left_after.st_ino, left_after.st_size,
                 left_after.st_mtime_ns, left_after.st_ctime_ns) == left_identity
                and (right_after.st_dev, right_after.st_ino, right_after.st_size,
                     right_after.st_mtime_ns, right_after.st_ctime_ns) == right_identity
            )
    except OSError:
        return False
    finally:
        if left_file is not None and not left_file.closed:
            left_file.close()
        if right_file is not None and not right_file.closed:
            right_file.close()
        if left_fd is not None:
            os.close(left_fd)
        if right_fd is not None:
            os.close(right_fd)


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
    temp_fd = fd
    published = False
    published_identity = None
    source_fd = None
    copied = None
    original = None
    retirement_dir = None
    retired_source = None
    source_retired = False
    preserve_temp = False
    try:
        source_fd = os.open(
            source,
            os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0),
        )
        opened_info = os.fstat(source_fd)
        source_identity = (source_info.st_dev, source_info.st_ino, source_info.st_size,
                           source_info.st_mtime_ns, source_info.st_ctime_ns)
        opened_identity = (opened_info.st_dev, opened_info.st_ino, opened_info.st_size,
                           opened_info.st_mtime_ns, opened_info.st_ctime_ns)
        if opened_identity != source_identity:
            raise RuntimeError("Source file changed before it could be moved.")
        copied = os.fdopen(temp_fd, "wb")
        temp_fd = None
        original = os.fdopen(source_fd, "rb")
        source_fd = None
        with copied, original:
            shutil.copyfileobj(original, copied)
            copied.flush()
            shutil.copystat(source, temp, follow_symlinks=False)
            copied_info = os.fstat(original.fileno())
            copied_identity = (copied_info.st_dev, copied_info.st_ino, copied_info.st_size,
                               copied_info.st_mtime_ns, copied_info.st_ctime_ns)
            if copied_identity != source_identity:
                raise RuntimeError("Source file changed while it was being copied.")
            os.fsync(copied.fileno())
        # os.replace would silently overwrite a path created after the check
        # above. Linking within the target directory is atomic and fails if
        # any file or symlink already occupies the destination.
        temp_info = temp.stat(follow_symlinks=False)
        published_identity = (temp_info.st_dev, temp_info.st_ino)
        os.link(temp, target, follow_symlinks=False)
        published = True
        target_stat = target.stat(follow_symlinks=False)
        if (target_stat.st_dev, target_stat.st_ino) != published_identity:
            raise RuntimeError("Target file changed during publication.")
        target_dir_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(target_dir_fd)
        finally:
            os.close(target_dir_fd)
        current_source = source.stat(follow_symlinks=False)
        current_identity = (current_source.st_dev, current_source.st_ino, current_source.st_size,
                            current_source.st_mtime_ns, current_source.st_ctime_ns)
        if current_identity != source_identity:
            raise RuntimeError("Source file changed before it could be removed.")
        # Move the current path into a private directory atomically, then
        # verify the moved inode before deleting it. A lstat()+unlink() pair
        # could otherwise delete an operator replacement made between calls.
        retirement_dir = Path(tempfile.mkdtemp(
            prefix=f".{source.name}.duplicate-retire-", dir=source.parent,
        ))
        retired_source = retirement_dir / source.name
        os.rename(source, retired_source)
        source_retired = True
        retired_stat = retired_source.stat(follow_symlinks=False)
        retired_identity = (retired_stat.st_dev, retired_stat.st_ino, retired_stat.st_size,
                            retired_stat.st_mtime_ns, retired_stat.st_ctime_ns)
        # rename() itself updates ctime on POSIX; compare the stable file
        # identity and content timestamps here while still detecting a path swap.
        if retired_identity[:4] != source_identity[:4]:
            # Restore without replacing a path another actor may have created.
            try:
                os.link(retired_source, source, follow_symlinks=False)
            except OSError:
                raise RuntimeError(
                    "Source changed during file retirement; the moved file was preserved in a recovery directory."
                ) from None
            retired_source.unlink()
            source_retired = False
            retirement_dir.rmdir()
            retirement_dir = None
            raise RuntimeError("Source file changed before it could be removed.")
        current_target = target.stat(follow_symlinks=False)
        if (current_target.st_dev, current_target.st_ino) != published_identity:
            raise RuntimeError("Target file changed before the source could be removed.")
        retired_source.unlink()
        source_retired = False
        retirement_dir.rmdir()
        retirement_dir = None
        try:
            current_target = target.stat(follow_symlinks=False)
            target_is_published = (current_target.st_dev, current_target.st_ino) == published_identity
        except OSError:
            target_is_published = False
        if not target_is_published:
            try:
                # Restore only into a vacant path. If the source directory is
                # on another filesystem or has a concurrent occupant, retain
                # the verified copy rather than overwrite or discard it.
                os.link(temp, source, follow_symlinks=False)
            except OSError:
                preserve_temp = True
                raise RuntimeError(
                    "Target changed after source retirement; the original file copy was preserved for recovery."
                ) from None
            raise RuntimeError("Target file changed after source retirement; the source was restored.")
        source_dir_fd = os.open(source.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(source_dir_fd)
        finally:
            os.close(source_dir_fd)
        temp.unlink()
    except BaseException:
        for handle in (copied, original):
            if handle is not None and not handle.closed:
                handle.close()
        if temp_fd is not None:
            os.close(temp_fd)
        if source_fd is not None:
            os.close(source_fd)
        if (source_retired and retired_source is not None
                and (retired_source.exists() or retired_source.is_symlink())):
            try:
                # Create-only restoration preserves any concurrent occupant.
                os.link(retired_source, source, follow_symlinks=False)
                retired_source.unlink()
                if retirement_dir is not None:
                    retirement_dir.rmdir()
            except OSError:
                # Keep the moved file in its private directory for manual recovery.
                pass
        if (published and not source.exists() and not source.is_symlink()
                and temp.exists()):
            try:
                # Never overwrite a concurrent source occupant. If a hardlink
                # cannot cross filesystems, leave the verified temp copy for
                # manual recovery instead of deleting the only remaining copy.
                os.link(temp, source, follow_symlinks=False)
            except OSError:
                preserve_temp = True
        if not preserve_temp:
            temp.unlink(missing_ok=True)
        raise
