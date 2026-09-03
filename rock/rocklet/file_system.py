import asyncio
import os
import shutil
import stat
import tempfile
import zipfile
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from rock.actions import (
    FileEntry,
    FileEntryType,
    FilePathRequest,
    ListDirectoryRequest,
    MakeDirectoryResponse,
)

_READ_CHUNK_SIZE = 64 * 1024
_MAX_LIST_ENTRIES = 10_000


def resolve_path(value: str) -> Path:
    return Path(value)


def store_upload(stream, target_path: Path, unzip: bool) -> None:
    if target_path.is_dir() and not unzip:
        raise IsADirectoryError(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp_dir:
        file_path = Path(temp_dir) / "temp_file_transfer"
        with file_path.open("wb") as destination:
            shutil.copyfileobj(stream, destination, length=shutil.COPY_BUFSIZE)
        if unzip:
            with zipfile.ZipFile(file_path, "r") as zip_ref:
                zip_ref.extractall(target_path)
            file_path.unlink()
        else:
            shutil.move(file_path, target_path)


def _owner_name(uid: int) -> str:
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return str(uid)


def _group_name(gid: int) -> str:
    try:
        import grp

        return grp.getgrgid(gid).gr_name
    except (ImportError, KeyError):
        return str(gid)


def _entry_from_path(path: Path) -> FileEntry:
    entry_stat = path.lstat()
    target_stat = entry_stat
    symlink_target = None
    if stat.S_ISLNK(entry_stat.st_mode):
        symlink_target = str(path.resolve(strict=True))
        target_stat = path.stat()

    entry_type = FileEntryType.DIR if stat.S_ISDIR(target_stat.st_mode) else FileEntryType.FILE
    return FileEntry(
        name=path.name,
        type=entry_type,
        path=str(path),
        size=entry_stat.st_size,
        mode=stat.S_IMODE(target_stat.st_mode),
        permissions=stat.filemode(entry_stat.st_mode),
        owner=_owner_name(entry_stat.st_uid),
        group=_group_name(entry_stat.st_gid),
        modified_time=datetime.fromtimestamp(entry_stat.st_mtime, tz=timezone.utc),
        symlink_target=symlink_target,
    )


def _make_directory(path: Path) -> MakeDirectoryResponse:
    try:
        path.mkdir(parents=True)
    except FileExistsError:
        if path.is_dir():
            return MakeDirectoryResponse(created=False)
        raise NotADirectoryError(f"Path exists and is not a directory: {path}") from None
    return MakeDirectoryResponse(created=True)


def _list_directory(path: Path, depth: int) -> list[FileEntry]:
    if not path.is_dir():
        raise NotADirectoryError(f"Not a directory: {path}")

    entries: list[FileEntry] = []

    def visit(directory: Path, current_depth: int) -> None:
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda child: child.name)
        for child in children:
            if len(entries) >= _MAX_LIST_ENTRIES:
                raise ValueError(f"Directory listing exceeds {_MAX_LIST_ENTRIES} entries")
            child_path = Path(child.path)
            entries.append(_entry_from_path(child_path))
            if current_depth < depth and child.is_dir(follow_symlinks=False):
                visit(child_path, current_depth + 1)

    visit(path, 1)
    entries.sort(key=lambda entry: entry.path)
    return entries


async def make_directory(request: FilePathRequest) -> MakeDirectoryResponse:
    return await asyncio.to_thread(_make_directory, resolve_path(request.path))


async def list_directory(request: ListDirectoryRequest) -> list[FileEntry]:
    return await asyncio.to_thread(_list_directory, resolve_path(request.path), request.depth)


async def stat_path(request: FilePathRequest) -> FileEntry:
    return await asyncio.to_thread(_entry_from_path, resolve_path(request.path))


async def read_file_chunks(request: FilePathRequest) -> AsyncIterator[bytes]:
    file = await asyncio.to_thread(resolve_path(request.path).open, "rb")
    try:
        while chunk := await asyncio.to_thread(file.read, _READ_CHUNK_SIZE):
            yield chunk
    finally:
        await asyncio.to_thread(file.close)
