import os
from pathlib import Path

import pytest

from app.ingestion.languages import Language
from app.ingestion.scanner import is_binary_file, scan_directory

MAX_FILE_BYTES = 1024 * 1024


def write(root: Path, relative_path: str, content: str | bytes = "x = 1\n") -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def scanned_paths(root: Path) -> list[str]:
    return [file.path for file in scan_directory(root, MAX_FILE_BYTES).files]


def test_finds_source_files_with_relative_posix_paths(tmp_path: Path) -> None:
    write(tmp_path, "main.py")
    write(tmp_path, "src/com/app/UserService.java", "class UserService {}")
    write(tmp_path, "web/app.ts", "export const a = 1")

    result = scan_directory(tmp_path, MAX_FILE_BYTES)

    assert [file.path for file in result.files] == [
        "main.py",
        "src/com/app/UserService.java",
        "web/app.ts",
    ]
    assert result.files[1].language == Language.JAVA
    assert result.language_counts == {"java": 1, "python": 1, "typescript": 1}


@pytest.mark.parametrize(
    "ignored_dir",
    [".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build"],
)
def test_skips_ignored_directories(tmp_path: Path, ignored_dir: str) -> None:
    write(tmp_path, "app.py")
    write(tmp_path, f"{ignored_dir}/hidden.py")
    write(tmp_path, f"nested/{ignored_dir}/deep/hidden.js", "var a = 1")

    assert scanned_paths(tmp_path) == ["app.py"]


def test_skips_unsupported_extensions(tmp_path: Path) -> None:
    write(tmp_path, "app.py")
    write(tmp_path, "README.md", "# docs")
    write(tmp_path, "style.css", "body {}")

    result = scan_directory(tmp_path, MAX_FILE_BYTES)

    assert [file.path for file in result.files] == ["app.py"]
    assert result.skipped_files == 2


def test_skips_binary_files_even_with_source_extension(tmp_path: Path) -> None:
    write(tmp_path, "real.py")
    write(tmp_path, "fake.py", b"\x00\x01\x02binary")

    assert scanned_paths(tmp_path) == ["real.py"]


def test_skips_files_larger_than_limit(tmp_path: Path) -> None:
    write(tmp_path, "small.py", "a = 1")
    write(tmp_path, "huge.py", "a = 1\n" * 1000)

    result = scan_directory(tmp_path, max_file_bytes=100)

    assert [file.path for file in result.files] == ["small.py"]


def test_skips_minified_javascript(tmp_path: Path) -> None:
    write(tmp_path, "app.js", "var a = 1")
    write(tmp_path, "vendor.min.js", "var a=1")

    assert scanned_paths(tmp_path) == ["app.js"]


def test_does_not_follow_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write(outside, "secret.py")
    repo = tmp_path / "repo"
    write(repo, "app.py")
    try:
        os.symlink(outside / "secret.py", repo / "link.py")
        os.symlink(outside, repo / "linked_dir", target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this system")

    assert scanned_paths(repo) == ["app.py"]


def test_is_binary_file(tmp_path: Path) -> None:
    write(tmp_path, "text.py", "print('héllo')")
    write(tmp_path, "data.bin", b"abc\x00def")

    assert is_binary_file(tmp_path / "text.py") is False
    assert is_binary_file(tmp_path / "data.bin") is True


def test_empty_directory_returns_no_files(tmp_path: Path) -> None:
    result = scan_directory(tmp_path, MAX_FILE_BYTES)

    assert result.files == []
    assert result.total_size_bytes == 0
