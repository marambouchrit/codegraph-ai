"""Safe extraction of uploaded ZIP archives.

A ZIP file is untrusted input. A malicious archive can try to:
  * write outside the target folder ("zip slip"), e.g. an entry named ../../evil.py
  * exhaust the disk ("zip bomb"): a tiny archive that expands to gigabytes
  * contain symbolic links that point elsewhere on the machine
This module checks every entry *before* writing anything, skips symlinks, and
counts the bytes actually written (archive headers can lie about sizes).
"""

import re
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from app.core.errors import ContentTooLargeError, InvalidArchiveError, UnsafeArchiveError

COPY_CHUNK_BYTES = 64 * 1024
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
# Folders added by macOS when zipping; they never contain source code.
_IGNORED_PREFIXES = ("__MACOSX/",)


def extract_zip_safely(
    archive: BinaryIO | Path,
    destination: Path,
    *,
    max_total_bytes: int,
    max_files: int,
) -> None:
    """Extract `archive` into `destination`, rejecting unsafe or oversized archives."""
    try:
        zip_file = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as error:
        raise InvalidArchiveError("The uploaded file is not a valid ZIP archive.") from error

    with zip_file:
        members = [m for m in zip_file.infolist() if not m.filename.startswith(_IGNORED_PREFIXES)]
        _check_declared_limits(members, max_total_bytes, max_files)

        destination.mkdir(parents=True, exist_ok=True)
        root = destination.resolve()
        # Validate every path first, so a malicious archive is rejected before anything is written.
        targets = [(member, safe_member_path(root, member.filename)) for member in members]

        bytes_written = 0
        for member, target in targets:
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif _is_symlink(member):
                continue
            else:
                remaining = max_total_bytes - bytes_written
                bytes_written += _extract_file(zip_file, member, target, remaining, max_total_bytes)


def safe_member_path(root: Path, member_name: str) -> Path:
    """Return where `member_name` should be extracted, or raise if it escapes `root`."""
    normalized = member_name.replace("\\", "/")
    if normalized.startswith("/") or _WINDOWS_DRIVE_PATTERN.match(normalized):
        raise UnsafeArchiveError(f"Unsafe absolute path in archive: '{member_name}'")

    parts = [part for part in PurePosixPath(normalized).parts if part not in ("", ".")]
    if not parts or ".." in parts:
        raise UnsafeArchiveError(f"Unsafe path in archive: '{member_name}'")

    target = root.joinpath(*parts).resolve()
    # Final safety net: the resolved path must still be inside the destination.
    if not target.is_relative_to(root):
        raise UnsafeArchiveError(f"Unsafe path in archive: '{member_name}'")
    return target


def find_project_root(extract_dir: Path) -> Path:
    """Many archives (including GitHub downloads) wrap everything in one top-level folder.

    If that is the case, return that folder; otherwise return `extract_dir` itself.
    """
    entries = list(extract_dir.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return extract_dir


def _check_declared_limits(
    members: list[zipfile.ZipInfo], max_total_bytes: int, max_files: int
) -> None:
    if len(members) > max_files:
        raise ContentTooLargeError(
            f"The archive contains {len(members)} entries; the limit is {max_files}."
        )
    declared_size = sum(member.file_size for member in members)
    if declared_size > max_total_bytes:
        raise ContentTooLargeError(
            f"The archive expands to {_to_mb(declared_size)} MB; "
            f"the limit is {_to_mb(max_total_bytes)} MB."
        )


def _extract_file(
    zip_file: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    target: Path,
    remaining_bytes: int,
    max_total_bytes: int,
) -> int:
    """Copy one entry to disk in small chunks and return the number of bytes written."""
    if member.flag_bits & 0x1:
        raise InvalidArchiveError("Password-protected ZIP archives are not supported.")

    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with zip_file.open(member) as source, target.open("wb") as output:
            while chunk := source.read(COPY_CHUNK_BYTES):
                written += len(chunk)
                if written > remaining_bytes:
                    raise ContentTooLargeError(
                        f"The archive expands to more than {_to_mb(max_total_bytes)} MB."
                    )
                output.write(chunk)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, OSError) as error:
        raise InvalidArchiveError(f"Could not extract '{member.filename}': {error}") from error
    return written


def _is_symlink(member: zipfile.ZipInfo) -> bool:
    # On Unix, the upper 16 bits of external_attr hold the file mode.
    unix_mode = member.external_attr >> 16
    return (unix_mode & 0o170000) == 0o120000


def _to_mb(size_bytes: int) -> int:
    return size_bytes // (1024 * 1024)
